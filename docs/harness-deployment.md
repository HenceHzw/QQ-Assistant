# 部署与故障排查

本文介绍服务器部署、镜像传输和更新。首次配置 QQ、邮箱和 DeepSeek API 的步骤见 [README](../README.md)。

## 选择部署方式

- **服务器可以访问 Docker 镜像仓库和 PyPI**：在服务器项目目录按 README 配置后，执行：

  ```bash
  docker compose -f compose.yaml -f compose.harness.yaml up -d --build
  ```

- **服务器无法拉取镜像**：在网络可用的电脑构建、打包镜像，再传到服务器。见下文的[本地构建与首次部署](#本地构建与首次部署)。

无论采用哪种方式，服务器运行时仍需能够连接 QQ、邮箱服务器和 DeepSeek API。服务器与 API 使用可能产生费用。本文中的 Docker 命令需要 Docker 权限；如果本机只能通过 `sudo docker` 运行，请自行添加 `sudo`。

## 本地构建与首次部署

本节适用于**新的服务器目录**。已有服务请先看[更新已有服务](#更新已有服务)。

### 1. 在电脑构建镜像

在电脑的项目目录执行。示例以服务器为 `linux/amd64` 架构为前提；服务器上可运行 `uname -m` 确认，输出 `x86_64` 对应 `amd64`。

```bash
docker build --platform linux/amd64 -f Dockerfile \
  -t hence-agent-assistant:harness .
docker build --platform linux/amd64 -f Dockerfile.harness \
  -t hence-agent-harness:latest .
docker pull --platform linux/amd64 mlikiowa/napcat-docker:latest
```

这里直接使用 Dockerfile 构建，不需要把服务器的 `.env` 复制到电脑。构建电脑仍需访问基础镜像仓库和 PyPI；如无法拉取镜像，请先配置可信的镜像源或网络环境。

将镜像和首次部署所需源码分别打包：

```bash
docker save hence-agent-assistant:harness hence-agent-harness:latest \
  mlikiowa/napcat-docker:latest > /tmp/qq-assistant-images.tar

tar -czf /tmp/qq-assistant-source.tar.gz \
  hence/*.py harness_runtime/server.py harness_runtime/profile.yml \
  config/mailboxes.example.json README.md docs/harness-deployment.md docs/date-reminders.md \
  .env.example .gitignore .dockerignore compose.yaml compose.harness.yaml \
  Dockerfile Dockerfile.harness requirements.txt LICENSE THIRD-PARTY-NOTICES.md
```

源码包只列出首次部署所需文件，不包含 `.env`、实际邮箱配置、`data/` 或测试文件。镜像包较大，不要提交到 Git 仓库。只向自己管理的服务器传输镜像；第三方镜像的分发应遵守各自条款。

### 2. 上传并配置服务器

将下面的 `your-user`、`your-server` 替换成服务器登录用户名和地址；若 SSH 使用 `-i`，`scp` 也需使用相同密钥参数。

```bash
scp /tmp/qq-assistant-images.tar /tmp/qq-assistant-source.tar.gz \
  your-user@your-server:~/
ssh your-user@your-server
```

在服务器终端创建**尚不存在**的目录，再解压源码：

```bash
mkdir ~/qq-assistant && \
  tar -xzf ~/qq-assistant-source.tar.gz -C ~/qq-assistant && \
  cd ~/qq-assistant && \
  docker load -i ~/qq-assistant-images.tar
```

如果目录已存在，请停止并选择另一个新目录，不要解压覆盖已有部署。按 [README 的创建配置步骤](../README.md#1-创建配置)在服务器填写自己的 `.env` 和邮箱配置、设置权限，然后使用导入的镜像启动：

```bash
docker compose -f compose.yaml -f compose.harness.yaml up -d --no-build --pull never
docker compose -f compose.yaml -f compose.harness.yaml ps
```

继续按 [README 的登录与验证步骤](../README.md#3-登录-qq-并验证)连接 QQ 和测试邮箱。确认运行正常后，可自行删除电脑和服务器上的传输包；传输包不是数据备份。

## 更新已有服务

以下操作在**服务器现有项目目录**执行。保留原目录与 Compose 项目名：改变它们可能让 Compose 将服务识别为新项目。不要覆盖服务器的 `.env`、`config/mailboxes.json`、`data/` 或已调整的 Compose 配置。

当前版本的自然语言功能统一使用 Harness，旧的 `LLM_URL` 配置不再使用。已经启用 Harness 的部署无需调整 API 配置；更新时请同时更新助手和 Harness 镜像。

1. 准备新版本镜像。例如，在电脑项目目录构建并上传（`update-1` 是示例标签，每次更新应使用尚未使用的新标签）：

   ```bash
   docker build --platform linux/amd64 -f Dockerfile -t hence-agent-assistant:update-1 .
   docker build --platform linux/amd64 -f Dockerfile.harness -t hence-agent-harness:update-1 .
   docker save hence-agent-assistant:update-1 hence-agent-harness:update-1 \
     > /tmp/qq-assistant-update.tar
   scp /tmp/qq-assistant-update.tar your-user@your-server:~/
   ```

   将服务器用户名和地址替换为实际值。已安装的新旧镜像使用不同标签，便于回退。
2. 更新前停止助手和 Harness，再备份配置与数据。下例假设这些文件和目录均已存在；备份文件含私人信息，应妥善保护：

   日期提醒版本会升级待办数据库。旧版程序不认识日期提醒，不能直接接管升级后的数据库；需要回退时，应先停止服务并恢复升级前的数据库备份。恢复备份会丢失备份之后产生的数据，请先妥善保留当前数据。

   ```bash
   docker compose -f compose.yaml -f compose.harness.yaml stop assistant harness
   umask 077
   tar -czf "$HOME/qq-assistant-backup-$(date +%Y%m%d-%H%M%S).tar.gz" \
     .env config/mailboxes.json data/assistant data/harness compose.harness.yaml
   ```

   此操作不停止 NapCat；若也需要备份 QQ 登录数据，应先停止 NapCat，再备份 `data/qq` 和 `data/napcat-*`。
3. 在服务器导入更新镜像，并检查当前 Compose 引用的镜像：

   ```bash
   docker load -i ~/qq-assistant-update.tar
   docker compose -f compose.yaml -f compose.harness.yaml config --images
   ```

   在服务器 `compose.harness.yaml` 中将助手和 Harness 的 `image` 标签分别改成 `hence-agent-assistant:update-1` 和 `hence-agent-harness:update-1`，或你本次构建时使用的标签。

   本地构建使用的标签与服务器已有标签可能不同。不要直接覆盖整个 Compose 文件，以免丢失服务器上的运行配置。若同步源码，应只更新需要的源码文件，并先核对新版本是否要求额外配置。
4. 确认备份、镜像和配置后，启动服务并检查日志：

   ```bash
   docker compose -f compose.yaml -f compose.harness.yaml \
     up -d --no-deps --no-build --pull never harness assistant
   docker compose -f compose.yaml -f compose.harness.yaml ps
   docker compose -f compose.yaml -f compose.harness.yaml logs --tail=50 assistant harness
   ```

如果更新失败并需要回退到不支持日期提醒的旧版，先停止助手和 Harness，妥善保留当前数据，再从升级前的备份恢复 `data/assistant` 和 Compose 配置，确认旧镜像仍存在后启动。恢复备份会丢失备份之后的数据。启动后用 `/help`、`/list` 和测试邮件验证；如需验证新邮箱，先等待首次同步完成。

## 检查状态

```bash
docker compose -f compose.yaml -f compose.harness.yaml ps
docker compose -f compose.yaml -f compose.harness.yaml logs --tail=50 assistant harness
```

- **QQ 无回复**：`OneBot disconnected` 表示助手与 QQ 接口断开。确认 NapCat 已登录、WebSocket 服务端启用且端口为 `3001`；重新登录方法见 README。
- **模型不可用**：检查 Harness 状态、API 凭据、模型名称和网络。固定指令可通过 `/help` 查看。
- **邮箱无通知**：检查授权码、服务器地址、网络和日志。IMAP 首次同步完成时会显示 `Mailbox 邮箱ID initialized`；首次同步不通知历史邮件。Microsoft Graph 不使用此日志标记。
- **提醒或通知未收到**：确认 QQ 在线和服务运行正常。发送失败会重试，极端情况下可能重复；邮箱连续三次检查失败及恢复时会通过 QQ 通知。

分享截图或日志前，请移除 Token、邮箱地址及聊天内容。QQ 登录可能失效或需要重新验证，具体取决于平台策略。

## 数据与隐私

`.env` 包含凭据，`config/mailboxes.json` 包含邮箱地址；`data/assistant` 保存待办和邮箱同步状态，`data/harness` 保存会话，`data/qq` 与 `data/napcat-*` 保存 QQ 登录或配置数据。备份应限制访问权限，不要公开这些内容。

邮箱流程不会将邮件正文发送给模型；用户主动发送的私聊文本、会话上下文、当前待办和未完成日期提醒快照会发送给模型服务。Harness 未启用模型可用的文件系统和 shell 工具。
