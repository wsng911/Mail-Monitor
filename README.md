# mail-code-monitor

监控 Gmail / QQ邮箱 / Outlook 收件箱，收到验证码自动推送 Telegram，支持转发完整邮件（HTML附件）。

Docker Hub: [wsng911/mail-code-monitor](https://hub.docker.com/r/wsng911/mail-code-monitor)

---

## 快速部署

```bash
mkdir -p /home/mail-monitor/config && cd /home/mail-monitor

# 编辑配置文件
nano config/config.yaml

# 启动
docker compose up -d
docker compose logs -f
```

`docker-compose.yml`：

```yaml
services:
  mail-monitor:
    image: wsng911/mail-code-monitor:v1.11
    container_name: mail-monitor
    restart: unless-stopped
    volumes:
      - ./config:/config
```

---

## config.yaml 完整示例

```yaml
telegram:
  bot_token: "your_bot_token"
  chat_id: "your_chat_id"

poll_interval: 30       # 轮询间隔（秒）
forward_all: false      # true = 转发所有邮件+HTML附件；false = 只推验证码

accounts:
  - type: gmail
    label: 我的Gmail
    email: you@gmail.com
    app_pass: "xxxx xxxx xxxx xxxx"

  - type: qq
    label: 我的QQ邮箱
    email: 123456@qq.com
    app_pass: "xxxxxxxxxxxxxxxx"

  - type: outlook
    label: 我的Outlook
    email: you@hotmail.com
    refresh_token: "0.AXXX..."
    client_id: ""         # 留空使用内置默认值
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

## Gmail 配置（应用专用密码 + IMAP）

> 需要开启两步验证才能使用应用专用密码

**第一步：开启 Gmail IMAP**

1. 打开 [Gmail 设置](https://mail.google.com) → 右上角齿轮 → 查看所有设置
2. 选择「转发和 POP/IMAP」标签
3. 「IMAP 访问」→ 启用 IMAP → 保存

**第二步：生成应用专用密码**

1. 打开 [Google 账号安全设置](https://myaccount.google.com/security)
2. 确认已开启「两步验证」
3. 搜索「应用专用密码」或访问：https://myaccount.google.com/apppasswords
4. 选择应用「邮件」→ 生成
5. 复制 16 位密码（格式如 `xxxx xxxx xxxx xxxx`）

**填入配置：**

```yaml
- type: gmail
  label: 我的Gmail
  email: you@gmail.com
  app_pass: "xxxx xxxx xxxx xxxx"   # 16位应用专用密码
```

---

## QQ邮箱 配置（授权码 + IMAP）

**第一步：开启 IMAP 服务**

1. 登录 [QQ邮箱](https://mail.qq.com)
2. 顶部「设置」→「账户」
3. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」
4. 开启「IMAP/SMTP服务」→ 按提示用手机发短信验证

**第二步：获取授权码**

1. 开启服务后会弹出授权码（16位字母）
2. 如需重新获取：「账户」页面 → 「生成授权码」

**填入配置：**

```yaml
- type: qq
  label: 我的QQ邮箱
  email: 123456@qq.com
  app_pass: "xxxxxxxxxxxxxxxx"   # 16位授权码
```

---

## Outlook / Hotmail 配置（OAuth2 refresh_token）

使用内置公共 `client_id`，无需注册 Azure 应用。

**第一步：浏览器打开授权链接**

复制以下链接到浏览器，登录你的 Outlook/Hotmail 账号并授权：

```
https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=7feada80-d946-4d06-b134-73afa3524fb7&response_type=code&redirect_uri=http://localhost&scope=https://graph.microsoft.com/Mail.Read%20offline_access&prompt=consent
```

**第二步：获取 code**

授权后浏览器会跳转到类似：
```
http://localhost/?code=M.C507_BAY...&session_state=xxx
```
复制 `code=` 后面的完整字符串（到 `&` 为止）。

**第三步：换取 refresh_token**

```bash
curl -X POST https://login.microsoftonline.com/common/oauth2/v2.0/token \
  -d "client_id=7feada80-d946-4d06-b134-73afa3524fb7" \
  -d "grant_type=authorization_code" \
  -d "code=你的code" \
  -d "redirect_uri=http://localhost" \
  -d "scope=https://graph.microsoft.com/Mail.Read offline_access"
```

响应示例：
```json
{
  "access_token": "...",
  "refresh_token": "0.AXXX...",
  ...
}
```

复制 `refresh_token` 的值。

**填入配置：**

```yaml
- type: outlook
  label: 我的Outlook
  email: you@hotmail.com
  refresh_token: "0.AXXX..."
  client_id: ""    # 留空即可
```

> refresh_token 有效期约 90 天，到期后需重新授权。程序运行期间会自动续期。

---

## 推送格式说明

**有验证码的邮件：**
```
`821543`

📬 我的Gmail
发件人: noreply@example.com
时间: 2026-04-16 10:08
主题: 验证您的邮箱地址
```
（验证码可直接点击复制）

**forward_all: true 时额外发送 .html 附件**，点开即可查看完整邮件排版和图片。

---

## 常见问题

**Q: Gmail 提示登录失败**
- 确认已开启两步验证
- 确认使用的是应用专用密码，不是 Gmail 登录密码
- 确认 IMAP 已在 Gmail 设置中开启

**Q: QQ邮箱登录失败**
- `app_pass` 填的是授权码，不是 QQ 密码
- 授权码只显示一次，忘记需重新生成

**Q: Outlook token 刷新失败**
- refresh_token 已过期，重新执行授权流程获取新的
- 确认 `email` 字段填写正确

**Q: 验证码重复推送**
- 正常现象，首次启动会读取未读邮件
- 重启后已读邮件不会再推送
