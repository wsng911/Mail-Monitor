# Mail-Monitor

监控 Gmail / QQ邮箱 / Outlook 收件箱，收到验证码自动推送 Telegram，支持转发完整邮件（HTML附件）。

| 邮箱 | 获取方式 | 延迟 | 认证方式 |
|------|---------|:----:|---------|
| Gmail | IMAP IDLE | ~1-3 秒 | 应用专用密码 |
| QQ邮箱 | IMAP IDLE | ~1-3 秒 | 授权码 |
| Gmail | Pub/Sub Push | ~1-5 秒 | OAuth2 |
| Outlook | Change Notifications Push | ~2-10 秒 | OAuth2 + Azure 应用 |
| Outlook | Graph API 轮询 | ~30 秒（可配置） | OAuth2 refresh_token |

Docker Hub: [wsng911/mail-monitor](https://hub.docker.com/r/wsng911/mail-monitor) · 当前版本：`v1.4`

---

## 快速部署

```bash
mkdir -p /home/mail-monitor/config && cd /home/mail-monitor
nano config/config.yaml
docker compose up -d
docker compose logs -f
```

`docker-compose.yml`：

```yaml
services:
  mail-monitor:
    image: wsng911/mail-monitor:v1
    container_name: mail-monitor
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
    ports:
      - "8080:8080"     # Outlook OAuth 回调 + Change Notifications
    volumes:
      - ./config:/config
```

> `latest` 与 `v1` 为同一镜像。

---

## config.yaml 完整示例

```yaml
telegram:
  bot_token: "your_bot_token"
  chat_id: "your_chat_id"

poll_interval: 30       # 轮询间隔（秒），Push/IDLE 账号不受此影响
forward_all: false      # true = 转发所有邮件+HTML附件；false = 只推验证码

# 全局接收模式（仅影响 Gmail 和 Outlook，QQ 永远使用 IDLE）
# push：优先使用 Push，账号缺少授权时发 Telegram 警告并降级
# idle：强制 IMAP IDLE / Graph API 轮询，不启动任何 Push
mode: push

# Outlook OAuth 回调服务（用于一键授权 + Change Notifications Push）
oauth:
  enabled: true
  client_id: "your_azure_client_id"
  client_secret: "your_azure_client_secret"
  redirect_uri: "https://your-domain.com/api/emails/oauth/outlook/callback"
  port: 8080

# Gmail Push 配置（可选，不填则使用 IMAP IDLE）
gmail_push:
  client_id: "your_google_client_id"
  client_secret: "your_google_client_secret"
  pubsub_topic: "projects/your-project/topics/gmail-push"

accounts:
  - type: gmail
    mailboxes:
      - label: 我的Gmail
        email: you@gmail.com
        app_pass: "xxxx xxxx xxxx xxxx"
        gmail_refresh_token: ""   # Gmail Push 授权后自动填入

  - type: qq
    mailboxes:
      - label: 我的QQ邮箱
        email: 123456@qq.com
        app_pass: "xxxxxxxxxxxxxxxx"

  - type: outlook
    mailboxes:
      - label: 我的Outlook
        email: you@hotmail.com
        refresh_token: "0.AXXX..."
        client_id: ""
```

---

## Telegram 配置

**获取 bot_token：**
1. Telegram 搜索 `@BotFather` → `/newbot` → 按提示创建
2. 创建完成后获得 `bot_token`

**获取 chat_id：**
1. 给你的 bot 发任意一条消息
2. 访问以下地址，在返回 JSON 里找 `message.chat.id`：
```
https://api.telegram.org/bot<你的bot_token>/getUpdates
```

---

## Gmail 配置

### 第一步：开启 IMAP + 生成应用专用密码

> 需要开启两步验证

1. 打开 Gmail → 右上角齿轮 → 查看所有设置 → 「转发和 POP/IMAP」→ 启用 IMAP → 保存
2. 打开 [应用专用密码页面](https://myaccount.google.com/apppasswords) → 选择「邮件」→ 生成，复制 16 位密码

```yaml
- type: gmail
  mailboxes:
    - label: 我的Gmail
      email: you@gmail.com
      app_pass: "xxxx xxxx xxxx xxxx"
```

### 第二步（可选）：配置 Pub/Sub Push 实现实时推送

**1. Google Cloud 控制台**
1. 打开 [Google Cloud Console](https://console.cloud.google.com)，创建项目
2. 启用 **Gmail API** 和 **Cloud Pub/Sub API**

**2. 创建 Pub/Sub Topic**
1. 搜索「Pub/Sub」→ 主题 → 创建主题，名称如 `gmail-push`
2. 添加发布者：成员填 `gmail-api-push@system.gserviceaccount.com`，角色选 `Pub/Sub 发布者`

**3. 创建推送订阅**
1. 订阅 → 创建订阅，类型选「推送」
2. 端点填：`https://your-domain.com/api/gmail/push`

**4. 创建 OAuth 客户端**
1. 「API 和服务」→「凭据」→ 创建 OAuth 客户端 ID，类型选「Web 应用」
2. 授权重定向 URI 填：`https://your-domain.com/api/gmail/oauth/callback`

**5. 填写配置并授权**
```yaml
gmail_push:
  client_id: "your_google_client_id"
  client_secret: "your_google_client_secret"
  pubsub_topic: "projects/your-project/topics/gmail-push"
```
启动容器后访问 `https://your-domain.com/auth/gmail` 完成授权。

> Gmail Watch 有效期 7 天，程序自动续期。

---

## 其他国内邮箱（163 / 126 / 189 / 新浪 / 阿里云等）

使用 `type: others`，程序自动根据邮件域名推断 IMAP 服务器：

| 域名 | IMAP Host | 海外服务器支持 | 备注 |
|------|-----------|:---:|------|
| 163.com | imap.163.com | ❌ | 海外 IP 被封禁，无法使用 |
| 126.com | imap.126.com | ❌ | 同 163，海外 IP 限制 |
| yeah.net | imap.yeah.net | ❌ | 同 163，海外 IP 限制 |
| 189.cn | imap.189.cn | ✅ | 正常 |
| sina.com / sina.cn | imap.sina.com | ⚠️ | 连接不稳定，会频繁重连 |
| 139.com | imap.139.com | ✅ | 需宽松 SSL，已内置支持 |
| sohu.com | imap.sohu.com | ⚠️ | 未充分测试 |
| aliyun.com | imap.aliyun.com | ✅ | 正常 |

```yaml
- type: others
  mailboxes:
    - label: 我的189
      email: user@189.cn
      app_pass: "your_auth_code"
    - label: 自定义
      email: user@custom.com
      app_pass: "your_auth_code"
      imap_host: "imap.custom.com"   # 域名不在列表时手动指定
```

> 163 / 126 / yeah.net 邮箱对海外 IP 有严格封禁，即使授权码正确也无法在境外服务器上使用，建议改用 189.cn 或 aliyun.com。

使用 IMAP IDLE 长连接，延迟 1-5 秒，与 QQ 邮箱相同。

---

## QQ邮箱 配置

**1. 开启 IMAP 服务**
1. 登录 [QQ邮箱](https://mail.qq.com) → 设置 → 账户
2. 找到「IMAP/SMTP服务」→ 开启 → 手机短信验证

**2. 获取授权码**
1. 开启服务后弹出授权码（16位字母）
2. 如需重新获取：账户页面 → 生成授权码

```yaml
- type: qq
  mailboxes:
    - label: 我的QQ邮箱
      email: 123456@qq.com
      app_pass: "xxxxxxxxxxxxxxxx"   # 授权码，非QQ密码
```

---

## Outlook 配置

### 方案一：手动获取 refresh_token（简单，Graph API 轮询）

**1. 浏览器打开授权链接**
```
https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=7feada80-d946-4d06-b134-73afa3524fb7&response_type=code&redirect_uri=http://localhost&scope=https://graph.microsoft.com/Mail.Read%20https://graph.microsoft.com/Mail.ReadWrite%20offline_access&prompt=consent
```

**2. 获取 code**

授权后浏览器跳转到（页面无法打开是正常的）：
```
http://localhost/?code=M.C507_BAY...&session_state=xxx
```
复制 `code=` 后面的值（到 `&session_state` 为止）。

**3. 换取 refresh_token**
```bash
curl -X POST https://login.microsoftonline.com/common/oauth2/v2.0/token \
  -d "client_id=7feada80-d946-4d06-b134-73afa3524fb7" \
  -d "grant_type=authorization_code" \
  -d "code=你的code" \
  -d "redirect_uri=http://localhost" \
  -d "scope=https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite offline_access"
```

复制响应中的 `refresh_token`：
```yaml
- type: outlook
  mailboxes:
    - label: 我的Outlook
      email: you@hotmail.com
      refresh_token: "0.AXXX..."
      client_id: ""
```

> `refresh_token` 有效期约 90 天，程序运行期间自动续期。

---

### 方案二：自建 Azure 应用 + Change Notifications Push（实时推送，推荐）

**1. 注册 Azure 应用**
1. 打开 [Azure 门户](https://portal.azure.com) → 应用注册 → 新注册
2. 受支持账户类型选「任何组织目录中的账户和个人 Microsoft 账户」
3. 重定向 URI 类型选「移动和桌面应用程序」，填：`https://your-domain.com/api/emails/oauth/outlook/callback`
4. 记录「应用程序(客户端) ID」

**2. 配置 API 权限**
1. 左侧「API 权限」→ 添加 Microsoft Graph 委托权限：`Mail.Read`、`Mail.ReadWrite`、`User.Read`、`offline_access`
2. 点击「代表 xxx 授予管理员同意」

**3. 允许公共客户端流**

左侧「身份验证」→ 高级设置 → 「允许公共客户端流」→ 开启

**4. 填写配置**
```yaml
oauth:
  enabled: true
  client_id: "你的应用ID"
  client_secret: ""
  redirect_uri: "https://your-domain.com/api/emails/oauth/outlook/callback"
  port: 8080
```

**5. 一键授权**

启动容器后访问 `https://your-domain.com/auth/outlook`，登录账号完成授权，系统自动注册 Change Notifications 订阅。

> 订阅有效期 3 天，程序自动续期。

---

## 验证码识别规则

采用多层匹配，按优先级依次：

**第一层：上下文关键词（同行）**
邮件正文同一行出现以下关键词时，提取紧跟的 4-8 位字母数字：
- 中文：`验证码`、`动态码`、`校验码`、`确认码`、`激活码`
- 英文：`verification code`、`security code`、`login code`、`access code`、`OTP`、`passcode`、`one-time`、`auth code`、`sign-in code`、`your code` 等

**第二层：关键词后多行扫描**
关键词和验证码不在同一行时（如 Cloudflare Access 邮件），向后扫描最多 5 行：
- **优先匹配纯6位数字**（避免装饰性字符串如 `A2Tx8` 被误识别）
- 无纯数字时再匹配字母数字混合码
- 关键词所在行为长句描述时，从下一行开始扫描（跳过干扰词）
- 纯数字至少 6 位（4位数字大概率是手机尾号/年份）

**第三层：XXXX-XXXX 格式**
匹配 GitHub 等平台的字母数字混合格式（如 `ABCD-1234`），纯数字和纯字母均排除。

**第四层：纯6位数字降级**
仅当邮件整体含验证码相关词汇时触发，同时排除：
- 交易类邮件（PayPal、银行转账等含 `transaction`/`payment`/`order` 等词）
- 年份（`1999`-`2027` 开头的6位数）
- 全同数字（`111111`）、常见误判数字（`300000` 等）

**过滤规则**
- HTML 邮件先转纯文本再匹配，防止 CSS 属性值被误识别
- 字母数字混合时必须含至少1个数字
- 纯字母字符串不识别为验证码

---

## 推送格式

**有验证码：**
```
`821543`

📬 我的Gmail
发件人: noreply@example.com
时间: 2026-04-16 10:08
主题: 验证您的邮箱地址
```

**`forward_all: true` 时额外发送 HTML 附件**，附件顶部包含发件人、收件人、时间，点开查看完整邮件排版。

---

## 常见问题

**Q: Gmail token 失效 / invalid_grant**
- refresh_token 已过期或被撤销（改密码、手动撤权、超过50个授权均会导致失效）
- 程序会自动发 Telegram 警告，访问 `/auth/gmail` 重新授权即可

**Q: 如何强制所有账号走 IMAP 轮询，不用 Push**
- 在 config.yaml 顶层设置 `mode: idle`

**Q: Gmail 登录失败**
- 使用应用专用密码，不是 Gmail 登录密码；确认 IMAP 已开启，两步验证已启用

**Q: QQ邮箱登录失败**
- `app_pass` 是授权码，不是 QQ 密码；授权码只显示一次，忘记需重新生成

**Q: Outlook token 刷新失败**
- `refresh_token` 已过期，重新执行授权步骤；使用自建 Azure 应用时访问 `/auth/outlook` 重新授权

**Q: Change Notifications / Gmail Push 收不到推送**
- 确认域名可从公网访问，端口 8080 已开放
- 查看容器日志：`docker compose logs -f`

**Q: 收不到 Telegram 消息**
- 确认 `bot_token` 和 `chat_id` 正确；确认已给 bot 发过消息
