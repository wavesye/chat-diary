# Chat Diary

[English](README.md) | **简体中文**

一个本地优先的对话日记 Agent：通过 Web 或 Telegram 自然聊天，将当天内容整理成
Markdown 日记写入本地文件夹或 Obsidian，同时维护独立 Todo、完成活动、回顾日历和
两层长期记忆。

## 技术方案

项目沿用确定性的轻量工作流，没有引入 LangGraph：

- FastAPI 提供本地 HTTP API 和静态 Web UI。
- `DiaryService` 组织聊天、日记总结、Todo、活动与记忆服务。
- OpenAI-compatible Chat Completions 支持 OpenAI、DeepSeek、OpenRouter、Ollama、
  通义千问及自定义服务。
- SQLite 保存对话、日记索引、Todo、活动、提醒记录和长期记忆。
- 默认由当前 Python 进程内的 ONNX Runtime 运行 `all-MiniLM-L6-v2`，
  不需要独立 Embedding 服务或 API。
- SQLite 中的 FTS5/BM25 与 Embedding 精确余弦检索分别召回候选，再通过加权 RRF
  融合排序；不希望使用向量或本地模型无法初始化时，仍可使用纯 FTS5 模式。
- 可插拔渠道 Adapter 将平台消息统一转换后，由路由器查找内部用户。Telegram 使用
  长轮询，LINE 和 WhatsApp 使用签名校验的 Webhook。
- 日记通过临时文件原子替换写入 Markdown 目录，格式为 `YYYY-M-D-AIGEN.md`。

聊天回复与 Todo 识别是两条独立流程：聊天先正常完成，再由轻量规则筛选可能的任务消息，
必要时调用结构化模型提取。Todo 提取失败不会让正常对话失败。

## 安装与启动

需要 Python 3.10+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，每次启动前加载：

```bash
set -a
source .env
set +a
```

启动 Web UI：

```bash
uvicorn api:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。网页包含“今天的对话”和“回顾日历”；回顾日历只展示过去
实际完成的活动，不承担未来日程安排。

使用兼容的单渠道入口启动 Telegram Bot：

```bash
python3 telegram_bot.py
```

启动所有已启用的聊天渠道可使用 `python3 chat_channels.py`，配置见下文。
同一套配置和 Telegram Bot 只运行一个渠道进程。

也可以继续使用命令行聊天：

```bash
python3 main.py
```

## 基础配置

```env
OBSIDIAN_VAULT_PATH="/Users/your-name/Documents/Obsidian/MyVault"
OBSIDIAN_JOURNAL_FOLDER="日记/AI 日记"
DIARY_TIMEZONE="Asia/Shanghai"
DIARY_USER_NAME="你的名字"
DIARY_DB_PATH="data/diary.sqlite"

LLM_PROVIDER="openai"
OPENAI_API_KEY="sk-..."
OPENAI_MODEL="gpt-5-mini"
```

虽然沿用了 `OBSIDIAN_` 变量名，项目并不依赖 Obsidian API 或 Obsidian 应用。
`OBSIDIAN_VAULT_PATH` 可以是任意可写文件夹，`OBSIDIAN_JOURNAL_FOLDER` 是该目录内的
相对路径。路径含空格时用双引号包住即可。

默认启用 Python 进程内的本地 ONNX Hybrid 检索：

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="local"
# 可选的模型根目录，下面是默认位置：
# LOCAL_EMBEDDING_CACHE_DIR="/Users/your-name/.cache/chat-diary/onnx_models/all-MiniLM-L6-v2"
LOCAL_EMBEDDING_THREADS=2
MEMORY_TOP_K=5
MEMORY_VECTOR_MIN_SIMILARITY=0.1
```

第一次需要向量时，程序会自动下载一次 `all-MiniLM-L6-v2`。默认缓存在
`~/.cache/chat-diary/onnx_models/all-MiniLM-L6-v2`，也可通过
`LOCAL_EMBEDDING_CACHE_DIR` 改到其他位置。
如果 Bot 是后台常驻启动，建议为缓存目录配置绝对路径。模型文件准备好后，
Embedding 会完全从本地加载，不需要 Ollama、API Key 或网络。这里只指
Embedding；如果聊天或历史提炼使用远程大模型，它们仍然需要网络。
`LOCAL_EMBEDDING_THREADS` 同时控制 ONNX 的 intra-op 和 inter-op 线程数，默认为 `2`；
只建议在专用机器上谨慎调大。

