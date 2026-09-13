# Chat Diary

**English** | [简体中文](README.zh-CN.md)

A local-first conversational diary agent. Chat naturally through the Web UI or Telegram,
turn the day into a Markdown journal entry, and keep todos, completed activities, a review
calendar, and two-layer long-term memory in one lightweight application.

## Architecture

Chat Diary uses a deterministic, lightweight workflow and does not require LangGraph:

- FastAPI provides the local HTTP API and static Web UI.
- `DiaryService` coordinates conversations, journal generation, todos, activities, and memory.
- OpenAI-compatible Chat Completions support OpenAI, DeepSeek, OpenRouter, Ollama, Qwen, and
  custom providers.
- SQLite stores conversations, journal indexes, todos, activities, reminder history, and
  long-term memory.
- By default, `all-MiniLM-L6-v2` runs through ONNX Runtime inside the Python process. It needs
  neither a separate embedding server nor an embedding API.
- FTS5/BM25 and exact cosine embedding search run independently over SQLite-backed indexes;
  weighted reciprocal rank fusion combines their candidates. Keyword-only mode remains
  available when vector retrieval is not wanted or cannot be initialized.
- Pluggable channel adapters normalize platform messages before a shared router resolves their
  internal user. Telegram uses long polling; LINE and WhatsApp use signed webhooks.
- Journal files are written atomically and named `YYYY-M-D-AIGEN.md`.

Chat replies and todo extraction run as separate paths. The conversation completes first;
lightweight rules then identify possible task-related messages, with structured model extraction
when needed. A todo extraction failure never prevents the normal chat reply.

## Installation and startup

Python 3.10 or later is required:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`, then load it before starting the application:

```bash
set -a
source .env
set +a
```

Start the Web UI:

```bash
uvicorn api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The Web UI includes today's conversation and a review calendar.
The calendar only shows activities that actually happened in the past; it is not a future
scheduling interface.

Start the Telegram bot with the compatible single-channel entry point:

```bash
python3 telegram_bot.py
```

To start all enabled chat channels, use `python3 chat_channels.py`; see the configuration below.
Run only one channel process for a given configuration and Telegram bot.

The command-line chat interface is also available:

```bash
python3 main.py
```

## Core configuration

```env
OBSIDIAN_VAULT_PATH="/Users/your-name/Documents/Obsidian/MyVault"
OBSIDIAN_JOURNAL_FOLDER="Journal/AI Journal"
DIARY_TIMEZONE="Asia/Shanghai"
DIARY_USER_NAME="Your name"
DIARY_DB_PATH="data/diary.sqlite"

LLM_PROVIDER="openai"
OPENAI_API_KEY="sk-..."
OPENAI_MODEL="gpt-5-mini"
```

Despite the legacy `OBSIDIAN_` variable names, Chat Diary does not require the Obsidian app or
an Obsidian API. `OBSIDIAN_VAULT_PATH` may point to any writable local directory, while
`OBSIDIAN_JOURNAL_FOLDER` is a relative directory inside it. Wrap paths containing spaces in
double quotes.

Hybrid retrieval uses an in-process local ONNX model by default:

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="local"
# Optional model root. The default is shown below:
# LOCAL_EMBEDDING_CACHE_DIR="/Users/your-name/.cache/chat-diary/onnx_models/all-MiniLM-L6-v2"
LOCAL_EMBEDDING_THREADS=2
MEMORY_TOP_K=5
MEMORY_VECTOR_MIN_SIMILARITY=0.1
```

The first operation that needs a vector downloads `all-MiniLM-L6-v2` once. By default it is
cached under `~/.cache/chat-diary/onnx_models/all-MiniLM-L6-v2`;
`LOCAL_EMBEDDING_CACHE_DIR` can move that model root elsewhere.
Use an absolute cache path when the bot is launched as a background service from an uncertain
working directory. Once the model files are present, embedding generation loads entirely from
local disk and does not need Ollama, an API key, or network access. This applies to embeddings
only: a remotely configured chat or history-extraction model still needs its own network access.
`LOCAL_EMBEDDING_THREADS` controls both ONNX intra-op and inter-op worker counts and defaults to
`2`; increase it cautiously on a dedicated machine.

Set `MEMORY_SEARCH_MODE="keyword"` or `EMBEDDING_PROVIDER="none"` to disable vector retrieval and
avoid downloading the ONNX model. `none` takes precedence over stale remote embedding variables
that may remain in an older `.env`. Until compatible vectors have been created, `/status` may
show the effective retrieval mode as `keyword` even though the requested mode is `hybrid`.

`all-MiniLM-L6-v2` is a compact English sentence encoder with a 256-word-piece context window.
Chat Diary processes longer chunks as overlapping windows and averages their normalized vectors,
so text after the first window is not discarded. FTS5 still provides exact Chinese retrieval,
but local semantic recall is stronger for English than Chinese. For higher-quality Chinese or
cross-language semantic recall, keep hybrid search enabled and select a multilingual model
through Ollama or an OpenAI-compatible provider.

Ollama remains available as an optional local provider:

```bash
ollama pull embeddinggemma
```

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="ollama"
OLLAMA_EMBEDDING_MODEL="embeddinggemma"
# Optional; this is the default:
OLLAMA_EMBEDDING_BASE_URL="http://127.0.0.1:11434/v1"
```

