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
- FTS5/BM25 and exact cosine embedding search run independently over SQLite-backed indexes;
  weighted reciprocal rank fusion combines their candidates. The system falls back to
  FTS5-only search when no embedding model is configured.
- The Telegram Bot API uses long polling, so no public domain or webhook is required.
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

Start the Telegram bot:

```bash
python3 telegram_bot.py
```

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

Embedding configuration is optional. For any OpenAI-compatible endpoint:

```env
MEMORY_SEARCH_MODE="hybrid"
EMBEDDING_PROVIDER="openai-compatible"
EMBEDDING_BASE_URL="https://api.openai.com/v1"
EMBEDDING_API_KEY="sk-..."
EMBEDDING_MODEL="text-embedding-3-small"
MEMORY_TOP_K=5
MEMORY_VECTOR_MIN_SIMILARITY=0.1
```

For a fully local Ollama embedding service:

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

Choose a multilingual embedding model for mixed Chinese and English journals.
`MEMORY_SEARCH_MODE` accepts `hybrid`, `vector`, or `keyword`. `hybrid` is requested by default,
but its effective mode is `keyword` until an embedding model and compatible stored vectors are
available. Without an embedding model, journal parsing, chunking, hashing, and FTS5 indexing
remain entirely local. A remote embedding endpoint receives only the text batches being embedded;
use Ollama if those fragments must never leave the machine. `MEMORY_VECTOR_MIN_SIMILARITY` filters
weak semantic matches and defaults to `0.1`.

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
return your numeric user ID. Add that value to `.env` and restart; only that user will then be
allowed to interact with the bot.

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
an empty generic error.

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
python3 import_history.py status
python3 import_history.py search "when I first started the BMS project"
```

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

If journals were scanned before an embedding model was configured, build the missing vectors
explicitly:

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
`memories`, `memory_sources`, or `app_state` data. Telegram offsets continue to use the existing
`app_state` key.

New tables include `todos`, `todo_events`, `activities`, `reminder_log`, `historical_documents`,
`historical_chunks`, `historical_chunk_fts`, and `historical_memory_evidence`. Back up
`data/diary.sqlite` before the first upgrade.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, fake model providers, and a fake Telegram API. They never
access a real model, Telegram, or an actual Obsidian vault.
