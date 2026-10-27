"""Rule-planned, coverage-driven retrieval of original AML memory evidence."""

from __future__ import annotations

import calendar
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

import jieba


_TOKENIZER = jieba.Tokenizer()
_WORDS = re.compile(r"[a-zA-Z]+[a-zA-Z0-9_]*|\d+(?:\.\d+)?|[\u3400-\u9fff]+")
_CJK = re.compile(r"[\u3400-\u9fff]+")
_STOP = set("a an the i me my we our you your user users what which who when where why how did do does is are was were be been have has had to of in on at for and or it that this then later initially first now current originally before after please tell about".split())
_STOP.update("我 我的 你 你的 用户 什么 哪个 哪些 为什么 为何 怎么 如何 什么时候 是否 是 的 了 吗 呢 啊 在 于 和 与 及 或 后来 最初 起初 最早 当前 现在 之前 之后 曾经 原来 后续 请 告诉 帮 查 一下".split())
_EARLY = re.compile(r"最初|起初|最早|原先|\b(?:first|initially|initial|originally|original)\b", re.I)
_LATE = re.compile(r"现在|当前|最新|最近|后来|后续|\b(?:now|current|latest|later|recent)\b", re.I)
_REASON = re.compile(r"为什么|为何|原因|\b(?:why|reason|because)\b", re.I)
_CHANGE = re.compile(r"改|变化|调整|更正|纠正|\b(?:change|changed|switch|switched|corrected)\b", re.I)
_REASON_EVIDENCE = re.compile(r"因为|由于|考虑到|为了|所以|需求|\b(?:because|due to|reason|requirement|needed)\b", re.I)
_CONFIRM = re.compile(r"确认|决定|改成|改为|就按|采用|选用|选择|使用|\b(?:confirmed|decided|switched|changed to|use|using|chose|choose)\b", re.I)
_CHANGE_EVIDENCE = re.compile(r"改成|改为|调整|更正|纠正|\b(?:changed|switched|corrected|instead)\b", re.I)
_DATE = re.compile(r"(?<!\d)(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?|年(?:(\d{1,2})月(?:(\d{1,2})日?)?)?)(?!\d)")
_ALIASES = {
    "数据库": {"mysql", "postgresql", "sqlite", "mongodb", "database"},
    "database": {"mysql", "postgresql", "sqlite", "mongodb", "数据库"},
}


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


@lru_cache(maxsize=4096)
def lexical_terms(text: str) -> tuple[str, ...]:
    """Chinese segmentation plus Latin identifiers; never replaces raw text."""
    terms: list[str] = []
    for part in _WORDS.findall(normalize(text)):
        tokens = _TOKENIZER.cut(part, HMM=False) if _CJK.fullmatch(part) else [part]
        terms.extend(token for token in tokens if token not in _STOP and token.strip())
    return tuple(terms)


def character_terms(text: str) -> tuple[str, ...]:
    # Keep only Chinese character runs; word boundaries matter for Latin text.
    return tuple(sorted({run[i:i + size] for run in _CJK.findall(normalize(text))
                         for size in (2, 3) for i in range(len(run) - size + 1)}))


def explicit_clues(text: str) -> set[str]:
    lowered = normalize(text)
    clues = set(re.findall(r"\b[a-z]+[a-z0-9_-]*\b|\b\d+(?:\.\d+)?\b", lowered)) - _STOP
    for match in _DATE.finditer(lowered):
        year, month, day, chinese_month, chinese_day = match.groups()
        month, day = month or chinese_month, day or chinese_day
        value = year
        if month:
            value += f"-{int(month):02}"
        if day:
            value += f"-{int(day):02}"
        clues.add(value)
    return clues


def _bound(year: int, month: int, day: int, end: bool = False) -> int:
    return calendar.timegm((year, month, day, 23 if end else 0, 59 if end else 0,
                           59 if end else 0)) * 1000 + (999 if end else 0)


@dataclass
class QueryPlan:
    original: str
    terms: set[str]
    grams: set[str]
    clues: set[str]
    aspects: list[str]
    lower_time: int | None = None
    upper_time: int | None = None

    @classmethod
    def build(cls, query: str) -> QueryPlan:
        aspects = ["topic"]
        if _EARLY.search(query):
            aspects.append("initial")
        if _LATE.search(query):
            aspects.append("later")
        if _REASON.search(query):
            aspects.append("reason")
        if _CHANGE.search(query):
            aspects.append("change")
        plan = cls(query, set(lexical_terms(query)), set(character_terms(query)),
                   explicit_clues(query), aspects)
        # Only explicit directional boundaries filter candidates. A date merely
        # mentioned in a question remains a clue, never an invented cutoff.
        for match in _DATE.finditer(normalize(query)):
            year, month, day, chinese_month, chinese_day = match.groups()
            month, day = month or chinese_month, day or chinese_day
            try:
                y, m = int(year), int(month or 1)
                start = _bound(y, m, int(day or 1))
                end = _bound(y, int(month or 12), int(day or calendar.monthrange(y, int(month or 12))[1]), True)
            except (ValueError, OverflowError):
                continue
            prefix = normalize(query)[max(0, match.start() - 12):match.start()]
            suffix = normalize(query)[match.end():match.end() + 6]
            if re.search(r"before\s*$", prefix) or re.match(r"(?:之)?前", suffix):
                plan.upper_time = min(plan.upper_time if plan.upper_time is not None else start - 1, start - 1)
            if re.search(r"after\s*$", prefix) or re.match(r"(?:之)?后", suffix):
                plan.lower_time = max(plan.lower_time if plan.lower_time is not None else end + 1, end + 1)
            if re.search(r"since\s*$", prefix) or re.match(r"以来", suffix):
                plan.lower_time = max(plan.lower_time if plan.lower_time is not None else start, start)
        return plan


