# 部署与故障排查

项目使用 Docker Compose 运行助手、NapCat 和独立的 DeepSeek Harness。先按 [README](../README.md) 创建并填写 `.env` 与 `config/mailboxes.json`，再在项目目录中启动：

```bash
docker compose -f compose.yaml -f compose.harness.yaml up -d --build
docker compose -f compose.yaml -f compose.harness.yaml ps
```

服务器需要 Docker Compose、可访问的镜像仓库、PyPI 和 DeepSeek API。镜像构建会下载依赖；服务器及模型 API 可能产生费用。QQ 登录、邮箱授权与测试步骤见 README。使用非官方 QQ 接入可能触发登录验证或下线，具体取决于平台策略。

## 本地构建并传输到服务器

如果服务器无法访问镜像仓库，可在网络可用的电脑上、从项目目录执行：

```bash
docker compose -f compose.yaml -f compose.harness.yaml build assistant harness
docker pull mlikiowa/napcat-docker:latest
docker save -o qq-assistant-images.tar \
  hence-agent-assistant:harness hence-agent-harness:latest mlikiowa/napcat-docker:latest
```

将镜像包传到服务器（`<用户>` 和 `<服务器地址>` 需自行替换；目录须已存在）：

```bash
scp qq-assistant-images.tar <用户>@<服务器地址>:~/qq-assistant-images.tar
```

服务器上先准备好项目源码与自己的配置文件，再从项目目录执行：

```bash
docker load -i ~/qq-assistant-images.tar
docker compose -f compose.yaml -f compose.harness.yaml up -d --no-build --pull never
```

已部署的服务器应保留自己的 `.env`、邮箱配置、`data/` 和 Compose 镜像标签。更新前先备份数据；不要用示例配置覆盖正在使用的配置。镜像包也可能很大，不应提交到仓库。

## 检查状态

```bash
docker compose -f compose.yaml -f compose.harness.yaml ps
docker compose -f compose.yaml -f compose.harness.yaml logs --tail=50 assistant harness
```

- **QQ 无回复**：查看助手日志中的 OneBot 连接状态，确认 NapCat 账号在线且 WebSocket 服务端已启用。如需重新登录，通过 README 中的 SSH 隧道访问 WebUI；不要公开 Token 或二维码。
- **模型不可用**：检查 Harness 容器状态、API 凭据、模型名称及网络；发送 `/help` 查看不依赖模型的固定指令。
- **邮箱无通知**：检查客户端专用密码、IMAP/Graph 授权与网络，查看助手日志。首次同步不通知历史邮件；连续三次检查失败会在 QQ 连接可用时发出告警。
- **提醒或通知未收到**：检查服务运行状态和 QQ 登录状态；消息发送失败会重试，极端情况下可能重复。截图和日志分享前应移除凭据、邮箱地址及聊天内容。

## 数据与隐私

`data/assistant` 保存待办及邮箱同步状态，`data/harness` 保存会话数据，`data/qq` 与 `data/napcat-*` 包含 QQ 登录或配置数据。备份时应停止相应服务并保护文件权限；不要公开这些目录、`.env`、邮箱授权码或访问令牌。

邮箱正文不会由邮箱流程发送给模型；用户主动发送的私聊文本、会话上下文和待办快照会发送给所配置的模型服务。Harness 未启用模型可用的文件系统和 shell 工具。
