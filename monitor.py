"""
邮箱验证码监控 - 多账号，支持 Gmail(应用密码/Push) + Outlook(OAuth2)
"""
import os, re, time, imaplib, email as email_lib, logging, httpx, yaml, html, threading, base64, json, ssl
from html.parser import HTMLParser
from email.header import decode_header
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/config.yaml")

def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError("config.yaml 内容为空或格式错误")
        return data
    except FileNotFoundError:
        print(f"\n[ERROR] 找不到配置文件：{CONFIG_FILE}\n请先创建 config.yaml\n", flush=True)
        raise SystemExit(1)
    except yaml.YAMLError as e:
        print(f"\n[ERROR] config.yaml 格式错误，请检查缩进和语法：\n{e}\n", flush=True)
        raise SystemExit(1)

cfg = load_config()
TG_BOT_TOKEN  = cfg["telegram"]["bot_token"]
TG_CHAT_ID    = cfg["telegram"]["chat_id"]
POLL_INTERVAL = cfg.get("poll_interval", 30)
FORWARD_ALL   = cfg.get("forward_all", False)

# OAuth2 回调服务配置
OAUTH_ENABLED     = cfg.get("oauth", {}).get("enabled", False)
OAUTH_CLIENT_ID     = cfg.get("oauth", {}).get("client_id", "7feada80-d946-4d06-b134-73afa3524fb7")
OAUTH_CLIENT_SECRET = cfg.get("oauth", {}).get("client_secret", "")
OAUTH_REDIRECT    = cfg.get("oauth", {}).get("redirect_uri", "https://oa.idays.gq/api/emails/oauth/outlook/callback")
OAUTH_PORT        = cfg.get("oauth", {}).get("port", 8080)

# Gmail Push 配置
GMAIL_CLIENT_ID     = cfg.get("gmail_push", {}).get("client_id", "")
GMAIL_CLIENT_SECRET = cfg.get("gmail_push", {}).get("client_secret", "")
GMAIL_PUBSUB_TOPIC  = cfg.get("gmail_push", {}).get("pubsub_topic", "")
GMAIL_PUSH_ENABLED  = bool(GMAIL_CLIENT_ID and GMAIL_PUBSUB_TOPIC)
GMAIL_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GMAIL_AUTH_URL      = "https://accounts.google.com/o/oauth2/v2/auth"

# 全局接收模式：push（默认）或 idle
# push  → Gmail/Outlook 优先使用 Push，缺少配置时 Telegram 警告并降级
# idle  → 强制 IMAP IDLE / Graph API 轮询，不启动任何 Push
GLOBAL_MODE = cfg.get("mode", "push").lower()

STARTUP_TIME = datetime.now(timezone.utc)  # 启动时间，用于过滤历史邮件
CODE_RE = re.compile(r'\b\d{6}\b')
# GitHub 格式：XXXX-XXXX（字母数字，带连字符）
_CODE_HYPHEN_RE = re.compile(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', re.IGNORECASE)
# 验证码上下文关键词（不跨行，捕获组必须含数字，支持带连字符格式）
_CODE_CONTEXT_RE = re.compile(
    r'(?:验证码|动态码|校验码|确认码|激活码|authorization code|verification code|'
    r'confirm(?:ation)? code|security code|one.time|OTP|passcode|access code|'
    r'authentication code|auth(?:entication)?\s+code|sign.in code|login code|'
    r'your code|the code)'
    r'[^\n]{0,60}?(?<!\w)([A-Z0-9]{4}-[A-Z0-9]{4}|[A-Z]*\d[A-Z0-9]{3,7})\b',
    re.IGNORECASE
)
# 备用：验证码在冒号/是后面（不用裸 code 避免匹配 CSS 类名）
_CODE_COLON_RE = re.compile(
    r'(?:验证码|动态码|校验码|OTP|passcode|one.time.password)[^\w\n]{0,10}'
    r'([A-Z0-9]{4}-[A-Z0-9]{4}|[A-Z0-9]{4,8})\b',
    re.IGNORECASE
)

def find_code(text: str) -> str | None:
    if not text:
        return None
    # 如果输入是 HTML，先转纯文本
    if "<" in text and ">" in text:
        text = html_to_text(text)
    # 优先：上下文匹配
    for pattern in (_CODE_CONTEXT_RE, _CODE_COLON_RE):
        for m in pattern.finditer(text):
            c = m.group(1).upper()
            if len(set(c.replace('-', ''))) == 1:  # 排除全同字符
                continue
            if c in ("123456", "654321", "000000"):
                continue
            # 字母数字混合时，必须含至少1个数字（排除纯单词）
            digits = sum(ch.isdigit() for ch in c)
            if not c.isdigit() and digits == 0:
                continue
            return c
    # 降级1：XXXX-XXXX 格式（GitHub 等，要求含字母且含数字，避免纯字母/纯数字）
    for m in _CODE_HYPHEN_RE.finditer(text):
        c = m.group(1).upper()
        if len(set(c.replace('-', ''))) == 1:
            continue
        raw = c.replace('-', '')
        if raw.isdigit() or raw.isalpha():
            continue
        return c
    # 降级1.5：关键词出现后向后扫描多行，匹配到第一个验证码即停止
    _KW_RE = re.compile(
        r'(?:验证码|动态码|OTP|passcode|access code|login code|your code|'
        r'verification code|security code|one.time|auth.*code)',
        re.IGNORECASE
    )
    _CANDIDATE_RE = re.compile(r'([A-Z0-9]{4,8})\b', re.IGNORECASE)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _KW_RE.search(line):
            # 从下一行开始扫描（关键词所在行通常是描述句，干扰项多）
            start = i + 1 if len(line) > 30 else i
            scan_lines = lines[start:min(start + 6, len(lines))]
            # 两轮：先找纯6位数字，再找字母数字混合
            for pure_only in (True, False):
                for scan_line in scan_lines:
                    for m in _CANDIDATE_RE.finditer(scan_line):
                        c = m.group(1).upper()
                        if len(set(c)) == 1:
                            continue
                        if c in ("123456", "654321", "000000"):
                            continue
                        if not c.isdigit() and sum(ch.isdigit() for ch in c) == 0:
                            continue  # 纯字母跳过
                        if c.isdigit() and len(c) < 6:
                            continue  # 纯数字至少6位
                        if pure_only and not c.isdigit():
                            continue  # 第一轮只要纯数字
                        raw = c.replace('-', '')
                        if raw.isdigit() and c[:4] in ("1999","2000","2001","2002","2003","2004","2005","2006","2007","2008","2009","2010","2011","2012","2013","2014","2015","2016","2017","2018","2019","2020","2021","2022","2023","2024","2025","2026","2027"):
                            continue
                        return c
            break
    # 降级2：纯6位数字（要求邮件整体含验证码相关词汇才触发）
    if not re.search(r'验证|校验|确认码|激活码|动态码|verify|verification code|confirm.*code|code.*confirm|OTP|passcode|one.time|auth.*code|code.*auth', text, re.IGNORECASE):
        return None
    # 交易类邮件排除（PayPal、银行转账等不含验证码）
    if re.search(r'transaction|transfer|payment|invoice|receipt|order|订单|转账|收款|付款|提交.*金额|金额.*提交', text, re.IGNORECASE):
        return None
    for m in CODE_RE.finditer(text):
        c = m.group()
        if len(set(c)) == 1:
            continue
        if c in ("123456", "654321", "000000", "100000", "200000", "300000",
                 "400000", "500000", "600000", "700000", "800000", "900000"):
            continue
        if c.endswith("0000"):
            continue
        # 排除年份（扩展范围）
        if c[:4] in ("1999", "2000", "2001", "2002", "2003", "2004", "2005",
                     "2006", "2007", "2008", "2009", "2010", "2011", "2012",
                     "2013", "2014", "2015", "2016", "2017", "2018", "2019",
                     "2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027"):
            continue
        return c
    return None

OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_DEFAULT_CLIENT_ID = "7feada80-d946-4d06-b134-73afa3524fb7"

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip_depth = 0  # 用计数器代替布尔，防止嵌套/乱序标签导致状态错乱
    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip_depth += 1
    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in ("p", "br", "div", "tr", "li"):
            self._parts.append("\n")
    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)
    def get_text(self):
        lines = "".join(self._parts).splitlines()
        # 过滤空行和明确的 CSS 键值对泄漏（如 "font-size: 14px"）
        _css_kv_re = re.compile(r'^[a-z\-]+\s*:\s*[a-z0-9#.]+', re.IGNORECASE)
        lines = [l for l in lines if l.strip()
                 and not _css_kv_re.match(l.strip())]
        return re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()

