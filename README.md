# Chat Diary

一个每天和你一问一答、在结束时把对话整理成 Markdown 日记并写入 Obsidian 的本地应用。

## 为什么采用轻量工作流

这个需求的路径是确定的：聊天 → 结束确认 → 结构化提取 → Markdown → Obsidian。
它不需要 BMS Agent 那种动态工具循环，也暂时不需要 Writing Agent 的 RAG。
因此本项目复用了两个项目里验证过的思路（Provider 配置、本地 SQLite 记忆），但不引入
LangGraph 和 MCP。等以后加入照片搜索、日历、健康数据等多个动态工具时，再引入
OpenAI Agents SDK 或 LangGraph 会更划算。

## 运行

需要 Python 3.10+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env` 后加载配置并运行：

```bash
set -a
source .env
set +a
python3 main.py
```

也可以启动 HTTP API，供网页、桌面客户端或定时任务使用：

```bash
uvicorn api:app --host 127.0.0.1 --port 8000 --reload
```

启动后可打开 `http://127.0.0.1:8000/docs` 调试。API 默认只监听本机：

- `GET /health`：健康状态与当前日记日期。
- `GET /v1/session`：读取今天的完整对话。
- `POST /v1/chat`：发送一条消息，body 为 `{"message": "今天……"}`。
- `POST /v1/preview`：生成 Markdown 预览，不写文件。
- `POST /v1/finalize`：生成并写入今天的 Obsidian 日记。

示例：

```bash
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"今天终于完成了拖了很久的事情"}'

curl -X POST http://127.0.0.1:8000/v1/finalize
```

聊天记录保存在 `data/diary.sqlite`。每天按配置时区自动建立一个会话；中途退出不会
丢失。输入 `/done` 后，程序会生成 `YYYY-MM-DD.md` 并原子写入指定的 Obsidian
目录；当天再次执行 `/done` 会更新同一篇日记。输入 `/preview` 可先看结果。

## 配置

- `OBSIDIAN_VAULT_PATH`：vault 的绝对路径，必填。
- `OBSIDIAN_JOURNAL_FOLDER`：vault 内的日记目录，默认 `日记/AI 日记`。
- `DIARY_TIMEZONE`：决定“今天”的时区，默认 `Asia/Shanghai`。
- `LLM_PROVIDER`：支持 `openai`、`deepseek`、`openrouter`、`ollama`、`qwen`、`custom`。
- `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL`：可覆盖 Provider 预设。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试不访问模型 API，也不会写入真实 Obsidian vault。