class HybridRetriever:
    def __init__(self, conn: sqlite3.Connection, *, candidate_limit: int = 200,
                 context_window: int = 1, evidence_char_budget: int = 24000,
                 retrieval_mode: str = "hybrid") -> None:
        self.conn = conn
        self.limit = max(1, candidate_limit)
        self.window = max(0, context_window)
        self.budget = max(1, evidence_char_budget)
        self.mode = retrieval_mode

    @staticmethod
    def _scope(user_id: str, plan: QueryPlan) -> tuple[str, list]:
        sql, args = "m.user_id=?", [user_id]
        if plan.lower_time is not None:
            sql += " AND m.original_timestamp>=?"
            args.append(plan.lower_time)
        if plan.upper_time is not None:
            sql += " AND m.original_timestamp<=?"
            args.append(plan.upper_time)
        return sql, args

    def _fts(self, table: str, terms: set[str], user_id: str, plan: QueryPlan) -> list[dict]:
        if not terms:
            return []
        # Quoting terms prevents user text from becoming FTS syntax. Bound the
        # expression and candidate pool independently from response top_k.
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in sorted(terms)[:96])
        scope, args = self._scope(user_id, plan)
        rows = self.conn.execute(f"""SELECT m.*, bm25({table}) AS path_score
            FROM {table} JOIN memories m ON m.rowid={table}.rowid
            WHERE {table} MATCH ? AND {scope}
            ORDER BY path_score, m.id LIMIT ?""", [expression, *args, self.limit]).fetchall()
        return [dict(row) for row in rows]

    def _exact(self, user_id: str, plan: QueryPlan) -> list[dict]:
        if not plan.clues:
            return []
        scope, args = self._scope(user_id, plan)
        clues = sorted(plan.clues)[:96]
        marks = ",".join("?" for _ in clues)
        return [dict(row) for row in self.conn.execute(f"""SELECT m.*, COUNT(*) AS clue_hits
            FROM memory_clues c JOIN memories m ON m.id=c.memory_id
            WHERE c.clue IN ({marks}) AND {scope} GROUP BY m.id
            ORDER BY clue_hits DESC, m.id LIMIT ?""", [*clues, *args, self.limit]).fetchall()]

    def _neighbors(self, row: dict, user_id: str, plan: QueryPlan) -> list[dict]:
        scope, args = self._scope(user_id, plan)
        return [dict(record) for record in self.conn.execute(f"""SELECT m.* FROM memories m
            WHERE {scope} AND m.session_id=? AND m.message_index BETWEEN ? AND ?
            ORDER BY m.message_index""", [*args, row["session_id"],
                row["message_index"] - self.window, row["message_index"] + self.window]).fetchall()]

    @staticmethod
    def _time(row: dict) -> tuple:
        # Millisecond source time wins. Unknown times stay explicitly unknown
        # in output; ingestion order is only a deterministic fallback.
        return (row["original_timestamp"] is not None,
                row["original_timestamp"] or 0, row["created_at"], row["message_index"], row["id"])

    def search(self, user_id: str, query: str, top_k: int) -> list[dict]:
        if self.mode == "abstain" or top_k <= 0:
            return []
        plan = QueryPlan.build(query)
        # Small, explicit dictionary expansions supplement the original path;
        # they only locate evidence and never assert a fact about the user.
        paths = [self._fts("memory_lexical", plan.terms, user_id, plan)]
        expanded = set().union(*(_ALIASES.get(term, set()) for term in plan.terms)) if plan.terms else set()
        if expanded and self.mode == "hybrid":
            paths.append(self._fts("memory_lexical", expanded, user_id, plan))
        if self.mode == "hybrid":
            # Avoid grammatical question fragments becoming the only evidence.
            content_grams = set(character_terms(" ".join(plan.terms)))
            paths += [self._fts("memory_character", plan.grams & content_grams, user_id, plan),
                      self._exact(user_id, plan)]
        fused: dict[str, dict] = {}
        scores: defaultdict[str, float] = defaultdict(float)
        for path in paths:
            for rank, row in enumerate(path, 1):
                fused[row["id"]] = row
                scores[row["id"]] += 1.0 / (60 + rank)
        if not fused:
            return []

        # Feature coverage is derived from observed source spans. An assistant
        # suggestion cannot satisfy user confirmation/change aspects.
        ordered = sorted(fused.values(), key=self._time)
        features: dict[str, set[str]] = {}
        source_terms = {row["id"]: set(lexical_terms(row["content"])) for row in ordered}
        times = [row["original_timestamp"] for row in ordered if row["original_timestamp"] is not None]
        minimum_time, maximum_time = (min(times), max(times)) if times else (0, 0)
        for row in ordered:
            terms = source_terms[row["id"]]
            feature = {"term:" + term for term in plan.terms & terms}
            if _REASON_EVIDENCE.search(row["content"]):
                feature.add("reason")
            if row["role"].lower() == "user":
                if _CHANGE_EVIDENCE.search(row["content"]):
                    feature.add("change")
                if _CONFIRM.search(row["content"]):
                    if "initial" in plan.aspects:
                        feature.add("initial")
                    if "later" in plan.aspects:
                        feature.add("later")
            features[row["id"]] = feature
        user_rows = [row for row in ordered if row["role"].lower() == "user"]
        for row in user_rows[1:]:
            features[row["id"]].discard("initial")
        for row in user_rows[:-1]:
            features[row["id"]].discard("later")

        # Greedy weighted coverage with a redundancy penalty. Repeated source
        # events retain separate IDs, while duplicate path hits are fused.
        chosen: list[dict] = []
        covered: set[str] = set()
        remaining = dict(fused)
        budget = self.budget
        ranked_scores: dict[str, float] = {}
        while remaining and len(chosen) < top_k:
            best: tuple | None = None
            best_row: dict | None = None
            for row in remaining.values():
                new_features = features[row["id"]] - covered
                gain = sum(0.035 if feature in plan.aspects else 0.005 for feature in new_features)
                redundancy = max(
                    (len(source_terms[row["id"]] & source_terms[old["id"]]) /
                     max(1, len(source_terms[row["id"]] | source_terms[old["id"]]))
                     for old in chosen), default=0,
                )
                value = scores[row["id"]] + gain - 0.006 * redundancy
                # Temporal preference applies only when query requests it;
                # it is an ordering hint, never a truth/causation assertion.
                time_key = self._time(row)
                event_time = row["original_timestamp"]
                if event_time is not None and maximum_time > minimum_time:
                    fraction = (event_time - minimum_time) / (maximum_time - minimum_time)
                    if "initial" in plan.aspects and "later" not in plan.aspects:
                        value += 0.025 * (1 - fraction)
                    if "later" in plan.aspects and "initial" not in plan.aspects:
                        value += 0.025 * fraction
                if ("later" in plan.aspects or "change" in plan.aspects) and row["role"].lower() == "user" and _CONFIRM.search(row["content"]):
                    value += 0.01
                key = (value, time_key)
                if len(self._render_content(row, "direct")) <= budget and (best is None or key > best):
                    best, best_row = key, row
            if best_row is None:
                break
            chosen.append(best_row)
            ranked_scores[best_row["id"]] = best[0]
            covered.update(features[best_row["id"]])
            budget -= len(self._render_content(best_row, "direct"))
            remaining.pop(best_row["id"])

        results: dict[str, tuple[dict, str, float]] = {
            row["id"]: (row, "direct", ranked_scores[row["id"]]) for row in chosen}
        # Add context in unused slots; never evict a selected coverage source.
        # A context row never gains direct-match status.
        context_candidates: dict[str, tuple[dict, str, float]] = {}
        for row in chosen:
            for neighbor in self._neighbors(row, user_id, plan):
                if neighbor["id"] not in results:
                    context_candidates.setdefault(neighbor["id"], (neighbor, "context", scores[row["id"]] * 0.35))
        if context_candidates and top_k > 1:
            for row, kind, score in context_candidates.values():
                length = len(self._render_content(row, kind))
                if len(results) < top_k and length <= budget:
                    results[row["id"]] = (row, kind, score)
                    budget -= length
        return [
            {"id": row["id"], "content": self._render_content(row, kind),
             "score": round(score, 8), "created_at": row["created_at"]}
            for row, kind, score in sorted(results.values(), key=lambda item: (-item[2], item[0]["id"]))
        ]

    @staticmethod
    def _render_content(row: dict, kind: str) -> str:
        # Existing local evaluator forwards only content. Put factual source
        # metadata here so the answering model actually sees role and time.
        # JSON escapes untrusted metadata, while the message body is verbatim.
        import json

        metadata = {"evidence": kind, "source_id": row["id"], "role": row["role"],
                    "session_id": row["session_id"], "message_index": row["message_index"],
                    "timestamp_ms": row["original_timestamp"]}
        return "[source " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "]\n" + row["content"]