def html_to_text(raw: str) -> str:
    try:
        p = _TextExtractor()
        p.feed(html.unescape(raw))
        return p.get_text()
    except Exception:
        return re.sub(r'<[^>]+>', '', html.unescape(raw)).strip()

def _safe_filename(subject: str, max_len: int = 30) -> str:
    """把主题转成安全的文件名，限制长度避免截断乱码"""
    name = re.sub(r'[\\/:*?"<>|]', '', subject).strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "邮件"

def _esc(text: str) -> str:
    """MarkdownV2 特殊字符转义"""
    for c in r'\_*[]()~`>#+-=|{}.!':
        text = text.replace(c, f'\\{c}')
    return text

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_tg(text: str) -> bool:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                       json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "MarkdownV2"}, timeout=10)
        if r.status_code == 200:
            return True
        log.error(f"TG 推送失败: {r.text}")
        # MarkdownV2 解析失败时降级为纯文本重试
        r2 = httpx.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                        json={"chat_id": TG_CHAT_ID, "text": re.sub(r'[\\`*_\[\]()~>#+=|{}.!\-]', '', text)},
                        timeout=10)
        return r2.status_code == 200
    except Exception as e:
        log.error(f"TG 推送异常: {e}")
        return False

def send_tg_document(filename: str, content: str):
    """发送 HTML 文件附件（无 caption，文字已单独发送）"""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument",
            data={"chat_id": TG_CHAT_ID},
            files={"document": (filename, content.encode("utf-8"), "text/html")},
            timeout=30
        )
        if r.status_code != 200:
            log.error(f"TG 附件推送失败: {r.text}")
    except Exception as e:
        log.error(f"TG 附件推送异常: {e}")

def send_tg_file(filename: str, data: bytes, content_type: str = "application/octet-stream"):
    """发送二进制文件附件到 TG"""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument",
            data={"chat_id": TG_CHAT_ID},
            files={"document": (filename, data, content_type)},
            timeout=60
        )
        if r.status_code != 200:
            log.error(f"TG 文件推送失败: {r.text[:200]}")
    except Exception as e:
        log.error(f"TG 文件推送异常: {e}")

def wrap_html(html_body: str, *, subject: str = "", from_: str = "", to: str = "",
              date: str = "", received: str = "") -> str:
    """在 HTML 邮件顶部注入邮件头信息"""
    def row(label, val):
        return f"<tr><td style='color:#888;white-space:nowrap;padding:2px 12px 2px 0'>{label}</td><td style='word-break:break-all'>{html.escape(val)}</td></tr>" if val else ""
    header = (
        "<div style='font-family:sans-serif;font-size:13px;background:#f5f5f5;color:#333;"
        "border-bottom:2px solid #ddd;padding:12px 16px;margin-bottom:12px'>"
        f"<div style='font-size:15px;font-weight:bold;color:#111;margin-bottom:8px'>{html.escape(subject)}</div>"
        "<table style='border-collapse:collapse'>"
        + row("发件人", from_)
        + row("收件人", to)
        + row("发送时间", date)
        + row("送达时间", received)
        + "</table></div>"
    )
    if "<body" in html_body.lower():
        return re.sub(r'(<body[^>]*>)', r'\1' + header, html_body, count=1, flags=re.IGNORECASE)
    return header + html_body

def _decode_bytes(data: bytes, charset: str | None) -> str:
    """健壮解码：优先用声明编码，失败时依次尝试常见中文编码"""
    charsets = []
    if charset:
        charsets.append(charset.lower().replace("_", "-"))
    charsets += ["utf-8", "gb18030", "gbk", "gb2312", "big5", "latin-1"]
    for enc in charsets:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")

# ── 工具 ──────────────────────────────────────────────────────────────────────
def extract_imap_body(msg) -> tuple[str, str]:
    """返回 (plain_body, html_body)"""
    plain = html_body = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain is None:
                raw = part.get_payload(decode=True)
                if raw:
                    plain = _decode_bytes(raw, part.get_content_charset())
            elif ct == "text/html" and html_body is None:
                raw = part.get_payload(decode=True)
                if raw:
                    html_body = _decode_bytes(raw, part.get_content_charset())
        return plain or html_body or "", html_body or ""
    payload = msg.get_payload(decode=True)
    decoded = _decode_bytes(payload, msg.get_content_charset()) if payload else ""
    ct = msg.get_content_type()
    if "html" in ct:
        return decoded, decoded
    return decoded, ""

def extract_attachments(msg) -> list[dict]:
    """提取邮件附件，返回 [{filename, content_type, data}]"""
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        cd = part.get("Content-Disposition", "")
        if "attachment" not in cd and "inline" not in cd:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        # 解码文件名
        decoded_parts = decode_header(filename)
        filename = "".join(
            _decode_bytes(p, enc) if isinstance(p, bytes) else p
            for p, enc in decoded_parts
        )
        data = part.get_payload(decode=True)
        if data:
            attachments.append({
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": data,
            })
    return attachments

def decode_subject(msg) -> str:
    raw, enc = decode_header(msg.get("Subject", ""))[0]
    return _decode_bytes(raw, enc) if isinstance(raw, bytes) else (raw or "")

def decode_from(msg) -> str:
    parts = decode_header(msg.get("From", ""))
    result = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            result.append(_decode_bytes(raw, enc))
        else:
            result.append(raw or "")
    return "".join(result)

def extract_to_email(msg) -> str:
    """提取实际收件地址（支持 +tag 别名）"""
    to = msg.get("Delivered-To") or msg.get("To", "")
    m = re.search(r'[\w.+%-]+@[\w.-]+', to)
    return m.group(0) if m else ""

def parse_date(msg) -> str:
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

# ── 通用 IMAP 轮询（Gmail / QQ 等应用密码方案）───────────────────────────────
_imap_pool: dict[str, imaplib.IMAP4_SSL] = {}  # email -> 复用连接
_imap_locks: dict[str, threading.Lock] = {}   # email -> 独立锁

def _get_imap(email: str, app_pass: str, host: str) -> imaplib.IMAP4_SSL:
    """获取复用的 IMAP 连接，断开时自动重连（每账号独立锁）"""
    if email not in _imap_locks:
        _imap_locks[email] = threading.Lock()
    with _imap_locks[email]:
        conn = _imap_pool.get(email)
        if conn:
            try:
                conn.noop()
                return conn
            except Exception:
                _imap_pool.pop(email, None)
        conn = imaplib.IMAP4_SSL(host, 993)
        conn.login(email, app_pass)
        _imap_pool[email] = conn
        return conn

