"""集中式配置：全部从环境变量读取，见 .env.example。

新增自己的配置项时，在这里加字段并在 .env.example 中补说明。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # 认证：api_key 为空 = 本地免认证；非空则按 auth_mode 校验请求头
    api_key: str = os.environ.get("AML_API_KEY", "")
    auth_mode: str = os.environ.get("AML_AUTH_MODE", "bearer")  # none | bearer | token | x-api-key

    # 存储
    db_path: str = os.environ.get("MEMORY_DB_PATH", "./data/memory.db")

    retrieval_mode: str = os.environ.get("RETRIEVAL_MODE", "hybrid")
    candidate_limit: int = int(os.environ.get("RETRIEVAL_CANDIDATE_LIMIT", "200"))
    context_window: int = int(os.environ.get("RETRIEVAL_CONTEXT_WINDOW", "1"))
    evidence_char_budget: int = int(os.environ.get("RETRIEVAL_CHAR_BUDGET", "24000"))

    def __post_init__(self) -> None:
        if self.retrieval_mode not in {"hybrid", "bm25", "abstain"}:
            raise ValueError("RETRIEVAL_MODE must be hybrid, bm25 or abstain")
        if self.auth_mode not in {"none", "bearer", "token", "x-api-key"}:
            raise ValueError("Unsupported AML_AUTH_MODE")
        if self.candidate_limit <= 0 or self.context_window < 0 or self.evidence_char_budget <= 0:
            raise ValueError("Invalid retrieval limits")


settings = Settings()