OpenAI and other compatible embedding APIs are also supported:

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="openai-compatible"
EMBEDDING_BASE_URL="https://api.openai.com/v1"
EMBEDDING_API_KEY="sk-..."
EMBEDDING_MODEL="text-embedding-3-small"
```

Existing `.env` files that contain the three legacy `EMBEDDING_BASE_URL`,
`EMBEDDING_API_KEY`, and `EMBEDDING_MODEL` values but omit `EMBEDDING_PROVIDER` continue to select
the OpenAI-compatible provider, so upgrading does not silently replace their embedding space.

`MEMORY_SEARCH_MODE` accepts `hybrid`, `vector`, or `keyword`. A remote embedding endpoint
receives the text batches being embedded; use the default local provider when journal fragments
must not leave the machine. `MEMORY_VECTOR_MIN_SIMILARITY` filters weak semantic matches and
defaults to `0.1`.

## Chat channels and user bindings

Telegram is a working adapter with the existing commands, buttons, debounce, retries, and
reminders. LINE and WhatsApp provide official text transport and webhook signature verification;
they need platform credentials and a public HTTPS callback to use. WeChat and QQ currently have
configuration and adapter skeletons only. Enabling either fails explicitly because its transport
is still TODO.

Copy the channel configuration and set its path in `.env`:

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

The format is JSON, with no YAML dependency. Omitting `CHANNELS_CONFIG` uses the switches shown
above. `CHANNEL_TELEGRAM_ENABLED`, `CHANNEL_WECHAT_ENABLED`, `CHANNEL_QQ_ENABLED`,
`CHANNEL_LINE_ENABLED`, and `CHANNEL_WHATSAPP_ENABLED` override individual switches; use `true`
or `false`. Credentials belong in environment variables, never in this JSON file. Disabled
channels do not require credentials. Reload `.env` after editing it, then start:

```bash
python3 chat_channels.py
```

### Identity and data ownership

`MessageRouter` resolves `(channel, external_user_id)` through `UserRegistry` into an opaque
internal UUID. `ChatHandler` then calls the existing `DiaryService` from `ServicePool`. Each
adapter implements async `start()`, `stop()`, and `send_message(user_id, text)`; the send target
is the recipient's external ID for that adapter. Incoming messages have `channel`,
`external_user_id`, `message_id`, `text`, and a timezone-aware `timestamp`, plus an optional
`kind` for text or actions. Platform APIs and signatures stay in `diary_agent/channels/`.

The binding database defaults to `channel_users.sqlite` beside `DIARY_DB_PATH`; set
`CHANNEL_BINDINGS_DB` to override it. Manage identities locally, using the UUID printed by
`local` for existing single-user data or by `create` for a separate user:

```bash
python3 manage_users.py local
python3 manage_users.py create --name "Alice"
python3 manage_users.py bind --user-id INTERNAL_UUID --channel line --external-user-id LINE_USER_ID
python3 manage_users.py bind --user-id INTERNAL_UUID --channel whatsapp --external-user-id WHATSAPP_USER_ID
python3 manage_users.py list
python3 manage_users.py unbind --channel line --external-user-id LINE_USER_ID
```

Replace the placeholders with actual IDs. Binding two channels to the same UUID shares that
user's diary, todos, and memory. An administrator must independently verify identity before
binding; sending a claimed UUID to a bot cannot claim an account. Unbound messages never create
business records. Each external identity can belong to only one internal user; changing its
owner requires an explicit unbind. Unbinding preserves diary data.

The local owner keeps the existing database, vault, and historical import path, including Web
UI and CLI data. `TELEGRAM_ALLOWED_USER_ID` automatically binds that Telegram identity to the
local owner for compatibility. Other users get `users/INTERNAL_UUID/diary.sqlite` beside the
original database and `Users/INTERNAL_UUID/` inside the vault, with historical import disabled.
Their `.chat-diary-user` marker prevents the local owner's history scanner from importing these
private subdirectories; retain this marker. Back up the binding database together with user
databases and vaults so their internal identities remain recoverable.

### Platform setup and current limits

| Channel | Environment variables | Official setup and implementation status |
| --- | --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN`; optional `TELEGRAM_ALLOWED_USER_ID`, `TELEGRAM_MESSAGE_DEBOUNCE_SECONDS` | Create a bot with [BotFather](https://core.telegram.org/bots/tutorial#obtain-your-bot-token). Private chats through long polling are implemented. |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` | Create a [LINE Official Account and enable Messaging API](https://developers.line.biz/en/docs/messaging-api/getting-started/). Direct text messages and signed webhooks are implemented. |
| WhatsApp | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_API_VERSION` | Set up a Meta app, WhatsApp Business Account, and business phone number using the [official Cloud API guide](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api). Direct text messages, callback verification, and signed webhooks are implemented. |
| WeChat | `WECHAT_APP_ID`, `WECHAT_APP_SECRET`, `WECHAT_TOKEN`; optional `WECHAT_ENCODING_AES_KEY` | TODO: choose an official product/account and obtain credentials through the [WeChat developer platform](https://developers.weixin.qq.com/). Implement its callback handshake, signature verification, encryption if applicable, token refresh, and message transport. Personal WeChat login is not implemented. |
| QQ | `QQ_APP_ID`, `QQ_APP_SECRET` | TODO: create an approved bot at the [QQ Open Platform](https://q.qq.com/), then implement its token lifecycle, event subscriptions, verification, and send/receive APIs for the chosen bot product. |

For LINE or WhatsApp, `chat_channels.py` automatically runs a separate webhook server on
`CHANNEL_WEBHOOK_HOST=127.0.0.1` and `CHANNEL_WEBHOOK_PORT=8080`. Configure an HTTPS reverse proxy
to expose only the relevant callbacks and register them in the platform console:

- LINE: `https://your-domain.example/webhooks/line`; enable webhooks and configure the channel
  secret used to validate the signature.
- WhatsApp: `https://your-domain.example/webhooks/whatsapp`; use the same
  `WHATSAPP_VERIFY_TOKEN` in the console, subscribe to message events, and set the app secret
  used to validate POST signatures. Set `WHATSAPP_API_VERSION` explicitly to a supported version
  from your Meta application settings, in the form `vNN.N`.

These callbacks accept direct text conversations, not groups or media. Bind the LINE `userId`
or WhatsApp sender ID to an internal user before sending diary messages. LINE/WhatsApp use text
commands; Telegram inline controls and scheduled reminders remain Telegram capabilities.
LINE currently sends via the push API, which requires an eligible recipient and uses the
account's message quota; reply-token delivery is TODO. See the [LINE Messaging API reference](https://developers.line.biz/en/reference/messaging-api/#send-push-message).
TODOs include richer message types, platform-specific interaction controls, LINE quotas and
push-message eligibility handling, WhatsApp messaging-window/template workflows, credential
rotation, and a persistent webhook work queue. Platform permissions and messaging policies
still apply; setting credentials does not grant those permissions.

The router serializes work per internal user and records successful processing to suppress
redelivery. On cancellation, it waits for an in-flight business workflow, including model work,
before releasing the user lock and allowing storage to close. Telegram saves received updates
in a persistent inbox in the binding database before advancing its polling offset; pending
updates, including messages waiting for the debounce window, are replayed after restart.

Model and other non-input failures are not marked successfully processed. Webhook processing
returns HTTP 503 so the platform can redeliver; Telegram sends a helpful error and asks the user
to resend instead of automatically replaying that failed workflow. Webhooks still wait for
model work synchronously: a persistent webhook queue and prompt acknowledgement remain TODO.

Business processing, reply delivery, and receipt recording are not one atomic transaction. A
crash or partial success can cause repeated business records or replies on retry; this is not
an exactly-once guarantee. Use one process for a shared binding database; the per-user
processing locks are in-process.

To add another platform, implement `ChannelAdapter`, normalize incoming identities into
`IncomingMessage`, and register the adapter and environment configuration in the channel
runtime. Business logic stays in `ChatHandler`/`DiaryService`; add transport, router, and binding
tests without calling real platform APIs.

## Telegram and todos

Create a bot with `@BotFather`, then configure:

```env
TELEGRAM_BOT_TOKEN="123456789:..."
TELEGRAM_ALLOWED_USER_ID=""
TELEGRAM_MESSAGE_DEBOUNCE_SECONDS=3

TODO_REMINDERS_ENABLED=true
TODO_MORNING_REMINDER_TIME="08:30"
TODO_EVENING_REMINDER_TIME="21:30"
```

Leave `TELEGRAM_ALLOWED_USER_ID` empty on the first run and send `/start` to the bot. It will
return your numeric user ID. Add that value to `.env` and restart to bind it to the local owner.
Other users can interact only after an administrator explicitly binds their Telegram identities
with `manage_users.py`. Without bindings, pairing only reveals the sender's ID and grants no
access to diary data.

Regular text uses a three-second quiet window by default. Consecutive messages are stored as
separate records, but they produce one model call, one reply, and one todo extraction pass. Each
new message restarts the timer. Commands and inline keyboard actions do not wait for the timer.
Set `TELEGRAM_MESSAGE_DEBOUNCE_SECONDS=0` to restore one reply per message.

Telegram commands:

- `/todo` — list active todos with inline action buttons.
- `/todo add Revise section two tomorrow` — create a todo.
- `/todo done 3` — complete todo 3 and record it as today's activity.
- `/todo postpone 3 next Friday` — change the planned date without changing the deadline.
- `/todo edit 3 New title` — rename a todo.
- `/todo cancel 3` — cancel a todo while retaining its event history.
- `/today` — show today's todos, including overdue unfinished items.
- `/week` — show todos planned for the next seven days.
- `/preview` — preview today's journal entry.
- `/done` — write today's Markdown journal entry.
- `/memory` — list long-term memories.
- `/status` — show the debounce window and effective memory/search modes loaded by the bot.

Temporary Telegram disconnects and transient model errors are retried automatically. A failed
typing indicator never interrupts the conversation or journal generation. If retries are
exhausted, the bot distinguishes model, Telegram, and file-system failures instead of returning
an empty generic error. After a reported processing failure, resend the message to retry;
the whole business workflow is not automatically retried indefinitely.

Natural-language examples:

- “What is on my todo list?” — lists the real active todos without creating a new one.
- “Do 10 push-ups today, add this to my todo list” — creates the todo through the local task
  router and returns the database-backed result without an extra conversational claim.
- “Remind me tomorrow to revise section two” — creates the todo immediately and reports it.
- “I finished revising the paper” — completes a matching todo, or records a completed activity
  when no match exists.
- “Postpone the BMS analysis until next Friday” — finds and reschedules the matching todo.
- “I might learn pottery someday” — treats this as an ambiguous wish and asks for confirmation.

Local rules cover common Chinese and English create, complete, postpone, and cancel expressions,
as well as relative dates, weekdays, English month names, and times such as `3pm`. Other languages
fall back to an independent structured model after the chat response. The model must quote date
evidence verbatim from the source message, preventing invented dates. This fallback is enabled by
default; set `TODO_MULTILINGUAL_MODEL_FALLBACK=false` to avoid an extra model call for ordinary
non-Chinese messages while retaining Chinese, English, and explicit-command support.
Read-only list questions and explicit local task instructions take precedence over model output,
so a model cannot turn a list question into a write or veto a clear create/complete operation. The
conversation model never claims a task was changed; only the task subsystem reports successful
database mutations.

`planned_date` means the day the user intends to work on a todo. `due_at` means the latest time
the user explicitly said it must be finished. A planned date never creates an implicit deadline.
Todos, long-term memories, and journal content are stored separately.

Morning and evening reminders run only while the bot is online and use `DIARY_TIMEZONE`.
Successful reminders are recorded in `reminder_log`, preventing duplicates after a restart.

## Activities and the review calendar

The `activities` table contains only completed or observed activities:

- `todo` — a todo was marked complete.
- `chat` — the user explicitly said an activity was completed, or the journal summary extracted
  it from the conversation.
- `obsidian` — an activity was imported from a historical journal or extracted by an explicitly
  configured model.

Unfinished todos, vague wishes, and suggestions never appear in the review calendar. Selecting a
past day shows its journal title, completed todos, other activities, and topic tags. Future dates
do not expose schedules.

## Importing historical Obsidian journals

Configure a read-only scan directory. It may be an entire vault or a journal subdirectory:

```env
OBSIDIAN_HISTORY_PATH="/Users/your-name/Documents/Obsidian/MyVault/Journal"
```

First, build the local source index:

```bash
python3 import_history.py scan
python3 import_history.py embed --max-batches 20 --batch-size 32
python3 import_history.py status
python3 import_history.py search "when I first started the BMS project"
```

With the default local provider, the first vector operation downloads and verifies the ONNX
model automatically. The download can take a while; subsequent runs use the cached model and
perform embedding entirely inside Python. `scan` embeds newly imported or changed documents,
while the idempotent `embed` command fills in vectors for documents that were previously scanned
in keyword-only mode.

The scanner:

1. Prefers `date`, `day`, `created`, or `created_at` from YAML frontmatter.
2. Falls back to `YYYY-M-D`, `YYYY_M_D`, or `YYYY年M月D日` in the filename.
3. Stores the source document and context chunks of approximately 1,600 characters, creates an
   FTS5 index, and batches embeddings when an embedding model is configured.
4. Uses SHA-256 content hashes to skip unchanged files and rebuild only new or modified files.
5. Imports explicitly checked Markdown items (`- [x]`) as activities.
6. Opens source Markdown files read-only and never modifies, renames, or moves them.

Documents and source chunks form the first memory layer, used to retrieve specific events and
their original context.

To continue or explicitly backfill historical vectors:

```bash
python3 import_history.py embed --max-batches 20 --batch-size 32
python3 import_history.py status
```

Each vector batch is committed independently. Rerun the same command after an interruption until
`pending_embeddings` is `0`. Changing the embedding endpoint or model changes its
signature, so the same command incrementally replaces stale vectors. Normal chat searches never
trigger a surprise full-history embedding job. `status` reports the requested and effective
search modes, compatible vectors, pending vectors, and the active embedding signature.

The second layer—people, projects, goals, preferences, routines, important events, and life
phases—is extracted only when a model is explicitly configured:

```env
HISTORY_LLM_MODEL="gpt-5-mini"
# When omitted, these two values inherit the current chat model endpoint and key:
HISTORY_LLM_BASE_URL="https://api.openai.com/v1"
HISTORY_LLM_API_KEY="sk-..."
```

Then run:

```bash
python3 import_history.py scan
python3 import_history.py extract --max-batches 20 --batch-size 5
```

Extraction sends only small batches of chunks; it never uploads years of journals in one request.
The cursor is saved after every batch, so rerunning the same command resumes interrupted work.
Memories retain their source file, journal date, time range, confidence, and
`historical/current/uncertain` status. Without `HISTORY_LLM_MODEL`, the `extract` command refuses
to run while first-layer local retrieval remains available.

## HTTP API

Main endpoints:

- Journal: `GET /v1/session`, `POST /v1/chat`, `POST /v1/preview`, `POST /v1/finalize`, and
  `DELETE /v1/messages/{id}`.
- Todos: `GET/POST /v1/todos`, `PATCH/DELETE /v1/todos/{id}`, and
  `POST /v1/todos/{id}/confirm|complete|postpone|cancel`.
- Memory: `GET /v1/memories` and `DELETE /v1/memories/{id}`.
- Review: `GET /v1/review/month?month=YYYY-MM` and `GET /v1/review/day/{day}`.
- History: `GET /v1/history/status|search` and `POST /v1/history/scan|embed|extract`.

Interactive parameters and request examples are available at `http://127.0.0.1:8000/docs`.
The service should listen on localhost by default.

## Database migrations and backups

On startup, the application performs additive migrations: it creates missing tables and indexes
and adds missing columns without deleting or rebuilding the existing `messages`, `entries`,
`memories`, `memory_sources`, or `app_state` data. The channel runtime reads the existing
Telegram offset on migration, then saves channel state, pending Telegram updates, and message
receipts in the binding database. Pending updates can contain message text; include this
database in diary backups. Existing diary records stay with the internal local owner.

New tables include `todos`, `todo_events`, `activities`, `reminder_log`, `historical_documents`,
`historical_chunks`, `historical_chunk_fts`, and `historical_memory_evidence`. Back up
`data/diary.sqlite` before the first upgrade.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, fake model providers, and fake channel APIs. They cover
message routing, user binding and isolation, Telegram compatibility, channel configuration, and
signed LINE/WhatsApp payloads without accessing real platforms or an actual Obsidian vault.