def _poll_imap(acc: dict, host: str, skip_existing: bool = False) -> list[dict]:
    results = []
    try:
        imap = _get_imap(acc["email"], acc["app_pass"], host)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        for uid in data[0].split():
            _, raw = imap.fetch(uid, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg = email_lib.message_from_bytes(raw[0][1])
            subject = decode_subject(msg)
            body, html_body = extract_imap_body(msg)
            date    = parse_date(msg)
            code    = find_code(body) or find_code(subject)
            if skip_existing:
                imap.store(uid, "+FLAGS", "\\Seen")
                continue
            # 跳过启动前的历史邮件
            try:
                msg_dt = parsedate_to_datetime(msg.get("Date", "")).astimezone(timezone.utc).replace(tzinfo=timezone.utc)
                if msg_dt < STARTUP_TIME:
                    imap.store(uid, "+FLAGS", "\\Seen")
                    continue
            except Exception:
                pass
            to_addr = extract_to_email(msg) or acc.get("label", acc["email"])
            if code or FORWARD_ALL:
                results.append({"label": to_addr, "subject": subject,
                                 "from": decode_from(msg), "to": to_addr,
                                 "received": parse_date(msg),
                                 "code": code, "body": body,
                                 "html_body": html_body, "date": date})
            imap.store(uid, "+FLAGS", "\\Seen")
    except Exception as e:
        log.error(f"[IMAP:{acc['email']}] {e}")
        _imap_pool.pop(acc["email"], None)  # 出错时清除连接，下次重连
    return results

# ── Gmail（应用专用密码）─────────────────────────────────────────────────────
def poll_gmail(acc: dict, skip_existing: bool = False) -> list[dict]:
    return _poll_imap(acc, "imap.gmail.com", skip_existing=skip_existing)

# ── 通用 IMAP IDLE（QQ / 163 / 126 等应用密码方案）─────────────────────────
# 域名 → IMAP host 映射（others 类型自动推断，也可在 config 里用 imap_host 手动指定）
_IMAP_HOST_MAP = {
    "qq.com":      "imap.qq.com",
    "foxmail.com": "imap.qq.com",
    "gmail.com":   "imap.gmail.com",
    "163.com":     "imap.163.com",
    "126.com":     "imap.126.com",
    "yeah.net":    "imap.yeah.net",
    "189.cn":      "imap.189.cn",
    "sina.com":    "imap.sina.com",
    "sina.cn":     "imap.sina.com",
    "139.com":     "imap.139.com",
    "sohu.com":    "imap.sohu.com",
    "aliyun.com":  "imap.aliyun.com",
}

def _get_imap_host(acc: dict) -> str | None:
    """从账号配置或邮件域名推断 IMAP host"""
    if acc.get("imap_host"):
        return acc["imap_host"]
    domain = acc.get("email", "").split("@")[-1].lower()
    return _IMAP_HOST_MAP.get(domain)

_imap_idle_threads: set[str] = set()  # 已启动 IDLE 的账号（qq + others）

def _imap_idle_worker(acc: dict, host: str):
    """通用 IMAP IDLE 长连接，支持 QQ / 163 / 126 等所有应用密码邮箱"""
    email = acc["email"]
    app_pass = acc.get("app_pass")
    if not app_pass:
        log.error(f"[IMAP IDLE] {email} 缺少 app_pass，跳过")
        return
    label = acc.get("label", email)
    tag = acc.get("type", "imap").upper()
    log.info(f"[{tag} IDLE] {email} 启动 IDLE 监听 ({host})")
    _login_fail_alerted = False
    _seen_uids: set[bytes] = set()
    _consecutive_fails = 0

    while True:
        try:
            _ssl_ctx = ssl.create_default_context()
            _ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            _ssl_ctx.check_hostname = False
            _ssl_ctx.verify_mode = ssl.CERT_NONE
            imap = imaplib.IMAP4_SSL(host, 993, ssl_context=_ssl_ctx, timeout=30)
            imap.login(email, app_pass)
            status, _ = imap.select("INBOX")
            if status != "OK":
                err_msg = str(_)
                if "frequent" in err_msg.lower() or "reject" in err_msg.lower():
                    log.warning(f"[{tag} IDLE] {email} 频率限制，等待 5 分钟")
                    time.sleep(300)
                raise RuntimeError(f"SELECT INBOX failed: {status} {_}")
            _consecutive_fails = 0

            # 检查服务器是否支持 IDLE
            _, caps = imap.capability()
            supports_idle = b"IDLE" in (caps[0] if caps else b"")

            # 先处理已有未读
            _, data = imap.search(None, "UNSEEN")
            for uid in data[0].split():
                if uid not in _seen_uids:
                    _seen_uids.add(uid)
                    _process_imap_uid(imap, uid, acc, label)

            if not supports_idle:
                # 降级：每 30 秒轮询一次
                log.warning(f"[{tag} IDLE] {email} 服务器不支持 IDLE，降级为轮询（30s）")
                while True:
                    imap.noop()
                    import time as _t; _t.sleep(30)
                    _, data = imap.search(None, "UNSEEN")
                    for uid in data[0].split():
                        if uid not in _seen_uids:
                            _seen_uids.add(uid)
                            _process_imap_uid(imap, uid, acc, label)
                # 永不到达，连接断开时由外层 except 捕获并重连

            # 进入 IDLE 循环
            while True:
                imap.socket().settimeout(360)
                imap.send(b"IDLE\r\n")
                imap.readline()  # 等待 "+ idling" 响应

                try:
                    line = imap.readline()
                    if b"EXISTS" in line or b"RECENT" in line:
                        imap.send(b"DONE\r\n")
                        imap.readline()
                        _, data = imap.search(None, "UNSEEN")
                        for uid in data[0].split():
                            if uid not in _seen_uids:
                                _seen_uids.add(uid)
                                _process_imap_uid(imap, uid, acc, label)
                    else:
                        imap.send(b"DONE\r\n")
                        imap.readline()
                except (TimeoutError, OSError):
                    try:
                        imap.socket().settimeout(10)
                        imap.send(b"DONE\r\n")
                        imap.readline()
                    except Exception:
                        break
                    try:
                        imap.noop()
                    except Exception:
                        break

        except Exception as e:
            err = str(e)
            if "timed out" in err.lower():
                log.debug(f"[{tag} IDLE] {email} IDLE 超时，重新连接")
                time.sleep(1)
                continue
            else:
                log.error(f"[{tag} IDLE] {email} 连接断开: {e}")
            if "Login fail" in err or "Authentication failed" in err or "Invalid credentials" in err:
                if not _login_fail_alerted:
                    _login_fail_alerted = True
                    send_tg(f"⚠️ {tag}邮箱账号失效：`{_esc(email)}`\n请重新生成授权码并更新配置")
                wait = 3600
            else:
                _login_fail_alerted = False
                _consecutive_fails += 1
                wait = min(15 * (2 ** (_consecutive_fails - 1)), 300)
                if _consecutive_fails == 5:
                    send_tg(f"⚠️ {tag}邮箱连接异常：`{_esc(email)}`\n已连续失败 {_consecutive_fails} 次，等待重连中\n错误：{_esc(err[:80])}")
            time.sleep(wait)


def _process_imap_uid(imap, uid: bytes, acc: dict, label: str):
    """处理单封 IMAP 邮件"""
    try:
        _, raw = imap.fetch(uid, "(RFC822)")
        if not raw or not raw[0]:
            return
        msg = email_lib.message_from_bytes(raw[0][1])
        subject = decode_subject(msg)
        body, html_body = extract_imap_body(msg)
        attachments = extract_attachments(msg)
        date = parse_date(msg)
        to_addr = extract_to_email(msg) or label
        code = find_code(body) or find_code(subject)
        imap.store(uid, "+FLAGS", "\\Seen")

        # 跳过启动前的历史邮件
        try:
            msg_dt = parsedate_to_datetime(msg.get("Date", "")).astimezone(timezone.utc).replace(tzinfo=timezone.utc)
            if msg_dt < STARTUP_TIME:
                return
        except Exception:
            pass

        if not (code or FORWARD_ALL):
            return

        plain = html_to_text(body)
        sender = decode_from(msg)

        # 在 HTML 末尾追加附件列表
        att_html = ""
        if attachments:
            att_html = "<hr><div style='font-size:13px;color:#555;padding:8px 0'><b>📎 附件：</b><ul>"
            for att in attachments:
                size_kb = len(att["data"]) / 1024
                att_html += f"<li>{html.escape(att['filename'])} ({size_kb:.0f} KB)</li>"
            att_html += "</ul></div>"

        attach_html = (html_body or f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{html.escape(plain)}</pre>") + att_html

        if code:
            text = (f"`{code}`\n\n"
                    f">{_esc('📬')} *{_esc(to_addr)}*\n"
                    f">{_esc('发件人')}: {_esc(sender)}\n"
                    f">{_esc('时间')}: {_esc(date)}\n"
                    f">{_esc('主题')}: {_esc(subject)}")
            log.info(f"[IMAP IDLE:{to_addr}] 验证码: {code}")
            if send_tg(text) and FORWARD_ALL:
                send_tg_document(f"{_safe_filename(subject)}.html",
                                 wrap_html(attach_html, subject=subject, from_=sender, to=to_addr, date=date))
                for att in attachments:
                    send_tg_file(att["filename"], att["data"], att["content_type"])
        elif FORWARD_ALL:
            header = (f">{_esc('📩')} *{_esc(to_addr)}*\n"
                      f">{_esc('发件人')}: {_esc(sender)}\n"
                      f">{_esc('时间')}: {_esc(date)}\n"
                      f">{_esc('主题')}: {_esc(subject)}")
            log.info(f"[IMAP IDLE:{to_addr}] 转发邮件: {subject}")
            if send_tg(header):
                send_tg_document(f"{_safe_filename(subject)}.html",
                                 wrap_html(attach_html, subject=subject, from_=sender, to=to_addr, date=date))
                for att in attachments:
                    send_tg_file(att["filename"], att["data"], att["content_type"])
    except Exception as e:
        log.error(f"[IMAP IDLE] 处理邮件失败: {e}")


def poll_imap_idle(acc: dict, skip_existing: bool = False) -> list[dict]:
    """QQ / others 邮箱：启动 IDLE 线程，不走轮询"""
    email = acc["email"]
    if email not in _imap_idle_threads:
        host = _get_imap_host(acc)
        if not host:
            log.error(f"[IMAP IDLE] {email} 无法推断 IMAP host，请在 config 中设置 imap_host")
            return []
        _imap_idle_threads.add(email)
        threading.Thread(target=_imap_idle_worker, args=(acc, host), daemon=True).start()
        log.info(f"[IMAP IDLE] {email} IDLE 线程已启动")
    return []

# 向下兼容别名
poll_qq = poll_imap_idle

# ── Outlook（OAuth2，Graph + IMAP fallback）──────────────────────────────────
_outlook_tokens: dict[str, dict] = {}  # email -> {access_token, expiry, token_type}
_token_fail_alerted: set[str] = set()  # 已推送过失效通知的账号
_gmail_fail_alerted: set[str] = set()  # Gmail token 失效已通知的账号
_processed_msg_ids: set[str] = set()  # 已处理的 Graph message id

def _outlook_refresh(acc: dict) -> dict:
    client_id = acc.get("client_id") or OAUTH_CLIENT_ID or OUTLOOK_DEFAULT_CLIENT_ID
    for scope in [
        "https://graph.microsoft.com/.default offline_access",
        "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    ]:
        payload = {
            "client_id": client_id, "grant_type": "refresh_token",
            "refresh_token": acc["refresh_token"], "scope": scope,
        }
        if OAUTH_CLIENT_SECRET:
            payload["client_secret"] = OAUTH_CLIENT_SECRET
        r = httpx.post(OUTLOOK_TOKEN_URL, data=payload, timeout=15)
        d = r.json()
        if r.status_code == 200 and "access_token" in d:
            returned = d.get("scope", "").lower()
            token_type = "imap" if "imap" in returned else "graph"
            # 更新 refresh_token（如果有新的）
            if d.get("refresh_token"):
                acc["refresh_token"] = d["refresh_token"]
            return {"access_token": d["access_token"],
                    "expiry": time.time() + d.get("expires_in", 3600) - 60,
                    "token_type": token_type}
    raise RuntimeError(f"Outlook token 刷新失败: {acc['email']}")

def _outlook_get_token(acc: dict) -> tuple[str, str]:
    email = acc["email"]
    cached = _outlook_tokens.get(email)
    if not cached or time.time() >= cached["expiry"]:
        _outlook_tokens[email] = _outlook_refresh(acc)
    t = _outlook_tokens[email]
    return t["access_token"], t["token_type"]

def poll_outlook(acc: dict, skip_existing: bool = False) -> list[dict]:
    """acc: {email, refresh_token, client_id(可选), label(可选)}"""
    results = []
    email = acc["email"]
    # 已确认失效的账号跳过轮询，避免日志刷屏
    if email in _token_fail_alerted:
        return results
    try:
        token, token_type = _outlook_get_token(acc)
        _token_fail_alerted.discard(email)
        label = acc.get("label", email)
        if token_type == "imap":
            results = _outlook_imap(acc, token, label, skip_existing=skip_existing)
        else:
            results = _outlook_graph(acc, token, label, skip_existing=skip_existing)
    except Exception as e:
        log.error(f"[Outlook:{email}] {e}")
        if email not in _token_fail_alerted:
            _token_fail_alerted.add(email)
            send_tg(f"⚠️ Outlook 账号失效：`{_esc(email)}`\n请重新授权：https://oa\\.idays\\.gq/auth/outlook")
    return results

def _outlook_graph(acc: dict, token: str, label: str, skip_existing: bool = False) -> list[dict]:
    results = []
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get("https://graph.microsoft.com/v1.0/me/messages",
                  params={"$filter": "isRead eq false", "$select": "id,subject,from,toRecipients,body,receivedDateTime",
                          "$top": 10, "$orderby": "receivedDateTime desc"},
                  headers=headers, timeout=15)
    if r.status_code != 200:
        log.error(f"[Outlook Graph:{acc['email']}] {r.status_code} {r.text[:200]}")
        return results
    for msg in r.json().get("value", []):
        msg_id = msg.get("id", "")
        if msg_id in _processed_msg_ids:
            continue
        subject = msg.get("subject", "")
        sender  = msg.get("from", {}).get("emailAddress", {}).get("address", "")
        body    = msg.get("body", {}).get("content", "")
        raw_dt  = msg.get("receivedDateTime", "")
        try:
            date = datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
            received_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        except Exception:
            date = raw_dt[:16]
            received_dt = None
        # 跳过启动前的历史邮件
        if received_dt and received_dt < STARTUP_TIME:
            try:
                httpx.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}",
                            json={"isRead": True}, headers=headers, timeout=3)
            except Exception:
                pass
            _processed_msg_ids.add(msg_id)
            continue
        code    = find_code(body) or find_code(subject)
        to_addr = next((rc["emailAddress"]["address"] for rc in msg.get("toRecipients", [])
                        if rc.get("emailAddress", {}).get("address")), label)
        if not skip_existing and (code or FORWARD_ALL):
            results.append({"label": to_addr, "subject": subject, "from": sender, "code": code, "body": body, "date": date})
        try:
            httpx.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}",
                        json={"isRead": True}, headers=headers, timeout=3)
        except Exception:
            pass
        _processed_msg_ids.add(msg_id)
    return results

