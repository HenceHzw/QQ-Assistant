# QQ Assistant

通过 QQ 官方机器人管理待办、接收邮件提醒的自托管个人助手。使用腾讯官方 Harness 插件连接 QQ，支持多个邮箱和持续对话；固定指令可在模型不可用时使用。

[快速开始](#快速开始) · [使用方式](#使用方式) · [工作原理](#工作原理) · [部署与故障排查](docs/harness-deployment.md)

## Motivation

我经常忘记查看学校邮箱，也觉得反复打开多个邮箱有些麻烦；待办软件虽然方便，却也容易忘记打开和管理。相比之下，QQ 是我每天都会打开、闲下来时肌肉记忆也会顺手点开的应用。因此，我想以QQ为接口，做一个能够帮我检查多个邮箱、提醒新邮件，也能通过自然语言随手记下待办并且按时提醒的工具。

## 能做什么

- **邮件通知**：通过 IMAP 检查多个收件箱，在 QQ 提醒新邮件的发件人和主题。
- **待办提醒**：用自然语言创建、查看和管理单次提醒；到期后通过 QQ 发送通知。
- **日期提醒**：为体测、截止日期等事项设置提前提醒，只在临近时显示在待办列表。
- **日常对话**：通过 DeepSeek Harness 进行对话，重启后恢复会话；长对话会压缩，旧信息可能被遗漏。
- **固定指令**：不依赖模型，支持创建、查看、完成和取消待办。

## 使用方式

在 QQ 与你创建的官方机器人的私聊中，直接说：

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

序号以当前列表为准。按序号操作前请先查看对应页，例如处理第 6 项前发送 `/list 2`。也可以直接说“取消体测”，助手会列出相关事项，等你确认后再处理。普通待办提醒过后仍会留在列表中，直到你完成或取消它。

生日、体测、考试和报名截止日期等会默认记为日期提醒，无需特意说明类型；你指定的提醒方式优先。具体用法见[日期提醒](docs/date-reminders.md)。

## 快速开始

### 准备

- Linux 服务器或 WSL 2 环境，已安装 Git、Docker 和 Docker Compose。
- 在 [QQ 开放平台](https://q.qq.com/qqbot/openclaw/login.html)创建一个官方机器人，取得 AppID 和 AppSecret，并将机器人添加到自己的 QQ。
- DeepSeek API Key。启用 IMAP 邮箱时，还需要邮箱服务商提供的 IMAP 地址和客户端授权码。
- 部署环境能够访问 Docker 镜像仓库、npm、PyPI、QQ、DeepSeek API 和所用的邮箱服务。服务器和模型调用可能产生费用。

### 1. 创建配置

用 `git clone` 克隆本项目的 GitHub 仓库，进入项目目录后复制示例配置。

```bash
cp -n .env.example .env
cp -n config/mailboxes.example.json config/mailboxes.json
mkdir -p data/assistant data/harness
```

编辑 `.env`，填写 `QQ_BOT_APP_ID`、`QQ_BOT_APP_SECRET`、`LLM_API_KEY`、`LLM_MODEL` 和邮箱授权码。用 `openssl rand -hex 32` 生成 `HARNESS_AUTH_TOKEN`；`QQ_BOT_OWNER_OPENID` 暂时留空，下一步绑定后填写。

使用 QQ 邮箱时，在 `config/mailboxes.json` 的示例中填入真实邮箱地址，在 `.env` 中填写 `QQ_MAIL_AUTH_CODE`。添加其他 IMAP 邮箱时复制一个配置对象，填写独立的 `id`、主机名和用户名，并让 `password_env` 指向对应的环境变量。只使用待办功能时，邮箱配置可写为 `[]`。

编辑完成后，设置容器访问所需的文件权限：

```bash
sudo chown 10001:10001 data/assistant data/harness config/mailboxes.json
sudo chmod 700 data/assistant data/harness
sudo chmod 600 .env config/mailboxes.json
```

### 2. 绑定自己的 QQ

先构建镜像并启动插件，再运行本人绑定程序；程序会自动等待 QQ 连接就绪：

```bash
docker compose build
docker compose up -d harness
docker compose \
  run --rm --no-deps assistant python -m hence.qqbot --bind
```

终端会显示一条 `/bind 随机码`。在 5 分钟内用你自己的 QQ 私聊机器人发送这条消息，再把终端返回的 `QQ_BOT_OWNER_OPENID=...` 写入 `.env`。**OpenID 不是 QQ 号码**，它只对应当前机器人；只有持有随机码的账号才能完成这次校验。

### 3. 启动并验证

```bash
docker compose up -d --force-recreate
docker compose ps
```

在 QQ 中允许机器人主动发送消息，以便接收定时提醒。

向机器人发送 `/help`，再测试一条即将到期的提醒。首次配置邮箱时，请等待收件箱初次同步完成后再发送测试邮件：首次同步只建立基准，不会通知已有邮件；默认每 120 秒检查一次。IMAP 邮箱初始化成功后，助手日志会显示 `Mailbox 邮箱ID initialized`，可用以下命令查看：

```bash
docker compose logs --tail=50 assistant
```

如未收到邮件通知，请参考[故障排查](docs/harness-deployment.md#检查状态)。

## 工作原理

```text
个人 QQ ←→ 腾讯官方插件 ←→ 助手 ←→ Harness 对话
                            ├── IMAP 邮箱
                            └── SQLite（待办、同步进度、消息队列）
```

助手只处理 `QQ_BOT_OWNER_OPENID` 指定账号的私聊文字，不处理群消息或附件。普通消息交给模型理解，待办操作由助手校验执行；固定指令直接由助手处理。

模型接收聊天与待办上下文，邮箱检查独立运行。数据保存位置及模型可访问的内容见[数据与权限](docs/harness-deployment.md#数据与权限)。

## 运维

在项目目录执行 `docker compose ps` 查看运行状态，执行 `docker compose logs --tail=50 assistant harness` 检查日志。[备份与更新](docs/harness-deployment.md#备份与更新) · [插件与测试](docs/harness-deployment.md#插件与测试)

## 项目结构

```text
hence/                         助手逻辑：QQ、邮箱、待办和存储
harness_runtime/               Harness 服务、官方插件依赖与适配层
config/mailboxes.example.json  邮箱配置模板
docs/                          部署与故障排查
tests/                         自动化测试
compose.yaml                   助手与 Harness 插件服务配置
Dockerfile*                    镜像构建文件
```

## 许可

项目自身代码使用 [MIT License](LICENSE)。QQ 开放平台、DeepSeek 和其他依赖有各自的条款，详见 [第三方组件说明](THIRD-PARTY-NOTICES.md)。