设置 `MEMORY_SEARCH_MODE="keyword"` 或 `EMBEDDING_PROVIDER="none"` 可关闭向量检索，也不会
触发 ONNX 模型下载。`none` 会优先于旧 `.env` 中可能残留的远程 Embedding 变量。
在兼容向量尚未建好前，即使期望模式是 `hybrid`，`/status` 也可能暂时显示实际
检索模式为 `keyword`。

`all-MiniLM-L6-v2` 是一个偏英文的轻量句向量模型，单个窗口最多使用
256 个 word pieces。Chat Diary 会将长片段分成带重叠的窗口，并对归一化向量取平均，
不会直接丢弃第一个窗口之后的内容。FTS5 仍能完成中文精确匹配，但默认模型的中文
和跨语言语义召回会弱于英文。如果对中文语义检索要求较高，可保持 Hybrid 模式，
并通过 Ollama 或 OpenAI-compatible Provider 换用多语言 Embedding 模型。

Ollama 仍然可作为可选本地 Provider：

```bash
ollama pull embeddinggemma
```

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="ollama"
OLLAMA_EMBEDDING_MODEL="embeddinggemma"
# 可省略，下面是默认地址：
OLLAMA_EMBEDDING_BASE_URL="http://127.0.0.1:11434/v1"
```

也可选择 OpenAI 或其他兼容的 Embedding API：

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="openai-compatible"
EMBEDDING_BASE_URL="https://api.openai.com/v1"
EMBEDDING_API_KEY="sk-..."
EMBEDDING_MODEL="text-embedding-3-small"
```