def _outlook_imap(acc: dict, token: str, label: str, skip_existing: bool = False) -> list[dict]:
    results = []
    auth_str = f"user={acc['email']}\x01auth=Bearer {token}\x01\x01"
    try:
        imap = imaplib.IMAP4_SSL("outlook.office365.com", 993)
        imap.authenticate("XOAUTH2", lambda _: auth_str.encode("ascii"))
        for folder in ["INBOX", "Junk"]:
            if imap.select(folder)[0] != "OK":
                continue
            _, data = imap.search(None, "UNSEEN")
            for uid in data[0].split():
                _, raw = imap.fetch(uid, "(RFC822)")
                if not raw or not raw[0]:
                    continue
                msg = email_lib.message_from_bytes(raw[0][1])
                subject = decode_subject(msg)
                body, html_body = extract_imap_body(msg)
                date    = parse_date(msg)
                code    = find_code(body) or find_code(subject)
                if skip_existing:
                    imap.store(uid, "+FLAGS", "\\Seen")
                    continue
                # 跳过启动前的历史邮件
                try:
                    msg_dt = parsedate_to_datetime(msg.get("Date", "")).astimezone(timezone.utc).replace(tzinfo=timezone.utc)
                    if msg_dt < STARTUP_TIME:
                        imap.store(uid, "+FLAGS", "\\Seen")
                        continue
                except Exception:
                    pass
                if code or FORWARD_ALL:
                    results.append({"label": label, "subject": subject,
                                    "from": decode_from(msg), "code": code, "body": body,
                                    "html_body": html_body, "date": date})
                imap.store(uid, "+FLAGS", "\\Seen")
        imap.logout()
    except Exception as e:
        log.error(f"[Outlook IMAP:{acc['email']}] {e}")
    return results

# ── Outlook Change Notifications ─────────────────────────────────────────────
_outlook_subscriptions: dict[str, str] = {}  # email -> subscription_id
OUTLOOK_PUSH_CALLBACK = OAUTH_REDIRECT.replace("/api/emails/oauth/outlook/callback", "/api/outlook/push")

