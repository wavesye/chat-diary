"""HTTP API for web, desktop, and automation clients."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from diary_agent.config import Settings
from diary_agent.service import DiaryService


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class ChatResponse(BaseModel):
    day: str
    reply: str


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    created_at: str


class SessionResponse(BaseModel):
    day: str
    greeting: str
    messages: list[MessageItem]


class PreviewResponse(BaseModel):
    day: str
    markdown: str


class FinalizeResponse(BaseModel):
    day: str
    path: str


class MemoryResponse(BaseModel):
    mode: str
    memories: list[dict]


class TodoCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=5000)
    planned_date: str | None = None
    due_at: str | None = None
    priority: int = Field(default=3, ge=1, le=5)
    confirmed: bool = True


class TodoUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    planned_date: str | None = None
    due_at: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)


class TodoPostponeRequest(BaseModel):
    planned_date: str


class HistoryExtractRequest(BaseModel):
    max_batches: int = Field(default=20, ge=1, le=200)
    batch_size: int = Field(default=5, ge=1, le=20)


def create_app(service: DiaryService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.diary = service
        else:
            app.state.diary = DiaryService(Settings.from_env())
        yield

    app = FastAPI(
        title="Chat Diary API",
        version="0.2.0",
        description="Conversational journaling and Obsidian export API.",
        lifespan=lifespan,
    )

    def get_service(request: Request) -> DiaryService:
        return request.app.state.diary

    @app.get("/health")
    def health(diary: DiaryService = Depends(get_service)) -> dict[str, str]:
        return {"status": "ok", "day": diary.day}

    @app.get("/v1/session", response_model=SessionResponse)
    def get_session(diary: DiaryService = Depends(get_service)) -> SessionResponse:
        return SessionResponse(
            day=diary.day,
            greeting=diary.greeting(),
            messages=diary.store.message_records(diary.day),
        )

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(
        body: ChatRequest, diary: DiaryService = Depends(get_service)
    ) -> ChatResponse:
        try:
            answer = diary.reply(body.message)
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return ChatResponse(day=diary.day, reply=answer)

    @app.delete("/v1/messages/{message_id}")
    def delete_message(
        message_id: int, diary: DiaryService = Depends(get_service)
    ) -> dict[str, bool]:
        if not diary.delete_message(message_id):
            raise HTTPException(status_code=404, detail="Message not found for today")
        return {"ok": True, "memories_need_rebuild": True}

    @app.post("/v1/preview", response_model=PreviewResponse)
    def preview(diary: DiaryService = Depends(get_service)) -> PreviewResponse:
        try:
            markdown = diary.preview()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return PreviewResponse(day=diary.day, markdown=markdown)

    @app.post("/v1/finalize", response_model=FinalizeResponse)
    def finalize(diary: DiaryService = Depends(get_service)) -> FinalizeResponse:
        try:
            path = diary.finalize()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return FinalizeResponse(day=diary.day, path=str(path))

    @app.get("/v1/memories", response_model=MemoryResponse)
    def memories(diary: DiaryService = Depends(get_service)) -> MemoryResponse:
        return MemoryResponse(mode=diary.memory.mode, memories=diary.memory.list())

    @app.delete("/v1/memories/{memory_id}")
    def delete_memory(
        memory_id: int, diary: DiaryService = Depends(get_service)
    ) -> dict[str, bool]:
        if not diary.memory.delete(memory_id):
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"ok": True}

    @app.get("/v1/todos")
    def list_todos(
        status: str = "active", diary: DiaryService = Depends(get_service)
    ) -> dict:
        statuses = tuple(part.strip() for part in status.split(",") if part.strip())
        return {"todos": diary.todos.list(statuses=statuses)}

    @app.post("/v1/todos", status_code=201)
    def create_todo(
        body: TodoCreateRequest, diary: DiaryService = Depends(get_service)
    ) -> dict:
        try:
            return diary.todos.create(
                body.title, description=body.description, planned_date=body.planned_date,
                due_at=body.due_at, priority=body.priority,
                creation_method="api", confirmed=body.confirmed,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.patch("/v1/todos/{todo_id}")
    def update_todo(
        todo_id: int, body: TodoUpdateRequest,
        diary: DiaryService = Depends(get_service),
    ) -> dict:
        try:
            return diary.todos.update(todo_id, **body.model_dump(exclude_unset=True))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/v1/todos/{todo_id}/complete")
    def complete_todo(todo_id: int, diary: DiaryService = Depends(get_service)) -> dict:
        try:
            return diary.todos.complete(todo_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/todos/{todo_id}/confirm")
    def confirm_todo(todo_id: int, diary: DiaryService = Depends(get_service)) -> dict:
        try:
            return diary.todos.confirm(todo_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/todos/{todo_id}/postpone")
    def postpone_todo(
        todo_id: int, body: TodoPostponeRequest,
        diary: DiaryService = Depends(get_service),
    ) -> dict:
        try:
            return diary.todos.postpone(todo_id, body.planned_date)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/todos/{todo_id}/cancel")
    def cancel_todo(todo_id: int, diary: DiaryService = Depends(get_service)) -> dict:
        try:
            return diary.todos.cancel(todo_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/v1/todos/{todo_id}")
    def delete_todo(todo_id: int, diary: DiaryService = Depends(get_service)) -> dict:
        """User-facing delete is recoverable cancellation, not physical row deletion."""
        try:
            return diary.todos.cancel(todo_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/v1/review/month")
    def review_month(
        month: str, diary: DiaryService = Depends(get_service)
    ) -> dict:
        try:
            date.fromisoformat(month + "-01")
        except ValueError as error:
            raise HTTPException(status_code=422, detail="month 必须是 YYYY-MM") from error
        return {"month": month, "days": diary.activities.month(month, diary.day)}

    @app.get("/v1/review/day/{day}")
    def review_day(day: str, diary: DiaryService = Depends(get_service)) -> dict:
        try:
            parsed = date.fromisoformat(day)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="日期必须是 YYYY-MM-DD") from error
        if parsed > date.fromisoformat(diary.day):
            raise HTTPException(status_code=404, detail="回顾日历不展示未来日期")
        return diary.activities.review_day(day)

    @app.get("/v1/history/status")
    def history_status(diary: DiaryService = Depends(get_service)) -> dict:
        return diary.history_index.status()

    @app.get("/v1/history/search")
    def history_search(
        q: str = Query(min_length=1, max_length=2000),
        diary: DiaryService = Depends(get_service),
    ) -> dict:
        return {"mode": diary.history_index.mode,
                "results": [item.__dict__ for item in diary.history_index.search(q, 10)]}

    @app.post("/v1/history/scan")
    def history_scan(diary: DiaryService = Depends(get_service)) -> dict:
        if not diary.history_importer:
            raise HTTPException(status_code=409, detail="请配置 OBSIDIAN_HISTORY_PATH")
        try:
            return diary.history_importer.scan()
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/v1/history/extract")
    def history_extract(
        body: HistoryExtractRequest, diary: DiaryService = Depends(get_service)
    ) -> dict:
        if not diary.history_importer:
            raise HTTPException(status_code=409, detail="请配置 OBSIDIAN_HISTORY_PATH")
        try:
            return diary.history_importer.extract(body.max_batches, body.batch_size)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    web_dir = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def web_ui() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    return app


app = create_app()
