"""HTTP API for web, desktop, and automation clients."""

from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from diary_agent.config import Settings
from diary_agent.service import DiaryService


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class ChatResponse(BaseModel):
    day: str
    reply: str


class SessionResponse(BaseModel):
    day: str
    messages: list[dict[str, str]]


class PreviewResponse(BaseModel):
    day: str
    markdown: str


class FinalizeResponse(BaseModel):
    day: str
    path: str


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
        return SessionResponse(day=diary.day, messages=diary.store.messages(diary.day))

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(
        body: ChatRequest, diary: DiaryService = Depends(get_service)
    ) -> ChatResponse:
        try:
            answer = diary.reply(body.message)
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return ChatResponse(day=diary.day, reply=answer)

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

    return app


app = create_app()
