# 部署与故障排查

首次安装见 [README 的快速开始](../README.md#快速开始)。以下命令均在项目目录执行。

## 备份与更新

更新前先备份配置和数据。备份期间服务会短暂停止，完成后自动启动。归档含有凭据，保存在当前用户的主目录中。

<details>
<summary>备份命令</summary>


```bash
(
  set -eu
  umask 077
  version=$(git rev-parse --short HEAD)
  backup=$(mktemp "$HOME/qq-assistant-${version}-XXXXXXXX.tar.gz")
  sudo -v
  trap 'docker compose start' EXIT
  docker compose stop assistant harness
  sudo tar -czf - .env config/mailboxes.json data/assistant data/harness compose.yaml > "$backup"
  tar -tzf "$backup" >/dev/null
  echo "备份已校验：$backup"
)
```

</details>

备份成功后，运行以下命令从 GitHub 拉取当前分支的更新，并重新构建和启动服务。

```bash
git pull --ff-only && docker compose up -d --build
docker compose ps
```

回退时，先停止两个服务，另存当前的数据目录，再恢复归档中的配置和数据。检出备份文件名记录的 Git 版本后，执行 `docker compose up -d --build`。从备份恢复会把数据还原到备份时刻。

## 检查状态

```bash
docker compose ps
docker compose logs --tail=50 assistant harness
```

| 现象 | 处理 |
| --- | --- |
| QQ 无回复 | 在助手日志中找 `connected=True blocked=False`。检查 AppID、AppSecret、OpenID 和平台私聊权限。同一 AppID 只运行一个连接实例。 |
| 插件启动失败或提示 HTTP 409 | 检查两项服务使用相同的 `HARNESS_AUTH_TOKEN`、AppID 和绑定账号。修改 `.env` 后执行 `docker compose up -d --force-recreate`。 |
| 能聊天，收不到提醒 | 检查是否允许机器人主动发送消息，以及平台的主动消息额度。遇到 `will retry` 会自动重试。遇到 `delivery paused`，修复日志所指的问题后执行 `docker compose restart assistant`。 |
| 助手提示重新描述 | 这次模型回复无法解析或操作参数无效。重新描述事项，或发送 `/help` 使用固定指令。 |
| 助手提示无法连接大模型 | 检查 Harness 日志、DeepSeek API Key、模型名称和网络。QQ 插件在线时仍可使用固定指令。 |
| 邮箱无通知 | IMAP 首次连接成功会记录 `Mailbox 邮箱ID initialized`。连续三次检查失败及恢复时都会发送 QQ 通知，失败原因见助手日志。 |

日志出现 `UIDVALIDITY changed` 时，把下面的 `school` 换成受影响邮箱的 `id`。下次同步会将该收件箱当时已有的邮件作为起点，之后的新邮件才会通知。

```bash
docker compose exec assistant python -c 'import os, sqlite3, sys
with sqlite3.connect(os.environ["DB_PATH"]) as db:
    print("已重置的邮箱数", db.execute("DELETE FROM mailbox_state WHERE id = ?", (sys.argv[1],)).rowcount)' school
```

更换机器人时，先停止 `assistant`，更新 AppID 和 AppSecret，清空 `QQ_BOT_OWNER_OPENID`，然后重新执行[绑定步骤](../README.md#2-绑定自己的-qq)。同一人换机器人可沿用待办、邮箱进度和会话，旧聊天中的列表序号及待确认操作会失效。

## 数据与权限

- `.env` 保存机器人、邮箱和模型凭据。真实邮箱配置在 `config/mailboxes.json`。
- `data/assistant` 保存 SQLite 数据库。`data/harness` 保存对话、待交接的私聊消息和发送回执。
- 模型接收绑定账号的私聊文字，以及最多各 30 条待办和日期提醒。邮箱轮询独立运行。模型没有文件或 Shell 工具。

## 插件与测试

项目使用腾讯官方 `@tencent-connect/dsh-qqbot` 处理 QQ 连接，适配层将私聊文字交给助手。版本由 `package-lock.json` 锁定。更换插件版本时，检查 `patch.py` 的接入位置并运行完整测试。

以下命令使用 Python 3.12 和 Node.js 22 或 24。联调会启动真实插件和 Harness，QQ 与模型接口由回环地址上的测试服务提供。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
(cd harness_runtime/qq_plugin && npm ci --omit=dev --ignore-scripts --legacy-peer-deps)
.venv/bin/python harness_runtime/qq_plugin/patch.py
node --test tests/test_qq_plugin.mjs
.venv/bin/python -m unittest discover -s tests -v
```
