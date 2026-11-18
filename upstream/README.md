# mulerun-pool

MuleRun 批量注册 + API Key 代理池的一体化开源版本。

本仓库把两个本地项目收口到一个最小可维护结构：

- `mulerun_register`：基于浏览器自动化的 MuleRun 邀请注册流程，邮箱来源统一改为 Outlook 批量账号文件。
- `mulerun_pool`：OpenAI-compatible / Anthropic-compatible 的 API Key 轮询代理池，可接入 new-api / one-api / OpenClaw 等网关。

> 本仓库不包含任何真实账号、密码、API Key、Cookie、token 或私有邮箱域名接入。请只在你拥有授权的账号和服务上使用。

## 目录结构

```text
.
├── src/
│   ├── mulerun_pool/             # FastAPI API Key 代理池
│   │   ├── server.py
│   │   ├── new_api_common.py
│   │   └── __main__.py
│   └── mulerun_register/         # Outlook 邮箱批注册流程
│       ├── register.py
│       ├── config.py
│       ├── email_client.py
│       ├── browser_helpers.py
│       └── providers/outlook_provider.py
├── scripts/
│   ├── mulerun_quota_cli.py
│   └── new_api_mulerun_pool_watch.py
├── examples/
│   ├── accounts.outlook.example.csv
│   └── credentials.example.json
├── .env.example
└── requirements.txt
```

## 安装

推荐 Python 3.11+。

```bash
git clone https://github.com/<your-user>/mulerun-pool.git
cd mulerun-pool
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium || true
```

如果你只运行代理池，`playwright/nodriver/curl_cffi/aiohttp` 不是强制依赖；如果要跑注册流程，需要完整安装 `requirements.txt`。

## 配置 Outlook 批量邮箱

复制示例文件：

```bash
cp examples/accounts.outlook.example.csv accounts.outlook.csv
```

CSV 字段：

```csv
email,password,status,invite_code,api_key
user1@outlook.com,account-password,pending,,
```

说明：

- `email` / `password`：Outlook 邮箱账号和密码。
- `status`：建议使用 `pending`、`confirmed`、`failed`。
- `invite_code`：注册后可记录来源邀请或运行批次。
- `api_key`：后续如果从 MuleRun 取到 key，可回填到这里。

当前开源版已删除原来的域名邮箱 / Cloudflare Worker / D1 临时邮箱接入，所有注册邮箱都从 `MR_OUTLOOK_ACCOUNTS` 指定的 CSV 中读取。

## 运行注册流程

创建环境文件：

```bash
cp .env.example .env
# 编辑 .env，至少配置 MR_INVITE_URL / MR_OUTLOOK_ACCOUNTS / MR_DEFAULT_PASSWORD
```

单账号测试：

```bash
set -a && source .env && set +a
python -m mulerun_register --count 1 --concurrency 1
```

批量运行：

```bash
MR_HEADLESS=1 python -m mulerun_register --count 5 --concurrency 2
```

如果需要非 headless + Xvfb：

```bash
MR_HEADLESS=0 xvfb-run --auto-servernum --server-args='-screen 0 1920x1080x24' \
  python -m mulerun_register --count 1 --concurrency 1
```

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MR_INVITE_URL` | `https://mulerun.com/invitation/YOUR_INVITE_CODE` | MuleRun 邀请链接 |
| `MR_OUTLOOK_ACCOUNTS` | `accounts.outlook.csv` | Outlook 账号 CSV |
| `MR_DEFAULT_PASSWORD` | `change-me` | 新注册账号默认密码 |
| `MR_CONCURRENCY` | `2` | 注册并发 |
| `MR_HEADLESS` | 自动 | 浏览器是否 headless |
| `MR_PROXY_SERVER` | 空 | 浏览器代理，例如 `socks5://127.0.0.1:17920` |
| `MR_VERIFICATION_TIMEOUT` | `60` | 验证码等待时间 |

## 运行 API Key 代理池

准备 key 文件：

```bash
cp examples/credentials.example.json credentials.json
```

`credentials.json` 示例：

```json
{
  "api_keys": [
    "mr-key-1",
    "mr-key-2"
  ]
}
```

启动：

```bash
PROXY_API_KEY='change-this-admin-key' \
CREDENTIALS_FILE=./credentials.json \
STATE_FILE=./state.json \
python -m mulerun_pool
```

默认监听：`0.0.0.0:8089`。

健康检查：

```bash
curl http://127.0.0.1:8089/health
```

OpenAI-compatible 调用示例：

```bash
curl http://127.0.0.1:8089/v1/chat/completions \
  -H "Authorization: Bearer change-this-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-6","messages":[{"role":"user","content":"reply OK"}],"stream":false}'
```

## 接入 new-api / one-api

在 new-api 新增渠道时可使用：

- Base URL: `http://127.0.0.1:8089`
- Key: `PROXY_API_KEY` 的值
- Models: 按你的 MuleRun 上游模型配置，例如 `claude-opus-4-6`

代理池会在本地多个 MuleRun key 间做轮询、失败熔断和恢复重试。

## 安全说明

- 不要提交 `credentials.json`、`accounts.outlook.csv`、`.env`、`state.json`。
- 不要把真实 Cookie、JWT、Bearer token、API Key 写进代码。
- 开源前请执行：

```bash
grep -RInE 'eyJ|sk-|api[_-]?key|token|password|secret|cookie' . --exclude-dir=.git
```

## License

MIT