def _outlook_subscribe(acc: dict):
    """注册 Outlook Change Notification 订阅，有效期 3 天"""
    email = acc["email"]
    try:
        token, _ = _outlook_get_token(acc)
        # 先删旧订阅
        old_sub = _outlook_subscriptions.get(email)
        if old_sub:
            try:
                httpx.delete(f"https://graph.microsoft.com/v1.0/subscriptions/{old_sub}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
            except Exception:
                pass

        expiry = (datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
        # 3 天后过期
        expiry_dt = datetime.now(timezone.utc) + timedelta(days=2, hours=23)
        expiry = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        r = httpx.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "changeType": "created",
                "notificationUrl": OUTLOOK_PUSH_CALLBACK,
                "resource": "me/mailFolders('Inbox')/messages",
                "expirationDateTime": expiry,
                "clientState": email,
            },
            timeout=15
        )
        if r.status_code in (200, 201):
            sub_id = r.json().get("id")
            _outlook_subscriptions[email] = sub_id
            log.info(f"[Outlook Push] {email} 订阅成功，到期: {expiry}")
        else:
            log.error(f"[Outlook Push] {email} 订阅失败: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"[Outlook Push] {email} 订阅异常: {e}")

def _process_outlook_push(data: dict):
    """处理 Microsoft Graph Change Notification"""
    try:
        for notification in data.get("value", []):
            email = notification.get("clientState", "")
            resource = notification.get("resourceData", {})
            msg_id = resource.get("id", "")
            # resourceData 可能为空，从 resource 字段解析消息 ID
            if not msg_id:
                res_path = notification.get("resource", "")
                # 格式: me/messages('AAMk...') 或 me/mailFolders('Inbox')/messages('AAMk...')
                m = re.search(r"messages\('([^']+)'\)", res_path)
                if m:
                    msg_id = m.group(1)
            if not email or not msg_id or msg_id in _processed_msg_ids:
                continue
            _processed_msg_ids.add(msg_id)

            # 找到对应账号
            acc = next((a for a in _outlook_accounts if a.get("email") == email), None)
            if not acc:
                continue

            token, _ = _outlook_get_token(acc)
            r = httpx.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "subject,from,body,receivedDateTime,isRead"},
                timeout=10
            )
            if r.status_code != 200:
                continue
            msg = r.json()
            subject = msg.get("subject", "")
            sender  = msg.get("from", {}).get("emailAddress", {}).get("address", "")
            body    = msg.get("body", {}).get("content", "")
            raw_dt  = msg.get("receivedDateTime", "")
            try:
                received_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                date = received_dt.astimezone().strftime("%Y-%m-%d %H:%M")
                # 跳过启动前的历史邮件
                if received_dt < STARTUP_TIME:
                    continue
            except Exception:
                date = raw_dt[:16]

            label = acc.get("label", email)
            code  = find_code(body) or find_code(subject)
            is_html = "<" in body and ">" in body

            # 标已读
            try:
                httpx.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                            json={"isRead": True},
                            headers={"Authorization": f"Bearer {token}"}, timeout=3)
            except Exception:
                pass

            plain = html_to_text(body)
            attach_html = body if is_html else f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{html.escape(plain)}</pre>"

            # 获取附件
            attachments = []
            try:
                att_r = httpx.get(f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}/attachments",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=15)
                if att_r.status_code == 200:
                    for att in att_r.json().get("value", []):
                        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                            continue
                        att_data = base64.b64decode(att.get("contentBytes", ""))
                        if att_data:
                            attachments.append({"filename": att.get("name", "attachment"),
                                                "content_type": att.get("contentType", "application/octet-stream"),
                                                "data": att_data})
            except Exception:
                pass

            if attachments:
                att_section = "<hr><div style='font-size:13px;color:#555;padding:8px 0'><b>📎 附件：</b><ul>"
                for att in attachments:
                    att_section += f"<li>{html.escape(att['filename'])} ({len(att['data'])/1024:.0f} KB)</li>"
                att_section += "</ul></div>"
                attach_html += att_section

            if code or FORWARD_ALL:
                if code:
                    text = (f"`{_esc(code)}`\n\n"
                            f">{_esc('📬')} *{_esc(label)}*\n"
                            f">{_esc('发件人')}: {_esc(sender)}\n"
                            f">{_esc('时间')}: {_esc(date)}\n"
                            f">{_esc('主题')}: {_esc(subject)}")
                    log.info(f"[Outlook Push:{label}] 验证码: {code}")
                    if send_tg(text) and FORWARD_ALL:
                        send_tg_document(f"{_safe_filename(subject)}.html",
                                         wrap_html(attach_html, subject=subject, from_=sender, to=label, date=date))
                        for att in attachments:
                            send_tg_file(att["filename"], att["data"], att["content_type"])
                elif FORWARD_ALL:
                    header = (f">{_esc('📩')} *{_esc(label)}*\n"
                              f">{_esc('发件人')}: {_esc(sender)}\n"
                              f">{_esc('时间')}: {_esc(date)}\n"
                              f">{_esc('主题')}: {_esc(subject)}")
                    log.info(f"[Outlook Push:{label}] 转发邮件: {subject}")
                    if send_tg(header):
                        send_tg_document(f"{_safe_filename(subject)}.html",
                                         wrap_html(attach_html, subject=subject, from_=sender, to=label, date=date))
                        for att in attachments:
                            send_tg_file(att["filename"], att["data"], att["content_type"])
    except Exception as e:
        log.error(f"[Outlook Push] 处理通知异常: {e}")

def _renew_outlook_subscriptions():
    """每 2.5 天自动续期所有 Outlook 订阅"""
    while True:
        time.sleep(int(2.5 * 24 * 3600))
        for acc in _outlook_accounts:
            _outlook_subscribe(acc)

_outlook_accounts: list[dict] = []  # 启动时填充

# ── Gmail Push OAuth ──────────────────────────────────────────────────────────
_gmail_tokens: dict[str, dict] = {}  # email -> {access_token, refresh_token, expiry, label}
_gmail_push_lock = threading.Lock()
_gmail_last_history: dict[str, str] = {}  # email -> last processed historyId

GMAIL_SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify"

def _gmail_refresh_token(email: str) -> str:
    t = _gmail_tokens.get(email)
    if not t:
        raise RuntimeError(f"Gmail token 不存在: {email}")
    if time.time() < t.get("expiry", 0) and t.get("access_token"):
        return t["access_token"]
    r = httpx.post(GMAIL_TOKEN_URL, data={
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": t["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=10)
    d = r.json()
    if "access_token" not in d:
        err = d.get("error", "")
        if err == "invalid_grant" and email not in _gmail_fail_alerted:
            _gmail_fail_alerted.add(email)
            auth_url = OAUTH_REDIRECT.replace("/api/emails/oauth/outlook/callback", "/auth/gmail")
            send_tg(f"⚠️ Gmail token 已失效：`{_esc(email)}`\n原因：refresh\\_token 已过期或被撤销\n请重新授权：{_esc(auth_url)}")
        raise RuntimeError(f"Gmail token 刷新失败: {email} {d}")
    _gmail_fail_alerted.discard(email)  # 刷新成功，清除失效记录
    _gmail_tokens[email]["access_token"] = d["access_token"]
    _gmail_tokens[email]["expiry"] = time.time() + d.get("expires_in", 3600) - 60
    return d["access_token"]

def _gmail_watch(email: str):
    """注册 Gmail Push Watch，有效期 7 天"""
    try:
        token = _gmail_refresh_token(email)
        r = httpx.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/watch",
            headers={"Authorization": f"Bearer {token}"},
            json={"topicName": GMAIL_PUBSUB_TOPIC, "labelIds": ["INBOX"]},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            _gmail_last_history[email] = data.get("historyId", "")
            log.info(f"[Gmail Push] {email} watch 注册成功，到期: {data.get('expiration')}")
        else:
            log.error(f"[Gmail Push] {email} watch 失败: {r.text[:200]}")
    except Exception as e:
        log.error(f"[Gmail Push] {email} watch 异常: {e}")

def _gmail_fetch_message(email: str, msg_id: str) -> dict | None:
    """获取单封邮件内容"""
    try:
        token = _gmail_refresh_token(email)
        r = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        msg = r.json()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        from_   = headers.get("from", "")
        date    = headers.get("date", "")
        try:
            date = parsedate_to_datetime(date).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        # 提取 body（plain 用于验证码提取，html 用于附件）
        plain_body = ""
        html_body = ""
        def extract_parts(parts):
            nonlocal plain_body, html_body
            for p in parts:
                if p.get("mimeType") == "text/plain" and not plain_body:
                    data = p.get("body", {}).get("data", "")
                    if data:
                        plain_body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                elif p.get("mimeType") == "text/html" and not html_body:
                    data = p.get("body", {}).get("data", "")
                    if data:
                        html_body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                if p.get("parts"):
                    extract_parts(p["parts"])

        payload = msg.get("payload", {})
        if payload.get("parts"):
            extract_parts(payload["parts"])
        else:
            data = payload.get("body", {}).get("data", "")
            if data:
                ct = payload.get("mimeType", "")
                decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                if "html" in ct:
                    html_body = decoded
                else:
                    plain_body = decoded

        body = plain_body or html_body

        # 提取附件
        attachments = []
        def extract_att_parts(parts):
            for p in parts:
                filename = p.get("filename")
                if filename and p.get("body", {}).get("attachmentId"):
                    attachments.append({
                        "filename": filename,
                        "content_type": p.get("mimeType", "application/octet-stream"),
                        "attachment_id": p["body"]["attachmentId"],
                        "size": p.get("body", {}).get("size", 0),
                    })
                if p.get("parts"):
                    extract_att_parts(p["parts"])
        if payload.get("parts"):
            extract_att_parts(payload["parts"])

        # 下载附件数据
        for att in attachments:
            try:
                att_r = httpx.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/attachments/{att['attachment_id']}",
                    headers={"Authorization": f"Bearer {token}"}, timeout=30)
                if att_r.status_code == 200:
                    att["data"] = base64.urlsafe_b64decode(att_r.json().get("data", "") + "==")
                else:
                    att["data"] = b""
            except Exception:
                att["data"] = b""
        attachments = [a for a in attachments if a.get("data")]

        # 标为已读
        httpx.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify",
            headers={"Authorization": f"Bearer {token}"},
            json={"removeLabelIds": ["UNREAD"]},
            timeout=5
        )
        return {"subject": subject, "from": from_, "date": date, "body": body, "html_body": html_body, "email": email, "attachments": attachments}
    except Exception as e:
        log.error(f"[Gmail Push] 获取邮件失败 {email}/{msg_id}: {e}")
        return None