旧 `.env` 如果已同时配置 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY` 和
`EMBEDDING_MODEL`，但没有 `EMBEDDING_PROVIDER`，升级后仍会自动选择
OpenAI-compatible Provider，不会静默换掉原有向量空间。

`MEMORY_SEARCH_MODE` 可设为 `hybrid`、`vector` 或 `keyword`。远程 Embedding 服务会收到
待向量化的文本批次；如果日记片段不能离开电脑，请使用默认的本地 Provider。
`MEMORY_VECTOR_MIN_SIMILARITY` 用于过滤相关性过低的语义候选，默认值为 `0.1`。

## 多聊天渠道与用户绑定

Telegram 已实现完整 Adapter，保留原有命令、按钮、消息合并、重试和提醒。LINE 与
WhatsApp 已实现官方文本收发和 Webhook 验签，使用前需要平台凭证与公网 HTTPS 回调。
微信与 QQ 目前只有配置接口和 Adapter 骨架；启用后会明确报出尚未实现的错误。

复制渠道配置，并在 `.env` 中指定路径：

```bash
cp channels.example.json channels.json
```

```env
CHANNELS_CONFIG=channels.json
```

```json
{
  "channels": {
    "telegram": {"enabled": true},
    "wechat": {"enabled": false},
    "qq": {"enabled": false},
    "line": {"enabled": false},
    "whatsapp": {"enabled": false}
  }
}
```

配置使用 JSON，无需添加 YAML 依赖。不设置 `CHANNELS_CONFIG` 时采用上述默认开关。
`CHANNEL_TELEGRAM_ENABLED`、`CHANNEL_WECHAT_ENABLED`、`CHANNEL_QQ_ENABLED`、
`CHANNEL_LINE_ENABLED`、`CHANNEL_WHATSAPP_ENABLED` 可分别用 `true` 或 `false` 覆盖开关。
凭证只通过环境变量读取，不写入此 JSON。关闭的渠道无需填写凭证。编辑后重新加载
`.env`，然后启动：

```bash
python3 chat_channels.py
```

### 身份解析与数据归属

`MessageRouter` 使用 `UserRegistry` 将 `(channel, external_user_id)` 查成内部 UUID，
再由 `ChatHandler` 调用 `ServicePool` 中现有的 `DiaryService`。各 Adapter 实现异步
`start()`、`stop()`、`send_message(user_id, text)`；发送接口的目标 ID 是该渠道中的
外部收件人 ID。统一 `IncomingMessage` 包含 `channel`、`external_user_id`、
`message_id`、`text`、含时区的 `timestamp`，以及区分文本与操作的 `kind`。
平台 API 和验签逻辑放在 `diary_agent/channels/` 中。

绑定库默认是 `DIARY_DB_PATH` 同目录下的 `channel_users.sqlite`，可用
`CHANNEL_BINDINGS_DB` 修改。通过本地 CLI 管理用户：`local` 输出原单用户数据所属的
内部 UUID，`create` 创建数据独立的新用户并输出其 UUID。

```bash
python3 manage_users.py local
python3 manage_users.py create --name "小明"
python3 manage_users.py bind --user-id INTERNAL_UUID --channel line --external-user-id LINE_USER_ID
python3 manage_users.py bind --user-id INTERNAL_UUID --channel whatsapp --external-user-id WHATSAPP_USER_ID
python3 manage_users.py list
python3 manage_users.py unbind --channel line --external-user-id LINE_USER_ID
```

把占位符替换成实际 ID。多个渠道绑定同一个 UUID，即可共享该用户的日记、Todo 和记忆。
管理员必须独立核验身份后再绑定；向 Bot 发送一个自称属于自己的 UUID 无法认领账户。
未绑定的消息不会创建业务记录。一个渠道身份只能绑定一个内部用户，改绑须先显式解绑；
解绑保留原日记数据。

本地拥有者继续使用原数据库、Vault 和历史导入目录，包含原 Web UI 与 CLI 数据。
`TELEGRAM_ALLOWED_USER_ID` 会自动将该 Telegram 身份绑定到本地拥有者，兼容旧配置。
其他用户的数据库位于原数据库父目录的 `users/INTERNAL_UUID/diary.sqlite`，日记位于
Vault 的 `Users/INTERNAL_UUID/`，默认关闭历史导入，不继承原用户的历史路径。
用户目录中的 `.chat-diary-user` 标记会阻止本地拥有者的历史扫描器导入这些私人子目录，
请保留该文件。备份时一起保存绑定库、用户数据库与 Vault，才能恢复身份和数据之间的对应关系。

### 各平台配置与完成范围

| 渠道 | 环境变量 | 官方平台准备与实现状态 |
| --- | --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`；可选 `TELEGRAM_ALLOWED_USER_ID`、`TELEGRAM_MESSAGE_DEBOUNCE_SECONDS` | 通过 [BotFather](https://core.telegram.org/bots/tutorial#obtain-your-bot-token) 创建 Bot；已实现私聊长轮询。 |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_CHANNEL_SECRET` | 创建 [LINE Official Account 并启用 Messaging API](https://developers.line.biz/en/docs/messaging-api/getting-started/)；已实现私聊文本与 Webhook 验签。 |
| WhatsApp | `WHATSAPP_ACCESS_TOKEN`、`WHATSAPP_PHONE_NUMBER_ID`、`WHATSAPP_APP_SECRET`、`WHATSAPP_VERIFY_TOKEN`、`WHATSAPP_API_VERSION` | 按 [官方 Cloud API 指南](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api) 配置 Meta 应用、WhatsApp Business Account 和业务号码；已实现私聊文本、回调验证与验签。 |
| 微信 | `WECHAT_APP_ID`、`WECHAT_APP_SECRET`、`WECHAT_TOKEN`；可选 `WECHAT_ENCODING_AES_KEY` | TODO：在[微信开发者平台](https://developers.weixin.qq.com/) 选择官方产品/账号并申请凭证，再按对应协议实现回调握手、验签、按需加解密、Token 刷新与文本收发；未实现个人微信登录。 |
| QQ | `QQ_APP_ID`、`QQ_APP_SECRET` | TODO：在 [QQ 开放平台](https://q.qq.com/) 创建并申请机器人权限，再按所选产品实现 Token 生命周期、事件订阅、验证与收发接口。 |

启用 LINE 或 WhatsApp 后，`chat_channels.py` 自动启动独立 Webhook 服务，默认监听
`CHANNEL_WEBHOOK_HOST=127.0.0.1`、`CHANNEL_WEBHOOK_PORT=8080`。通过 HTTPS 反向代理
仅公开相应回调，并在平台控制台登记：

- LINE：`https://your-domain.example/webhooks/line`。启用 Webhook，并配置用于验签的
  Channel Secret。
- WhatsApp：`https://your-domain.example/webhooks/whatsapp`。控制台填入与
  `WHATSAPP_VERIFY_TOKEN` 相同的验证字符串，订阅消息事件，并配置用于 POST 验签的
  App Secret。`WHATSAPP_API_VERSION` 必须自行按照 Meta 应用配置选择支持的版本，
  格式为 `vNN.N`。

回调只处理一对一文本，暂不处理群聊或媒体。发送日记消息前，由管理员把 LINE `userId`
或 WhatsApp 发送者 ID 绑定到内部用户。LINE/WhatsApp 通过文本命令操作；Telegram
内联按钮与定时提醒仍由 Telegram 提供。LINE 目前通过 Push API 发送，接收者须具备推送
资格，并会消耗账号消息配额；Reply Token 回复尚待实现，详见
[LINE Messaging API 文档](https://developers.line.biz/en/reference/messaging-api/#send-push-message)。
后续 TODO 包含更多消息类型、各平台交互控件、
LINE 配额与推送资格处理、WhatsApp 消息窗口/模板工作流、凭证轮换和持久化 Webhook
工作队列。平台权限与消息政策仍然适用，填写凭证不会自动获得额外权限。

路由器按内部用户串行处理消息，成功处理后记录消息 ID 来过滤重复投递。取消处理时会等待
已开始的业务流程（包括模型工作）结束，再释放用户锁并允许关闭存储。Telegram 推进
轮询 offset 前会把收到的 updates 存入绑定库中的持久化 inbox；重启后重放待处理消息，
包括仍在等待消息合并窗口的内容。

模型等非输入错误不会被标为处理成功。Webhook 处理失败时返回 HTTP 503，供平台重新
投递；Telegram 给出友好错误提示后，由用户重新发送消息来重试，不会自动反复执行失败的
整套业务流程。Webhook 目前仍同步等待模型处理，持久化 Webhook 工作队列与及时确认
接收尚待实现。

业务处理、平台回复与成功标记不是一个原子事务；处理中崩溃或部分成功后重试，仍可能产生
重复业务记录或回复，因此不保证 exactly-once。同一绑定库仅运行一个渠道进程，用户串行
锁只在当前进程内生效。

扩展平台时实现 `ChannelAdapter`、将入站消息转为 `IncomingMessage`，并在渠道运行器中
注册 Adapter 和环境变量配置。业务继续复用 `ChatHandler`/`DiaryService`；增加不访问
真实平台的 Adapter、路由与绑定测试即可。

## Telegram 与 Todo

在 `@BotFather` 创建 Bot 后配置：

```env
TELEGRAM_BOT_TOKEN="123456789:..."
TELEGRAM_ALLOWED_USER_ID=""
TELEGRAM_MESSAGE_DEBOUNCE_SECONDS=3

TODO_REMINDERS_ENABLED=true
TODO_MORNING_REMINDER_TIME="08:30"
TODO_EVENING_REMINDER_TIME="21:30"
```

首次把 `TELEGRAM_ALLOWED_USER_ID` 留空，启动 Bot 后发送 `/start`，Bot 会返回你的数字
user ID。填回 `.env` 并重启后，该身份会绑定到本地拥有者。其他用户只有经过管理员
使用 `manage_users.py` 显式绑定后才能操作。未绑定时，配对流程只返回发送者自己的 ID，
不会开放日记数据。

普通文字默认使用 3 秒静默窗口：连续发送的多条消息会分别保存，但只调用一次聊天模型、
回复一次并执行一次 Todo 提取。每收到一条新消息都会重新计时；命令和 inline keyboard
按钮不等待。如果更喜欢逐条回复，设置 `TELEGRAM_MESSAGE_DEBOUNCE_SECONDS=0`。

Telegram 命令：

- `/todo`：列出所有进行中的 Todo，并显示操作按钮。
- `/todo 添加 明天修改论文第二节`：创建 Todo。
- `/todo 完成 3`：完成编号 3，并生成当天 activity。
- `/todo 延期 3 后天`：修改计划日期，不改变截止时间。
- `/todo 修改 3 新标题`：修改标题。
- `/todo 取消 3`：取消任务，保留操作记录。
- `/today`：当天 Todo，包含过期未完成项。
- `/week`：未来七天 Todo。
- `/preview`：预览当天日记。
- `/done`：写入 Markdown 日记。
- `/memory`：查看长期记忆。
- `/status`：查看 Bot 实际加载的消息合并秒数和记忆检索模式。

Telegram 短暂断线和模型服务的临时超时会自动重试；“正在输入”状态发送失败不会中断
聊天或写日记。连续重试仍未恢复时，Bot 会按模型、Telegram 或文件写入错误给出明确提示，
而不是只显示空白的“处理失败”。收到处理失败提示后，请重新发送消息来重试；程序不会
无限自动重试整套业务流程。

也可以直接说：

- “我现在的 Todo list 有什么”——读取真实的进行中任务，不会误创建同名 Todo。
- “今天做 10 个俯卧撑，加入 Todo”——由本地任务路由直接创建，并返回数据库中的结果。
- “明天提醒我修改论文第二节”——明确创建并告知结果。
- “论文已经改完了”——完成匹配的 Todo；没有匹配项时记为当天已完成活动。
- “BMS 分析挪到后天”——匹配并延期。
- “我希望有空学陶艺”——属于模糊愿望，Bot 先显示“确认加入/取消”按钮。

中英文的创建、完成、延期、取消、相对日期、星期、英文月份和 `3pm` 等时间表达有本地
规则兜底。其他语言会在聊天回复完成后交给独立的结构化模型判断，并要求模型逐字返回
日期证据，避免凭空安排日期。这个多语言兜底默认开启；如果不希望普通的非中文消息多
产生一次模型调用，可以设置 `TODO_MULTILINGUAL_MODEL_FALLBACK=false`，此时仍保留完整的
中英文规则和明确命令支持。
只读列表查询和明确的本地任务指令优先于模型输出，因此模型不能把查询误变成创建，也不能
用 `none` 否决明确的创建或完成操作。聊天模型不会声称任务已经修改；只有 Todo 子系统在
数据库操作成功后才会报告结果。

Todo 的 `planned_date` 表示计划哪天做，`due_at` 表示用户明确说出的最晚完成时间；系统
不会因为出现计划日期就凭空制造截止时间。Todo 与长期记忆、日记正文分表保存。

早晚提醒只在 Bot 常驻运行时触发，使用 `DIARY_TIMEZONE`。发送成功后写入
`reminder_log`，所以程序重启不会重复发送同一时段的提醒。

## Activities 与回顾日历

`activities` 只保存已经完成或真实发生的活动，来源分为：

- `todo`：Todo 被标记完成。
- `chat`：用户在聊天中明确表示事情已经完成，或完成日记时模型从对话中提取。
- `obsidian`：历史日记中的完成项或显式配置模型提取的已发生事件。

未完成 Todo、普通愿望和建议不会进入回顾日历。日历点击某天后会显示日记标题、已完成
Todo、其他活动和主题标签；未来日期不可打开。

## 导入 Obsidian 历史日记

先设置只读扫描目录，可以是整个 Vault，也可以是某个日记子目录：

```env
OBSIDIAN_HISTORY_PATH="/Users/your-name/Documents/Obsidian/MyVault/日记"
```

第一步建立本地原文索引：

```bash
python3 import_history.py scan
python3 import_history.py embed --max-batches 20 --batch-size 32
python3 import_history.py status
python3 import_history.py search "第一次开始 BMS 项目"
```

在默认本地 Provider 下，第一次生成向量时会自动下载并校验 ONNX 模型，所以
首次可能需要多等一会儿。之后会使用本地缓存，向量生成完全在 Python 进程内完成。
`scan` 会为新增或修改的日记建立向量；可重复执行的 `embed` 命令用于给以前只建立了
关键词索引的日记补建向量。

扫描器会：

1. 优先从 YAML frontmatter 的 `date`、`day`、`created` 或 `created_at` 读取日期；
2. 找不到时再从 `YYYY-M-D`、`YYYY_M_D` 或 `YYYY年M月D日` 文件名读取；
3. 保存原文文档和约 1600 字符的上下文分片，建立 FTS5，并在配置了 Embedding 时分批
   建立向量；
4. 使用 SHA-256 内容哈希跳过未改变文件，只重建新增或修改过的文件；
5. 将 Markdown 中明确勾选的 `- [x]` 项目导入 activity；
6. 只读打开原 Markdown，不修改、重命名或移动原文件。

原文文档和片段是第一层记忆，用于找回具体往事和上下文。

如需继续或显式补建历史向量：

```bash
python3 import_history.py embed --max-batches 20 --batch-size 32
python3 import_history.py status
```

每一批都会独立写入 SQLite；中断后重复运行同一命令，直到 `pending_embeddings` 为 `0`。
更换 Embedding 地址或模型后，签名会随之变化，再运行一次即可增量替换不兼容的
旧向量。普通聊天检索不会再临时触发全量历史向量化。`status` 会显示期望模式、实际模式、
已完成/待补建的向量数以及当前 Embedding 签名。

第二层人物、项目、目标、偏好、习惯、重要事件与阶段经历只在明确配置提炼模型后运行：

```env
HISTORY_LLM_MODEL="gpt-5-mini"
# 不设置下面两项时继承当前聊天模型的服务地址和 Key：
HISTORY_LLM_BASE_URL="https://api.openai.com/v1"
HISTORY_LLM_API_KEY="sk-..."
```

然后执行：

```bash
python3 import_history.py scan
python3 import_history.py extract --max-batches 20 --batch-size 5
```

提炼按少量分片分批发送，绝不会把多年日记一次性提交给模型。每批后都保存游标；中断后
重复运行同一命令即可继续。记忆会记录来源文件、日记日期、时间范围、置信度和
`historical/current/uncertain` 状态。未配置 `HISTORY_LLM_MODEL` 时，`extract` 会拒绝
运行，但第一层本地检索仍然可用。

## HTTP API

主要接口：

- 日记：`GET /v1/session`、`POST /v1/chat`、`POST /v1/preview`、
  `POST /v1/finalize`、`DELETE /v1/messages/{id}`。
- Todo：`GET/POST /v1/todos`、`PATCH/DELETE /v1/todos/{id}`、
  `POST /v1/todos/{id}/confirm|complete|postpone|cancel`。
- 记忆：`GET /v1/memories`、`DELETE /v1/memories/{id}`。
- 回顾：`GET /v1/review/month?month=YYYY-MM`、`GET /v1/review/day/{day}`。
- 历史：`GET /v1/history/status|search`、`POST /v1/history/scan|embed|extract`。

完整参数和请求示例可在 `http://127.0.0.1:8000/docs` 查看。服务默认只应监听本机。

## 数据库迁移与备份

应用启动时执行增量迁移：只创建新表、索引，并为现有表补充列，不会删除或重建已有
`messages`、`entries`、`memories`、`memory_sources` 和 `app_state`。渠道运行器首次迁移时
读取旧 Telegram offset，之后将渠道状态、待处理的 Telegram updates 与成功消息记录
保存到绑定库。待处理 updates 可能包含消息正文，请将绑定库一起纳入日记备份。
原日记记录仍归属于内部的本地拥有者。

新增表包括 `todos`、`todo_events`、`activities`、`reminder_log`、
`historical_documents`、`historical_chunks`、`historical_chunk_fts` 和
`historical_memory_evidence`。首次升级前仍建议复制一份 `data/diary.sqlite` 作为备份。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时 SQLite、虚拟模型和虚拟渠道 API，覆盖消息路由、用户绑定与隔离、Telegram
兼容性、渠道配置以及 LINE/WhatsApp 签名消息，不访问真实平台或真实 Obsidian Vault。
