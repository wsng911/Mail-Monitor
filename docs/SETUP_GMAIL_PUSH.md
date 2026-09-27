# Gmail Push 配置备忘

> 本文档用于在重新部署或迁移域名时参考。敏感信息请自行保管。

## 前置条件

- Google Cloud 项目已创建：`mail-monitor-493615`
- Gmail API 和 Cloud Pub/Sub API 已启用
- OAuth 同意屏幕已配置

---

## 新增邮箱账号

### 第一步：GCP 添加测试用户

1. 打开 [OAuth 同意屏幕](https://console.cloud.google.com/apis/credentials/consent?project=mail-monitor-493615)
2. 点「添加用户」
3. 输入 Gmail 邮箱地址
4. 保存

> 测试用户可以在应用未发布时进行 OAuth 授权

### 第二步：授权账号

1. 访问 `https://your-domain.com/auth/gmail`（替换为实际域名）
2. 使用 Gmail 账号登录并授权
3. 授权完成后，系统自动注册 Gmail Watch 订阅

---

## Pub/Sub 主题和订阅配置

> 仅在首次设置或需要重建时执行

### 1. 创建 Pub/Sub 主题

1. 打开 [Pub/Sub 主题页](https://console.cloud.google.com/cloudpubsub/topic/list?project=mail-monitor-493615)
2. 点「创建主题」
3. 主题 ID 填：`gmail-push`
4. **取消勾选「创建默认订阅」**
5. 点「创建」

### 2. 配置发布者权限

1. 进入刚创建的主题（`gmail-push`）
2. 点「权限」→「添加主账号」
3. 输入：`gmail-api-push@system.gserviceaccount.com`
4. 角色选：**Pub/Sub 发布者**
5. 点「保存」

> 此步骤允许 Gmail API 将新邮件事件发送到 Pub/Sub 主题

### 3. 创建推送订阅

1. 打开 [Pub/Sub 订阅页](https://console.cloud.google.com/cloudpubsub/subscription/list?project=mail-monitor-493615)
2. 点「创建订阅」
3. 填写：
   - **订阅 ID**：`gmail-push-sub`
   - **选择 Cloud Pub/Sub 主题**：`gmail-push`
   - **传递类型**：选「推送」
   - **推送端点**：`https://your-domain.com/api/gmail/push`（替换为实际域名）
4. 点「创建」

> 推送端点在部署时需要公网可访问，且 HTTPS 证书有效

---

## 获取配置信息

### GCP 项目 ID
```
mail-monitor-493615
```

### Pub/Sub 主题
```
projects/mail-monitor-493615/topics/gmail-push
```

### OAuth 客户端 ID

1. 打开 [GCP 凭据页](https://console.cloud.google.com/apis/credentials?project=mail-monitor-493615)
2. 找到类型为「OAuth 2.0 客户端 ID」的凭据
3. 复制「客户端 ID」（格式：`xxxxxxx-yyyyyyy.apps.googleusercontent.com`）

### 配置文件示例

```yaml
gmail_push:
  client_id: "your_client_id"      # 从上述步骤获取
  client_secret: "your_secret"     # 从 GCP 凭据页面保存
  pubsub_topic: "projects/mail-monitor-493615/topics/gmail-push"
```

---

## 重新部署或迁移域名

### 情况 1：域名变更

1. 打开 [Pub/Sub 订阅详情](https://console.cloud.google.com/cloudpubsub/subscription/detail/gmail-push-sub?project=mail-monitor-493615)
2. 点「编辑」
3. 更新「推送端点」为新域名（如：`https://new-domain.com/api/gmail/push`）
4. 点「保存」

> Pub/Sub 订阅本身无需重建，域名验证永久有效

### 情况 2：容器重启或服务迁移

1. 更新 `config.yaml` 的 `gmail_push` 配置（若 client_id 或 client_secret 变更）
2. 重启容器
3. **重新授权账号**：访问 `https://new-domain.com/auth/gmail`，使用各个 Gmail 账号登录并授权
4. 授权完成后，系统自动刷新 Gmail Watch 订阅

### 清除积压消息

如果部署时间较长，Pub/Sub 可能积压旧消息，可以手动清除：

1. 打开 [订阅详情](https://console.cloud.google.com/cloudpubsub/subscription/detail/gmail-push-sub?project=mail-monitor-493615)
2. 点「完全清除」（仅删除未消费的消息，不影响已推送的通知）

---

## 常见问题

**Q: Gmail Push 无法收到新邮件通知**
- 检查推送端点是否公网可访问（`curl https://your-domain.com/api/gmail/push`）
- 检查 HTTPS 证书是否有效
- 查看容器日志：`docker logs mail-monitor | grep gmail`

**Q: 如何添加新的 Gmail 账号**
- 只需在 GCP 添加测试用户后，访问 `/auth/gmail` 授权即可
- 无需重新创建 Pub/Sub 主题和订阅

**Q: Gmail Watch 订阅过期了怎么办**
- Watch 有效期 7 天，程序自动续期
- 如果中断过长，访问 `/auth/gmail` 重新授权

**Q: 能否使用多个 GCP 项目**
- 可以，但需要分别配置 client_id、client_secret 和 pubsub_topic
- 不同项目的 Pub/Sub 主题需要分别创建