def _process_gmail_push(data: dict):
    """处理 Pub/Sub 推送的 Gmail 通知"""
    try:
        msg_data = base64.b64decode(data.get("message", {}).get("data", "")).decode()
        notification = json.loads(msg_data)
        email = notification.get("emailAddress", "")
        history_id = notification.get("historyId")
        if not email or not history_id:
            return

        token = _gmail_refresh_token(email)
        # 用上次记录的 historyId，没有则用推送的 historyId
        start_id = _gmail_last_history.get(email) or str(max(1, int(history_id) - 1))
        r = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/history",
            headers={"Authorization": f"Bearer {token}"},
            params={"startHistoryId": start_id, "historyTypes": "messageAdded", "labelId": "INBOX"},
            timeout=10
        )
        if r.status_code != 200:
            with _gmail_push_lock:
                _gmail_last_history[email] = history_id
            return
        history_data = r.json()
        # 更新 historyId
        with _gmail_push_lock:
            _gmail_last_history[email] = history_data.get("historyId", history_id)
        for record in history_data.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id in _processed_msg_ids:
                    continue
                _processed_msg_ids.add(msg_id)
                item = _gmail_fetch_message(email, msg_id)
                if not item:
                    continue
                # 跳过启动前的历史邮件
                try:
                    item_dt = parsedate_to_datetime(item["date"]).astimezone(timezone.utc).replace(tzinfo=timezone.utc)
                    if item_dt < STARTUP_TIME:
                        continue
                except Exception:
                    pass
                body = item["body"]
                code = find_code(body) or find_code(item["subject"])
                label = _gmail_tokens.get(email, {}).get("label", email)
                html_body = item.get("html_body", "")
                attachments = item.get("attachments", [])
                plain = html_to_text(body)
                attach_html = html_body or f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{html.escape(plain)}</pre>"

                if attachments:
                    att_section = "<hr><div style='font-size:13px;color:#555;padding:8px 0'><b>📎 附件：</b><ul>"
                    for att in attachments:
                        att_section += f"<li>{html.escape(att['filename'])} ({len(att['data'])/1024:.0f} KB)</li>"
                    att_section += "</ul></div>"
                    attach_html += att_section

                if code or FORWARD_ALL:
                    if code:
                        text = (f"`{_esc(code)}`\n\n"
                                f">{_esc('📬')} *{_esc(label)}*\n"
                                f">{_esc('发件人')}: {_esc(item['from'])}\n"
                                f">{_esc('时间')}: {_esc(item['date'])}\n"
                                f">{_esc('主题')}: {_esc(item['subject'])}")
                        log.info(f"[Gmail Push:{label}] 验证码: {code}")
                        if send_tg(text) and FORWARD_ALL:
                            send_tg_document(f"{_safe_filename(item['subject'])}.html",
                                             wrap_html(attach_html, subject=item['subject'], from_=item['from'],
                                                       to=label, date=item['date']))
                            for att in attachments:
                                send_tg_file(att["filename"], att["data"], att["content_type"])
                    elif FORWARD_ALL:
                        header = (f">{_esc('📩')} *{_esc(label)}*\n"
                                  f">{_esc('发件人')}: {_esc(item['from'])}\n"
                                  f">{_esc('时间')}: {_esc(item['date'])}\n"
                                  f">{_esc('主题')}: {_esc(item['subject'])}")
                        log.info(f"[Gmail Push:{label}] 转发邮件: {item['subject']}")
                        if send_tg(header):
                            send_tg_document(f"{_safe_filename(item['subject'])}.html",
                                             wrap_html(attach_html, subject=item['subject'], from_=item['from'],
                                                       to=label, date=item['date']))
                            for att in attachments:
                                send_tg_file(att["filename"], att["data"], att["content_type"])
    except Exception as e:
        log.error(f"[Gmail Push] 处理通知异常: {e}")

def _renew_gmail_watches():
    """每 6 天自动续期所有 Gmail Watch"""
    while True:
        time.sleep(6 * 24 * 3600)
        for email in list(_gmail_tokens.keys()):
            _gmail_watch(email)

# ── OAuth2 回调服务 ───────────────────────────────────────────────────────────
AUTH_URL = (
    f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    f"?client_id={{client_id}}&response_type=code&redirect_uri={{redirect}}"
    f"&scope=https://graph.microsoft.com/Mail.Read%20https://graph.microsoft.com/Mail.ReadWrite%20https://graph.microsoft.com/User.Read%20offline_access&prompt=select_account"
)

