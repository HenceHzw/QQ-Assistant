# QQ Assistant

通过 QQ 管理待办、接收邮件提醒的自托管个人助手。使用 DeepSeek Harness 对话，支持多个邮箱；固定指令可在模型不可用时使用。

[快速开始](#快速开始) · [使用方式](#使用方式) · [工作原理](#工作原理) · [部署与故障排查](docs/harness-deployment.md)

## Motivation

我经常忘记查看学校邮箱，也觉得反复打开多个邮箱有些麻烦；待办软件虽然方便，却也容易忘记打开和管理。相比之下，QQ 是我每天都会打开、闲下来时肌肉记忆也会顺手点开的应用。因此，我想以QQ为接口，做一个能够帮我检查多个邮箱、提醒新邮件，也能通过自然语言随手记下待办并且按时提醒的工具。

## 能做什么

- **邮件通知**：通过 IMAP 或 Microsoft Graph 检查多个收件箱，在 QQ 提醒新邮件的发件人和主题。
- **待办提醒**：用自然语言创建、查看和管理单次提醒；到期后通过 QQ 发送通知。
- **日期提醒**：为体测、截止日期等事项设置提前提醒，只在临近时显示在待办列表。
- **日常对话**：通过 DeepSeek Harness 进行对话，重启后恢复会话；长对话会压缩，旧信息可能被遗漏。
- **固定指令**：不依赖模型，支持创建、查看、完成和取消待办。

## 使用方式

在个人 QQ 与助手 QQ 的私聊中，直接说：

```text
明天上午 9 点提醒我交作业
现在有哪些待办？
```

也可以使用固定指令：

| 指令 | 作用 |
| --- | --- |
| `/help` | 查看指令及格式 |
| `/add 2027-11-20 09:00 更新网页` | 创建单次提醒，使用未来的北京时间 |
| `/list`、`/list 2` | 查看待办及编号，支持分页 |
| `/complete 1` | 完成编号为 1 的待办 |
| `/cancel 1` | 取消编号为 1 的待办 |
| `/date-add 2027-12-20 体测` | 创建日期提醒，默认提前两天及当天提醒 |

自然语言提醒默认使用北京时间；只说日期时默认 09:00。请检查助手回复中的事项和时间；模型不可用时请使用 `/add` 创建提醒。

待办按提醒时间排序，序号可能变化，按序号操作前请先查看 `/list`。也可以直接说“取消体测”，助手会列出相关事项，等你确认后再处理。普通待办提醒过后仍会留在列表中，直到你完成或取消它。

生日、体测、考试和报名截止日期等会默认记为日期提醒，无需特意说明类型；你指定的提醒方式优先。具体用法见[日期提醒](docs/date-reminders.md)。

## 快速开始

### 准备

- Linux 服务器或本机 Linux 环境，以及 Docker Compose。
- 一个供 NapCat 登录的助手 QQ 账号和你自己的 QQ 号码。
- DeepSeek API Key；邮箱服务商提供的 IMAP 地址及客户端专用密码或授权码。
- 网络可访问 Docker 镜像仓库、PyPI 和 DeepSeek API。服务器和模型调用可能产生费用。

### 1. 创建配置

进入项目目录，复制示例文件。`cp -n` 不会覆盖已有配置：

```bash
cp -n .env.example .env
cp -n config/mailboxes.example.json config/mailboxes.json
mkdir -p data/assistant data/harness data/qq data/napcat-config data/napcat-plugins
```

编辑 `.env`，填写 `OWNER_QQ`、`LLM_API_KEY`、`LLM_MODEL`、邮箱授权码，以及三个不同的随机 Token：`NAPCAT_WEBUI_TOKEN`、`ONEBOT_TOKEN`、`HARNESS_AUTH_TOKEN`。可用 `openssl rand -hex 32` 分别生成 Token。

在 `config/mailboxes.json` 中填写邮箱地址和 IMAP 服务器。`password_env` 是 `.env` 中对应授权码的变量名，**不要把授权码写进 JSON**。示例：

```json
[
  {
    "id": "personal",
    "name": "个人邮箱",
    "type": "imap",
    "host": "imap.example.com",
    "port": 993,
    "username": "you@example.com",
    "password_env": "PERSONAL_MAIL_AUTH_CODE"
  }
]
```

将示例地址替换为邮箱服务商提供的信息，并在 `.env` 中添加 `PERSONAL_MAIL_AUTH_CODE`。多个邮箱在数组中分别添加，每个 `id` 保持唯一且稳定；无需邮箱时可使用 `[]`。Microsoft 365 也可通过 Graph 设备授权接入，具体权限与参数见 `python -m hence.graph_auth --help`；可用性取决于邮箱服务商的策略。

编辑完成后，设置容器访问所需的文件权限：

```bash
sudo chown 10001:10001 data/assistant data/harness config/mailboxes.json
sudo chmod 700 data/assistant data/harness
sudo chmod 600 .env config/mailboxes.json
```

### 2. 启动服务

```bash
docker compose -f compose.yaml -f compose.harness.yaml up -d --build
docker compose -f compose.yaml -f compose.harness.yaml ps
```

如果服务器无法拉取镜像，参见[部署说明](docs/harness-deployment.md)。

### 3. 登录 QQ 并验证

NapCat WebUI 只监听服务器本地地址。远程部署时，在自己的电脑建立 SSH 隧道：

```bash
ssh -L 6099:127.0.0.1:6099 your-user@your-server
```

将 `your-user` 和 `your-server` 换成服务器登录用户名和地址。保持隧道打开，在浏览器访问 `http://127.0.0.1:6099/webui`，使用 `NAPCAT_WEBUI_TOKEN` 登录并扫码登录助手 QQ。在 WebUI 启用 OneBot **正向 WebSocket 服务端**，监听容器内 `0.0.0.0:3001`，Token 与 `.env` 中的 `ONEBOT_TOKEN` 一致。

向助手 QQ 发送 `/help`。首次配置邮箱时，请等待收件箱初次同步完成后再发送测试邮件：首次同步只建立基准，不会通知已有邮件；默认每 120 秒检查一次。IMAP 邮箱初始化成功后，助手日志会显示 `Mailbox 邮箱ID initialized`，可用以下命令查看：

```bash
docker compose -f compose.yaml -f compose.harness.yaml logs --tail=50 assistant
```

Microsoft Graph 邮箱不使用这条 IMAP 初始化日志；如同步异常，请参考[故障排查](docs/harness-deployment.md#检查状态)。

## 工作原理

```text
个人 QQ ←→ NapCat / OneBot ←→ 助手 ←→ DeepSeek Harness
                                  ├── IMAP / Microsoft Graph
                                  └── SQLite（待办、同步进度、待发送消息）
```

助手只处理 `OWNER_QQ` 指定账号的私聊文字。普通消息交给模型理解，待办操作由助手校验执行；固定指令直接由助手处理。邮箱通过 IMAP 或 Graph 接入，并不读取 Outlook 软件本身。

邮箱流程不会将邮件正文发送给模型；私聊文本、对话上下文、最多 30 条当前待办及 30 条未完成日期提醒快照会发送给模型服务。模型不能直接访问邮箱、文件系统或执行命令。

## 运维与数据

```bash
# 查看状态和日志
docker compose -f compose.yaml -f compose.harness.yaml ps
docker compose -f compose.yaml -f compose.harness.yaml logs --tail=50 assistant harness

# 运行测试
python3 -m unittest discover -s tests -v
```

- `.env` 保存 API Key、授权码和 Token；`config/mailboxes.json` 包含邮箱地址。它们不应提交到公开仓库。
- `data/assistant` 保存待办和邮箱进度，`data/harness` 保存会话，`data/qq` 和 `data/napcat-*` 保存 QQ 登录或配置数据。备份时应保护这些文件。
- QQ 下线可能需要重新验证；邮箱连续检查失败三次及恢复时会通过 QQ 通知。发送失败会重试，极端情况下可能重复通知。

更多操作和故障排查见[部署说明](docs/harness-deployment.md)。

## 项目结构

```text
hence/                         助手逻辑：QQ、邮箱、待办和存储
harness_runtime/               DeepSeek Harness 服务与配置
config/mailboxes.example.json  邮箱配置模板
docs/                          部署与故障排查
tests/                         自动化测试
compose.yaml                   助手与 NapCat
compose.harness.yaml           Harness 服务配置
Dockerfile*                    镜像构建文件
```

## 许可

项目自身代码使用 [MIT License](LICENSE)。NapCat、DeepSeek 和其他依赖有各自的条款，详见 [第三方组件说明](THIRD-PARTY-NOTICES.md)。
