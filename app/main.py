"""AML 契约服务入口：/aml/add、/aml/search、/health。

骨架已实现（契约要求）：
- 契约字段原样回显、认证、幂等、隔离边界、弃权语义。

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from functools import lru_cache
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .config import settings
from .schemas import (
    AddRequest,
    AddResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
)
from .store import RequestConflictError, SQLiteMemoryStore


@lru_cache(maxsize=1)
def get_store() -> SQLiteMemoryStore:
    return SQLiteMemoryStore(
        settings.db_path, retrieval_mode=settings.retrieval_mode,
        candidate_limit=settings.candidate_limit, context_window=settings.context_window,
        evidence_char_budget=settings.evidence_char_budget,
    )


def require_api_key(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """认证。api_key 未配置时免认证（仅本地）；配置后按 auth_mode 校验。

    支持 Authorization: Bearer / Token 以及 X-Api-Key 三种头。
    """
    expected = settings.api_key
    if not expected or settings.auth_mode == "none":
        return
    token = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() in ("bearer", "token"):
            token = value.strip()
    if not token:
        token = (x_api_key or "").strip()
    if not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )


app = FastAPI(title="AML Temporal Evidence Memory", version="1.0.0")


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(**get_store().stats())


@app.post("/aml/add", response_model=AddResponse)
def add_memory(
    request: AddRequest,
    _: Annotated[None, Depends(require_api_key)],
) -> AddResponse:
    store = get_store()
    try:
        store.add(
            request.request_id,
            request.user_id,
            request.session_id,
            [m.model_dump() for m in request.messages],
        )
    except RequestConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"request_id conflict: {exc}",
        ) from exc
    # 契约要求：原样回显 request_id / user_id / session_id（平台逐字节比对）
    return AddResponse(
        request_id=request.request_id,
        user_id=request.user_id,
        session_id=request.session_id,
    )


@app.post("/aml/search", response_model=SearchResponse)
def search_memory(
    request: SearchRequest,
    _: Annotated[None, Depends(require_api_key)],
) -> SearchResponse:
    store = get_store()
    # user_id 是唯一的检索隔离边界：绝不允许跨用户返回记忆
    items = store.search(request.user_id, request.query, request.top_k)
    return SearchResponse(data=items)