class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # 静默 HTTP 日志

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/gmail/push":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            self._respond(200, "ok")
            try:
                data = json.loads(body)
                threading.Thread(target=_process_gmail_push, args=(data,), daemon=True).start()
            except Exception as e:
                log.error(f"[Gmail Push] 解析推送失败: {e}")

        elif parsed.path == "/api/outlook/push":
            params = parse_qs(parsed.query)
            # Microsoft 验证握手
            validation_token = params.get("validationToken", [None])[0]
            if validation_token:
                body_bytes = validation_token.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", len(body_bytes))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            self._respond(202, "")
            try:
                data = json.loads(body)
                threading.Thread(target=_process_outlook_push, args=(data,), daemon=True).start()
            except Exception as e:
                log.error(f"[Outlook Push] 解析推送失败: {e}")
        else:
            self._respond(404, "Not Found")

    def do_GET(self):
        parsed = urlparse(self.path)

        # 授权入口：跳转微软登录
        if parsed.path == "/auth/outlook":
            url = AUTH_URL.format(client_id=OAUTH_CLIENT_ID, redirect=OAUTH_REDIRECT)
            self._redirect(url)

        # 微软回调
        elif parsed.path == "/api/emails/oauth/outlook/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if not code:
                self._respond(400, "缺少 code 参数")
                return
            try:
                rt, email = _exchange_code(code)
                _save_outlook_account(rt, email)
                self._respond(200, f"✅ 授权成功！{email} 已添加，监控将在下一轮询周期生效。")
                send_tg(f"✅ Outlook 账号已授权：`{email}`")
                log.info(f"新 Outlook 账号授权成功：{email}")
            except Exception as e:
                self._respond(500, f"授权失败: {e}")
                log.error(f"OAuth 回调处理失败: {e}")

        # Gmail 授权入口
        elif parsed.path == "/auth/gmail":
            params = parse_qs(parsed.query)
            redirect = f"https://oa.idays.gq/api/gmail/oauth/callback"
            url = (f"{GMAIL_AUTH_URL}?client_id={GMAIL_CLIENT_ID}"
                   f"&redirect_uri={redirect}&response_type=code"
                   f"&scope={GMAIL_SCOPES.replace(' ', '%20')}"
                   f"&access_type=offline&prompt=consent")
            self._redirect(url)

        # Gmail OAuth 回调
        elif parsed.path == "/api/gmail/oauth/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if not code:
                self._respond(400, "缺少 code 参数")
                return
            try:
                redirect = f"https://oa.idays.gq/api/gmail/oauth/callback"
                r = httpx.post(GMAIL_TOKEN_URL, data={
                    "client_id": GMAIL_CLIENT_ID,
                    "client_secret": GMAIL_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect,
                    "grant_type": "authorization_code",
                }, timeout=15)
                d = r.json()
                if "refresh_token" not in d:
                    raise RuntimeError(d.get("error_description", d))
                # 获取邮箱地址
                me = httpx.get("https://www.googleapis.com/gmail/v1/users/me/profile",
                               headers={"Authorization": f"Bearer {d['access_token']}"}, timeout=10)
                email = me.json().get("emailAddress", "")
                # 找到对应账号的 label
                label = email
                for acc in cfg.get("accounts", []):
                    for mb in (acc.get("mailboxes") or []):
                        if mb.get("email") == email:
                            label = mb.get("label", email)
                _gmail_tokens[email] = {
                    "access_token": d["access_token"],
                    "refresh_token": d["refresh_token"],
                    "expiry": time.time() + d.get("expires_in", 3600) - 60,
                    "label": label,
                }
                _save_gmail_token(email, d["refresh_token"])
                _gmail_watch(email)
                self._respond(200, f"✅ Gmail 授权成功！{email} 已启用 Push 监控。")
                send_tg(f"✅ Gmail Push 已启用：`{email}`")
            except Exception as e:
                self._respond(500, f"授权失败: {e}")
                log.error(f"Gmail OAuth 回调失败: {e}")

        # Google Search Console 域名验证
        elif parsed.path == "/google883877c5c8e86eea.html":
            self._respond(200, "google-site-verification: google883877c5c8e86eea.html")

        else:
            self._respond(404, "Not Found")

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _respond(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def _exchange_code(code: str) -> tuple[str, str]:
    """返回 (refresh_token, email)"""
    data = {
        "client_id":    OAUTH_CLIENT_ID,
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": OAUTH_REDIRECT,
        "scope":        "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/User.Read offline_access",
        "token_endpoint_auth_method": "none",
    }
    if OAUTH_CLIENT_SECRET:
        data["client_secret"] = OAUTH_CLIENT_SECRET
        data.pop("token_endpoint_auth_method", None)
    r = httpx.post(OUTLOOK_TOKEN_URL, data=data, timeout=15)
    d = r.json()
    if "refresh_token" not in d:
        raise RuntimeError(d.get("error_description", d))
    # 用 access_token 获取邮箱地址
    email = ""
    try:
        me = httpx.get("https://graph.microsoft.com/v1.0/me",
                       headers={"Authorization": f"Bearer {d['access_token']}"},
                       params={"$select": "mail,userPrincipalName"}, timeout=10)
        me_data = me.json()
        email = me_data.get("mail") or me_data.get("userPrincipalName", "")
    except Exception:
        pass
    return d["refresh_token"], email


def _save_outlook_account(refresh_token: str, email: str):
    """更新已有账号的 token，不存在则追加"""
    with open(CONFIG_FILE) as f:
        content = f.read()

    # 用 yaml 解析判断是否已存在
    data = yaml.safe_load(content)
    exists = False
    for entry in data.get("accounts", []):
        if entry.get("type") == "outlook":
            for mb in entry.get("mailboxes", []):
                if mb.get("email") == email:
                    exists = True
                    break

    if exists:
        # 替换该邮箱对应的 refresh_token（找到 email 行后的第一个 refresh_token）
        import re as _re
        pattern = rf'(email:\s*["\']?{_re.escape(email)}["\']?\s*\n\s+refresh_token:\s*)[^\n]+'
        new_content = _re.sub(pattern, rf'\g<1>"{refresh_token}"', content, count=1)
        with open(CONFIG_FILE, "w") as f:
            f.write(new_content)
        return

    # 不存在则追加
    new_entry = (
        f"      - label: \"{email}\"\n"
        f"        email: \"{email}\"\n"
        f"        refresh_token: \"{refresh_token}\"\n"
    )
    if "type: outlook" in content:
        content = content.rstrip() + "\n" + new_entry
    else:
        content = content.rstrip() + "\n  - type: outlook\n    mailboxes:\n" + new_entry

    with open(CONFIG_FILE, "w") as f:
        f.write(content)


def _save_gmail_token(email: str, refresh_token: str):
    """保存 Gmail refresh_token 到 config.yaml，不破坏原有格式"""
    with open(CONFIG_FILE) as f:
        lines = f.readlines()

    # 先尝试直接替换已有的 gmail_refresh_token 行（在对应 email 块内）
    in_block = False
    new_lines = []
    replaced = False
    for line in lines:
        if re.search(rf'email:\s*["\']?{re.escape(email)}["\']?\s*$', line.rstrip()):
            in_block = True
        elif in_block and re.match(r'\s*-\s+\w', line) and 'email:' not in line:
            in_block = False  # 进入下一个 mailbox 块
        if in_block and not replaced and re.match(r'(\s*)gmail_refresh_token:', line):
            indent = len(line) - len(line.lstrip())
            new_lines.append(f'{" " * indent}gmail_refresh_token: "{refresh_token}"\n')
            replaced = True
            continue
        new_lines.append(line)

    if replaced:
        with open(CONFIG_FILE, "w") as f:
            f.writelines(new_lines)
        return

    # 没有找到已有字段，在 email: 行后插入
    new_lines = []
    inserted = False
    for line in lines:
        new_lines.append(line)
        if not inserted and re.search(rf'email:\s*["\']?{re.escape(email)}["\']?\s*$', line.rstrip()):
            indent = len(line) - len(line.lstrip())
            new_lines.append(f'{" " * indent}gmail_refresh_token: "{refresh_token}"\n')
            inserted = True

    if not inserted:
        content = "".join(new_lines)
        new_entry = (
            f"      - email: \"{email}\"\n"
            f"        label: \"{email}\"\n"
            f"        gmail_refresh_token: \"{refresh_token}\"\n"
        )
        content += ("\n  - type: gmail\n    mailboxes:\n" if "type: gmail" not in content else "") + new_entry
        new_lines = [content]

    with open(CONFIG_FILE, "w") as f:
        f.writelines(new_lines)


def start_oauth_server():
    server = HTTPServer(("0.0.0.0", OAUTH_PORT), OAuthHandler)
    log.info(f"OAuth 回调服务已启动，授权入口: http://0.0.0.0:{OAUTH_PORT}/auth/outlook")
    server.serve_forever()


# ── 主循环 ────────────────────────────────────────────────────────────────────
def main():
    if OAUTH_ENABLED:
        t = threading.Thread(target=start_oauth_server, daemon=True)
        t.start()

    # 支持新格式（按 type 分组）和旧格式（flat list）
    raw = cfg.get("accounts", [])
    accounts = []
    for entry in raw:
        if "mailboxes" in entry:
            for mb in (entry["mailboxes"] or []):
                accounts.append({**mb, "type": entry["type"]})
        else:
            accounts.append(entry)

    # 去重：同邮箱保留最后一条
    seen = {}
    for acc in accounts:
        seen[acc.get("email", "")] = acc
    accounts = list(seen.values())
    log.info(f"加载 {len(accounts)} 个账号")

    def _group(t):
        return "\n".join(f"`{a['email']}`" for a in accounts if a.get("type","").lower()==t and a.get("email"))

    gmail_list   = _group("gmail")
    qq_list      = _group("qq")
    outlook_list = _group("outlook")
    others_list  = _group("others")

    parts = []
    if gmail_list:   parts.append(f"📧 Gmail：\n{gmail_list}")
    if qq_list:      parts.append(f"📧 QQ：\n{qq_list}")
    if outlook_list: parts.append(f"📧 Outlook：\n{outlook_list}")
    if others_list:  parts.append(f"📧 其他邮箱：\n{others_list}")

    auth_url = OAUTH_REDIRECT.replace("/api/emails/oauth/outlook/callback", "/auth/outlook")
    if OAUTH_ENABLED:
        parts.append(f"➕ [Outlook Push 授权]({auth_url})")
    if GMAIL_PUSH_ENABLED:
        parts.append(f"➕ [Gmail Push 授权]({auth_url.replace('/auth/outlook', '/auth/gmail')})")

    def _make_guide(title: str, sections: list[tuple[str, list[str]]]) -> str:
        """生成 expandable blockquote，超过3行自动折叠，code 可复制"""
        def _line(text: str) -> str:
            # 先拆出 `code` 和 [text](url)，其余部分转义
            parts_l = re.split(r'(`[^`]*`|\[[^\]]+\]\([^)]+\))', text)
            result = []
            for p in parts_l:
                if p.startswith('`') or (p.startswith('[') and '](' in p):
                    result.append(p)
                else:
                    result.append(_esc(p))
            return "".join(result)
        lines = [f"*{_esc(title)}*"]
        for sec_title, sec_lines in sections:
            lines.append("")
            lines.append(f"*{_esc(sec_title)}*")
            for l in sec_lines:
                lines.append(_line(l))
        return "**>" + "\n>".join(lines) + "||"

    send_tg(_make_guide("📋 Gmail Push 配置备忘", [
        ("➕ 新增邮箱账号", [
            "第一步：[GCP 添加测试用户](https://console.cloud.google.com/apis/credentials/consent?project=mail-monitor-493615)",
            "第二步：[Gmail Push 授权](https://oa.idays.gq/auth/gmail)",
        ]),
        ("🔧 新建 Pub/Sub（首次或重建）", [
            "1\\. 打开 [Pub/Sub 主题页](https://console.cloud.google.com/cloudpubsub/topic/list?project=mail-monitor-493615) → 创建主题",
            "2\\. 主题 ID：`gmail-push`，取消勾选默认订阅 → 创建",
            "3\\. 进入主题 → 权限 → 添加主账号：`gmail-api-push@system.gserviceaccount.com`，角色：Pub/Sub 发布者",
            "4\\. 打开 [订阅页](https://console.cloud.google.com/cloudpubsub/subscription/list?project=mail-monitor-493615) → 创建订阅",
            "5\\. 订阅 ID：`gmail-push-sub`，主题：`gmail-push`，类型：推送",
            "6\\. 端点：`https://oa.idays.gq/api/gmail/push` → 创建",
        ]),
        ("📋 配置信息", [
            "项目：`mail-monitor-493615`",
            "Topic：`projects/mail-monitor-493615/topics/gmail-push`",
            "客户端 ID：`1081529245632-cvnkkf4clntgsimne1se6khv5u0t0c5j.apps.googleusercontent.com`",
            "回调：`https://oa.idays.gq/api/gmail/oauth/callback`",
        ]),
        ("重装后操作", [
            "1\\. Pub/Sub 订阅无需重建，域名验证永久有效",
            "2\\. 重新授权各账号：[Gmail Push 授权](https://oa.idays.gq/auth/gmail)",
            "3\\. 如积压旧消息：[清除消息](https://console.cloud.google.com/cloudpubsub/subscription/detail/gmail-push-sub?project=mail-monitor-493615) → 完全清除",
        ]),
    ]))

    send_tg(_make_guide("📋 Outlook Push 配置备忘", [
        ("Azure 应用注册", [
            "地址：`portal.azure.com`",
            "应用名：`imail`",
            "应用 ID：`2e6ee5ed-2fb6-454c-8e1b-a5515b78571b`",
        ]),
        ("重定向 URI", [
            "类型：移动和桌面应用程序",
            "地址：`https://oa.idays.gq/api/emails/oauth/outlook/callback`",
        ]),
        ("API 权限", [
            "`Mail.Read` / `Mail.ReadWrite` / `User.Read` / `offline_access`",
            "允许公共客户端流：已启用",
        ]),
        ("Change Notifications 端点", [
            "`https://oa.idays.gq/api/outlook/push`",
            "订阅有效期：3 天，程序自动续期",
        ]),
        ("重装后操作", [
            "1. 点 Outlook Push 授权链接重新授权各账号",
            "2. 授权后自动注册 Change Notifications 订阅",
            "3. client_secret 到期需去 Azure 重新生成并更新 config",
        ]),
    ]))

    send_tg(f"✅ 监控已启动，共 {len(accounts)} 个账号\n\n" + "\n\n".join(parts))

    # 加载已有 Gmail Push token 并注册 watch
    if GLOBAL_MODE == "push" and GMAIL_PUSH_ENABLED:
        for acc in accounts:
            if acc.get("type") == "gmail":
                email = acc["email"]
                if acc.get("gmail_refresh_token"):
                    _gmail_tokens[email] = {
                        "access_token": "",
                        "refresh_token": acc["gmail_refresh_token"],
                        "expiry": 0,
                        "label": acc.get("label", email),
                    }
                    _gmail_watch(email)
                else:
                    send_tg(f"⚠️ Gmail Push 模式：`{_esc(email)}` 缺少 `gmail_refresh_token`，已降级为 IMAP 轮询\n请访问 /auth/gmail 完成授权")
                    log.warning(f"[Gmail] {email} 缺少 gmail_refresh_token，降级为 IMAP 轮询")
        threading.Thread(target=_renew_gmail_watches, daemon=True).start()

    # 注册 Outlook Change Notifications（同步执行，避免与轮询并发）
    if GLOBAL_MODE == "push" and OAUTH_ENABLED:
        outlook_push_accs = [a for a in accounts if a.get("type") == "outlook" and a.get("refresh_token")]
        # 检查缺少 refresh_token 的 Outlook 账号
        for acc in [a for a in accounts if a.get("type") == "outlook" and not a.get("refresh_token")]:
            send_tg(f"⚠️ Outlook Push 模式：`{_esc(acc['email'])}` 缺少 `refresh_token`，已降级为 Graph API 轮询\n请访问 /auth/outlook 完成授权")
            log.warning(f"[Outlook] {acc['email']} 缺少 refresh_token，降级为 Graph API 轮询")
        _outlook_accounts.extend(outlook_push_accs)
        for acc in outlook_push_accs:
            _outlook_subscribe(acc)  # 同步执行，确保订阅完成后再计算 poll_accounts
        if outlook_push_accs:
            threading.Thread(target=_renew_outlook_subscriptions, daemon=True).start()

    # 启动 IMAP IDLE 线程（QQ / others，不受 GLOBAL_MODE 影响）
    for acc in accounts:
        if acc.get("type", "").lower() in ("qq", "others"):
            poll_imap_idle(acc)

    # 判断是否有需要轮询的账号
    def _needs_poll(acc):
        t = acc.get("type", "").lower()
        if t in ("qq", "others"):
            return False  # 统一用 IDLE
        if GLOBAL_MODE == "idle":
            return True   # 强制 idle 模式，全部走轮询
        # push 模式下：已成功订阅/注册 watch 的账号不需要轮询
        if t == "gmail":
            return acc["email"] not in _gmail_tokens  # 没有 token = 未完成 Push 授权，走轮询
        if t == "outlook":
            return acc.get("email") not in _outlook_subscriptions  # 订阅失败时降级轮询
        return True

    poll_accounts = [a for a in accounts if _needs_poll(a)]
    if not poll_accounts:
        log.info("所有账号已使用 Push/IDLE，轮询循环已跳过")
        threading.Event().wait()  # 永久阻塞，保持进程运行
        return

    first_run = True
    while True:
        _skip = first_run
        def poll_one(acc, skip=_skip):
            t = acc.get("type", "").lower()
            try:
                if t == "gmail":
                    if GLOBAL_MODE == "push" and acc["email"] in _gmail_tokens:
                        return []
                    return poll_gmail(acc, skip_existing=skip)
                elif t in ("qq", "others"):
                    return poll_imap_idle(acc, skip_existing=skip)
                elif t == "outlook":
                    if GLOBAL_MODE == "push" and acc.get("email") in _outlook_subscriptions:
                        return []
                    return poll_outlook(acc, skip_existing=skip)
            except Exception as e:
                log.error(f"[{acc.get('email')}] {e}")
            return []

        all_items = []
        with ThreadPoolExecutor(max_workers=min(len(poll_accounts), 10)) as ex:
            futures = {ex.submit(poll_one, acc): acc for acc in poll_accounts}
            for f in as_completed(futures):
                all_items.extend(f.result() or [])

        for item in all_items:
            body_raw = item.get("body", "")
            html_body = item.get("html_body", "")
            plain = html_to_text(body_raw)
            # 没有 HTML 时把纯文本包成简单 HTML
            attach_html = html_body or f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{html.escape(plain)}</pre>"

            def _meta(item):
                return (f">{_esc('发件人')}: {_esc(item['from'])}\n"
                        f">{_esc('时间')}: {_esc(item.get('date', ''))}\n"
                        f">{_esc('主题')}: {_esc(item['subject'])}")

            def _send_attach(item, content):
                send_tg_document(f"{_safe_filename(item['subject'])}.html",
                                 wrap_html(content, subject=item['subject'], from_=item['from'],
                                           to=item.get('to', item['label']), date=item.get('date', ''),
                                           received=item.get('received', '')))

            if item.get("code"):
                text = (f"`{item['code']}`\n\n"
                        f">{_esc('📬')} *{_esc(item['label'])}*\n"
                        + _meta(item))
                log.info(f"[{item['label']}] 验证码: {item['code']}")
                if send_tg(text) and FORWARD_ALL:
                    _send_attach(item, attach_html)
            elif FORWARD_ALL:
                header = (f">{_esc('📩')} *{_esc(item['label'])}*\n" + _meta(item))
                log.info(f"[{item['label']}] 转发邮件: {item['subject']}")
                if send_tg(header):
                    _send_attach(item, attach_html)
        first_run = False
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
