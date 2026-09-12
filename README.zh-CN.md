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
- SQLite 中的 FTS5/BM25 与 Embedding 精确余弦检索分别召回候选，再通过加权 RRF
  融合排序；没有配置 Embedding 时自动退化为 FTS5 关键词检索。
- Telegram Bot API 使用长轮询，无需公网域名。
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

启动 Telegram Bot：

```bash
python3 telegram_bot.py
```

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

Embedding 配置是可选的。使用任意 OpenAI-compatible 服务时：

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="openai-compatible"
EMBEDDING_BASE_URL="https://api.openai.com/v1"
EMBEDDING_API_KEY="sk-..."
EMBEDDING_MODEL="text-embedding-3-small"
MEMORY_TOP_K=5
MEMORY_VECTOR_MIN_SIMILARITY=0.1
```

使用 Ollama 完全在本机生成向量时：

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

中英文混合日记应选择多语言 Embedding 模型。`MEMORY_SEARCH_MODE` 可设为 `hybrid`、
`vector` 或 `keyword`。默认期望使用 `hybrid`，但没有可用 Embedding 时，实际模式会明确
显示为 `keyword`；配置了模型但还没有兼容的已存向量时也一样。无 Embedding 时，日记解析、
分片、哈希和 FTS5 索引仍全部在本地完成；远程 Embedding 服务会收到当前待向量化的文本批次，
不希望片段离开电脑时请使用 Ollama。
`MEMORY_VECTOR_MIN_SIMILARITY` 用于过滤相关性过低的语义候选，默认值为 `0.1`。

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
user ID。填回 `.env` 并重启后，只有这个用户可以操作。

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
而不是只显示空白的“处理失败”。

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
python3 import_history.py status
python3 import_history.py search "第一次开始 BMS 项目"
```

扫描器会：

1. 优先从 YAML frontmatter 的 `date`、`day`、`created` 或 `created_at` 读取日期；
2. 找不到时再从 `YYYY-M-D`、`YYYY_M_D` 或 `YYYY年M月D日` 文件名读取；
3. 保存原文文档和约 1600 字符的上下文分片，建立 FTS5，并在配置了 Embedding 时分批
   建立向量；
4. 使用 SHA-256 内容哈希跳过未改变文件，只重建新增或修改过的文件；
5. 将 Markdown 中明确勾选的 `- [x]` 项目导入 activity；
6. 只读打开原 Markdown，不修改、重命名或移动原文件。

原文文档和片段是第一层记忆，用于找回具体往事和上下文。

如果旧日记是在配置 Embedding 之前扫描的，需要显式补建向量：

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
`messages`、`entries`、`memories`、`memory_sources` 和 `app_state`。Telegram offset 继续
使用原来的 `app_state` key。

新增表包括 `todos`、`todo_events`、`activities`、`reminder_log`、
`historical_documents`、`historical_chunks`、`historical_chunk_fts` 和
`historical_memory_evidence`。首次升级前仍建议复制一份 `data/diary.sqlite` 作为备份。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时 SQLite、虚拟模型和虚拟 Telegram API，不访问真实模型、Telegram 或真实
Obsidian Vault。
