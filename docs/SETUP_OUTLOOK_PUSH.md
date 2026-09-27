# Outlook Push 配置备忘

> 本文档用于在重新部署或迁移域名时参考。敏感信息请自行保管。

---

## Azure 应用注册

### 前置条件

- Azure 订阅（免费层足够）
- Microsoft 账号登录权限

### 第一步：注册新应用（或查看现有应用）

1. 打开 [Azure 门户 - 应用注册](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. 点「新注册」
3. 填写：
   - **名称**：`mail-monitor`
   - **受支持账户类型**：选「任何组织目录中的账户和个人 Microsoft 账户」
   - **重定向 URI**：选择「移动和桌面应用程序」，输入 `https://your-domain.com/api/emails/oauth/outlook/callback`（替换为实际域名）
4. 点「注册」

### 第二步：记录应用 ID

1. 在应用概览页面，复制「应用程序(客户端) ID」
2. 保存到安全的地方（本文档最后有示例）

### 第三步：配置 API 权限

1. 左侧点「API 权限」
2. 点「添加权限」→「Microsoft Graph」
3. 选「委托的权限」，搜索并勾选：
   - `Mail.Read`
   - `Mail.ReadWrite`
   - `User.Read`
   - `offline_access`
4. 点「添加权限」

### 第四步：启用公共客户端流

1. 左侧点「身份验证」
2. 找到「高级设置」部分
3. 将「允许公共客户端流」设置为 `是`
4. 点「保存」

> 此步骤允许桌面和移动应用进行 OAuth 授权

---

## Change Notifications 订阅配置

### 推送端点

配置推送端点地址：
```
https://your-domain.com/api/outlook/push
```

> 替换 `your-domain.com` 为实际部署域名

**要求：**
- 必须是 HTTPS（不支持 HTTP）
- 必须公网可访问
- HTTPS 证书必须有效
- 建议配置 DNS 解析

### 订阅有效期

- **有效期**：3 天
- **自动续期**：程序每 1 天自动刷新一次
- **无需手动管理**：系统后台自动处理

---

## 账号授权

### 首次授权新账号

1. 启动容器后，访问 `https://your-domain.com/auth/outlook`
2. 使用 Microsoft 账号登录
3. 点「接受」同意应用访问邮件
4. 授权完成后自动注册 Change Notifications 订阅

> 授权信息保存在 `config.yaml`，无需重复授权

### 重新授权已有账号

1. 访问 `https://your-domain.com/auth/outlook`
2. 重复上述步骤即可

---

## 配置信息记录

### 应用注册信息

| 项目 | 值 |
|------|-----|
| 应用名 | `mail-monitor` |
| 应用 ID（公开） | `8c66bce8-c8a3-4b75-8120-58e307a85a3e` |
| 重定向 URI | `https://your-domain.com/api/emails/oauth/outlook/callback` |
| API 权限 | Mail.Read, Mail.ReadWrite, User.Read, offline_access |

> ⚠️ **client_secret 不填到本文档**（敏感信息，保管好别泄露）

### config.yaml 配置

```yaml
oauth:
  enabled: true
  client_id: "8c66bce8-c8a3-4b75-8120-58e307a85a3e"
  client_secret: ""  # 保存在本地，不上传到 Git
  redirect_uri: "https://your-domain.com/api/emails/oauth/outlook/callback"
  port: 8080
```

---

## 部署或迁移场景

### 场景 1：首次部署

1. 上述步骤完成后，更新 `config.yaml` 中的 `oauth` 配置
2. 启动容器
3. 访问 `https://your-domain.com/auth/outlook` 授权账号
4. 系统自动注册 Change Notifications 订阅

### 场景 2：域名变更（如迁移服务器）

1. **更新 Azure 应用配置**（必须）
   - 打开 [Azure 应用注册](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
   - 进入 `mail-monitor` 应用
   - 左侧「身份验证」→ 重定向 URI
   - 更新为新域名：`https://new-domain.com/api/emails/oauth/outlook/callback`
   - 点「保存」

2. **更新 config.yaml**
   ```yaml
   oauth:
     redirect_uri: "https://new-domain.com/api/emails/oauth/outlook/callback"
   ```

3. **重新授权所有账号**
   - 重启容器
   - 访问 `https://new-domain.com/auth/outlook`
   - 使用各个 Outlook 账号重新授权
   - 系统自动注册新的 Change Notifications 订阅

### 场景 3：需要创建新应用（如当前应用泄露或无法使用）

1. 注册新的 Azure 应用（参考「第一步」）
2. 获得新的应用 ID
3. 更新 `config.yaml` 中的 `client_id`
4. **不需要更新 client_secret**（新应用重新创建也行）
5. 删除 `config.yaml` 中已有 Outlook 账户的 `refresh_token`（强制重新授权）
6. 重启容器后，访问 `/auth/outlook` 重新授权

---

## 常见问题

**Q: Change Notifications 推送无法收到**
- 检查推送端点是否公网可访问：`curl -X POST https://your-domain.com/api/outlook/push`
- 检查 HTTPS 证书是否有效
- 查看容器日志：`docker logs mail-monitor | grep outlook`
- 确认 Azure 应用的重定向 URI 与部署域名一致

**Q: 授权后收不到新邮件通知**
- 容器日志检查是否有报错
- 确认 Change Notifications 订阅已注册（启动日志中应有相关信息）
- 订阅有效期 3 天，如超期需重新授权

**Q: refresh_token 过期了怎么办**
- 访问 `/auth/outlook` 重新授权
- 新的 refresh_token 会自动保存并替换旧的

**Q: 能否使用 Office 365 多租户应用**
- 可以，但需要额外配置租户相关参数
- 建议使用个人应用或单租户应用（更简单）

**Q: 应用 ID 暴露了怎么办**
- 应用 ID 本身不是秘密（Azure 中可见）
- 真正需要保护的是 `client_secret`
- 如果 client_secret 泄露，需要重新生成或创建新应用
