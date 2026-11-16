# QQ Assistant

一个自托管的个人 QQ 助手：接收多邮箱新邮件通知、管理定时待办，并通过 DeepSeek Harness 进行对话。

## 处理逻辑

- **QQ**：NapCat 登录独立的助手 QQ 号，只处理 `OWNER_QQ` 指定用户的私聊文字。
- **对话与待办**：基础指令直接执行，其他消息交给 Harness 理解；助手校验模型结果后创建或管理待办。默认时区为北京时间，未指定时间时使用当天 09:00。
- **邮箱**：通过 IMAP 或 Microsoft Graph 定期检查收件箱，默认每 120 秒通知新邮件的发件人和主题。首次同步只建立基准，不提醒历史邮件。
- **记忆与恢复**：Harness 持久化会话并自动压缩长对话；待办和待发送消息保存在 SQLite 中。模型可能遗漏旧对话细节；投递失败会重试，极端情况下可能重复通知。

邮箱正文不会由邮箱流程发送给模型；发给助手的私聊文本、对话上下文和待办信息会发送给 DeepSeek。当前未启用联网搜索或任意命令执行。

## 文件结构

```text
hence/                    # QQ 消息、待办、邮箱、Harness 接口和 SQLite 存储
harness_runtime/          # Harness 服务及其工具配置
config/mailboxes.example.json  # 多邮箱配置示例
compose.yaml              # 助手与 NapCat
compose.harness.yaml      # DeepSeek Harness 扩展配置
Dockerfile*               # 容器镜像构建文件
docs/harness-deployment.md # 构建、部署和故障排查
tests/                    # 自动化测试
LICENSE                   # 本项目许可证
THIRD-PARTY-NOTICES.md    # 第三方组件及其许可
.env                      # 运行凭据，本地创建，不提交
config/mailboxes.json     # 实际邮箱配置，不提交
data/                     # QQ 登录、待办、邮箱进度和聊天记录，不提交
```

## 配置与启动

需要 Linux 服务器、Docker Compose、一个助手 QQ 小号和 DeepSeek API Key。首次部署时，复制示例配置（已有配置请勿覆盖）：

```bash
cp -n .env.example .env
cp -n config/mailboxes.example.json config/mailboxes.json
mkdir -p data/assistant data/harness data/qq data/napcat-config data/napcat-plugins
sudo chown 10001:10001 data/assistant data/harness config/mailboxes.json
sudo chmod 700 data/assistant data/harness
sudo chmod 600 .env config/mailboxes.json
```

在 `.env` 中填写你的 `OWNER_QQ`、独立随机的 `NAPCAT_WEBUI_TOKEN`、`ONEBOT_TOKEN` 和 `HARNESS_AUTH_TOKEN`，以及 `LLM_API_KEY`、`LLM_MODEL` 和邮箱客户端专用密码。可用 `openssl rand -hex 32` 生成 Token。邮箱在 `config/mailboxes.json` 中逐项配置，`password_env` 指向 `.env` 里的凭据变量；每个邮箱的 `id` 应保持唯一且稳定。

IMAP 使用 TLS 和证书验证，服务器地址和客户端专用密码需要向邮箱服务商确认；Microsoft 365 邮箱可使用 Graph 设备授权，需要具备 `Mail.ReadBasic` 委托权限的 Entra 应用，授权参数见 `python -m hence.graph_auth --help`。邮箱能否接入取决于服务商策略。

启动完整服务：

```bash
docker compose -f compose.yaml -f compose.harness.yaml up -d --build
```

使用预先导入的镜像时，先确认 Compose 中的镜像标签正确，再将 `--build` 换成 `--no-build --pull never`。镜像下载和 Harness 升级步骤见 [部署说明](docs/harness-deployment.md)。

NapCat WebUI 只绑定服务器回环地址。通过 `ssh -L 6099:127.0.0.1:6099 用户名@服务器IP` 建立隧道，访问 `http://127.0.0.1:6099/webui`，用 `NAPCAT_WEBUI_TOKEN` 登录并扫码登录助手 QQ。在页面中启用正向 WebSocket 服务端，监听容器内 `0.0.0.0:3001`，Token 与 `ONEBOT_TOKEN` 一致。

首次启动后，在 QQ 向助手发送 `/help`；向已配置的邮箱发送测试邮件，确认新邮件通知。邮箱首次同步只记录基准，不会补发历史邮件。

## 常用指令

在 QQ 私聊中：

| 内容 | 作用 |
| --- | --- |
| `/help` | 查看固定指令及格式 |
| `/add 2027-11-20 09:00 更新网页` | 按北京时间创建未来提醒 |
| `/list` / `/list 2` | 查看未完成待办及编号，按页显示 |
| `/complete 1` / `/cancel 1` | 完成或取消指定待办 |

固定指令不依赖大模型；普通消息由模型理解，模型不可用时请用 `/add` 创建提醒。模型只接收指令与能力说明及最多 30 条待办快照，不能直接读写文件或邮箱，待办操作由助手校验执行。

服务器上查看服务与日志：

```bash
docker compose -f compose.yaml -f compose.harness.yaml ps
docker compose -f compose.yaml -f compose.harness.yaml logs --tail=50 assistant harness
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 运行注意事项

- 不要公开 `.env`、邮箱授权码、QQ 登录数据或聊天记录；备份时保护 `data/` 和邮箱配置。
- 容器配置了自动重启；邮箱连续三次检查失败及恢复时会通过 QQ 通知。QQ 掉线或服务器停机时无法即时收到告警，恢复后会重试待发送消息。
- 首次登录 NapCat 需要扫码；重启服务不等同于清除聊天记忆。`data/assistant` 保存待办和邮件同步进度，`data/harness` 保存 Harness 会话。
- 本项目通过第三方 NapCat 接入 QQ，登录可能失效或需要重新验证；QQ、NapCat 与 DeepSeek 均有各自的服务或许可条款。部署和模型调用可能产生费用。

## 许可

项目自身代码使用 [MIT License](LICENSE)；依赖与第三方服务的条款见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
