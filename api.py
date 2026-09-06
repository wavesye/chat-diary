"""HTTP API for web, desktop, and automation clients."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request
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
        version="0.1.0",
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

    web_dir = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def web_ui() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    return app


app = create_app()
