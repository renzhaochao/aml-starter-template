"""AML starter request/response contract; keep echoed identifiers unchanged."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MessagePayload(BaseModel):
    role: str = Field(min_length=1)
    content: str = Field(min_length=1)
    timestamp: int | None = None  # Unix 毫秒


class AddRequest(BaseModel):
    request_id: str = Field(min_length=1)
    messages: list[MessagePayload] = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class AddResponse(BaseModel):
    success: Literal[True] = True
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    options: list[str] | None = None
    user_id: str = Field(min_length=1)
    top_k: int = Field(gt=0, le=100)  # 正式评测 top_k=100


class SearchItem(BaseModel):
    id: str
    content: str
    score: float | None = None
    created_at: str | None = None  # ISO 8601


class SearchResponse(BaseModel):
    data: list[SearchItem]  # 空数组 = 弃权（证据不足），平台允许


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    memories: int
    users: int
