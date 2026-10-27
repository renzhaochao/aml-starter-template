"""用官方 BEAM 评测链路测试本模板（回答生成 + rubric 评审复用官方 pipeline.py）。

数据准备（一次性）：
    从上游获取 BEAM 公开数据（Hugging Face: Mohammadta/BEAM，或许可证 CC BY-SA 4.0），
    整理为两个文件（字段名兼容本脚本的 loader）：

    conversations.json  [{"id": "conv-1", "messages": [{"role","content","timestamp"?}, ...]}]
    questions.jsonl     每行 {"id","conversation_id","question","question_type",
                              "rubric_nuggets": ["..."]}

用法（服务先跑起来）：
    python scripts/eval_beam.py ingest  --data conversations.json --server http://127.0.0.1:8000
    python scripts/eval_beam.py search  --data questions.jsonl  --server http://127.0.0.1:8000 --out eval_input.jsonl
    python scripts/eval_beam.py score   --input eval_input.jsonl --out scored.jsonl \
        --pipeline D:\\lunwen\\research\\tri-memory\\agent-memory-leaderboard\\data\\beam\\pipeline.py

score 需要环境变量（与官方 api_config.py 同名）：
    ANSWER_API_BASE / ANSWER_API_KEY / ANSWER_MODEL     回答模型（OpenAI 兼容接口）
    JUDGE_API_BASE  / JUDGE_API_KEY  / JUDGE_MODEL       评审模型
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def memory_headers() -> dict[str, str]:
    key = os.environ.get("AML_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}

# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def load_conversations(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("conversations", [data])
    for conv in data:
        assert conv.get("id") and conv.get("messages"), f"会话缺 id/messages: {conv.get('id')}"
    return data


def load_questions(path: str) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        assert row.get("id") and row.get("question") and row.get("conversation_id"), f"问题缺字段: {row.get('id')}"
        rubrics = row.get("rubric_nuggets") or row.get("rubrics") or row.get("rubric")
        assert isinstance(rubrics, list) and rubrics, f"问题缺 rubric 列表: {row.get('id')}"
    return rows


# ---------------------------------------------------------------------------
# ingest：把会话回放进本模板（POST /aml/add）
# ---------------------------------------------------------------------------


def build_adds(conv: dict, messages_per_add: int = 50) -> list[dict]:
    """把一条长会话切成多个 Add 请求（契约：单请求 messages 数量自拟，request_id 全局唯一）。"""
    msgs, adds = conv["messages"], []
    for start in range(0, len(msgs), messages_per_add):
        chunk = msgs[start : start + messages_per_add]
        adds.append(
            {
                "request_id": f"beam:{conv['id']}:{start // messages_per_add}",
                "messages": [
                    {"role": m["role"], "content": m["content"], "timestamp": m.get("timestamp")}
                    for m in chunk
                ],
                "user_id": f"beam-user-{conv['id']}",  # 每会话一个用户（隔离边界）
                "session_id": conv["id"],
            }
        )
    return adds


def cmd_ingest(args) -> None:
    conversations = load_conversations(args.data)

    def one(conv):
        # Keep chunks within each session sequential. The AML payload has no
        # global message offset, so parallel chunks would scramble neighbors.
        count = 0
        for body in build_adds(conv, args.messages_per_add):
            r = httpx.post(f"{args.server}/aml/add", json=body, headers=memory_headers(), timeout=120)
            r.raise_for_status()
            assert r.json().get("success") is True, r.text
            count += 1
        return count

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        total = sum(pool.map(one, conversations))
    print(f"ingest 完成：{total} 个 Add 请求已写入 {args.server}")


# ---------------------------------------------------------------------------
# search：逐问题检索，生成官方 pipeline.py 兼容的评测输入
# ---------------------------------------------------------------------------


def cmd_search(args) -> None:
    questions = load_questions(args.data)
    if args.limit:
        questions = questions[: args.limit]
    server, out = args.server.rstrip("/"), Path(args.out)

    def one(q):
        body = {"query": q["question"], "user_id": f"beam-user-{q['conversation_id']}", "top_k": 100}
        r = httpx.post(f"{server}/aml/search", json=body, headers=memory_headers(), timeout=120)
        r.raise_for_status()
        data = r.json()["data"]
        return {
            "id": q["id"],
            "question": q["question"],
            "question_type": q.get("question_type", q.get("category", "")),
            # 官方 context_text() 兼容的三种键之一；列表会被逐条拼接
            "memories": [d["content"] for d in data],
            "rubric_nuggets": q.get("rubric_nuggets") or q.get("rubrics") or q.get("rubric"),
            "n_memories": len(data),
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, questions))

    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    n = len(rows)
    avg = sum(r["n_memories"] for r in rows) / n if n else 0
    print(f"search 完成：{n} 个问题 → {out}；平均每题召回 {avg:.1f} 条记忆"
          + ("（当前骨架检索为弃权，召回 0 条——实现检索算法后再跑 score）" if avg == 0 else ""))


# ---------------------------------------------------------------------------
# score：复用官方 pipeline.py 的提示词与解析器，走 OpenAI 兼容接口
# ---------------------------------------------------------------------------


def load_pipeline(path: str):
    """动态加载官方 data/beam/pipeline.py（只借用其纯函数与提示词，不执行其 main）。"""
    spec = importlib.util.spec_from_file_location("beam_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(path).resolve().parents[2]))  # 使其可 import api_config
    spec.loader.exec_module(module)
    return module


def chat(base: str, key: str, model: str, prompt: str, max_tokens: int, json_mode: bool = False) -> str:
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = httpx.post(
        base.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=300,
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def cmd_score(args) -> None:
    pipe = load_pipeline(args.pipeline)
    rows = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    answer_cfg = (os.environ["ANSWER_API_BASE"], os.environ["ANSWER_API_KEY"], os.environ["ANSWER_MODEL"])
    judge_cfg = (os.environ["JUDGE_API_BASE"], os.environ["JUDGE_API_KEY"], os.environ["JUDGE_MODEL"])
    out = Path(args.out)

    def one(row):
        # 官方回答提示词：只用检索回的记忆作答
        answer = chat(*answer_cfg, pipe.render_answer_prompt(row), args.max_tokens)
        rubrics = pipe.rubric_items(row)
        verdict = chat(*judge_cfg, pipe.render_batch_judge_prompt(row["question"], answer, rubrics),
                         args.judge_max_tokens, json_mode=True)
        scores = pipe.parse_rubric_scores(verdict, len(rubrics))
        return {
            "id": row["id"],
            "question_type": row.get("question_type"),
            "answer": answer,
            "score": sum(s["score"] for s in scores) / len(scores),
            "rubric_scores": [{"rubric": r, "score": s["score"], "reason": s["reason"]}
                            for r, s in zip(rubrics, scores)],
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        scored = list(pool.map(one, rows))
    with out.open("w", encoding="utf-8") as handle:
        for row in scored:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    mean = sum(r["score"] for r in scored) / len(scored) if scored else 0.0
    by_type: dict[str, list[float]] = {}
    for row in scored:
        by_type.setdefault(row.get("question_type") or "unknown", []).append(row["score"])
    print(f"BEAM 本地复测：平均 rubric 得分 {mean:.3f}（{len(scored)} 题，评判模型 {judge_cfg[2]}）")
    for qtype, scores in sorted(by_type.items()):
        print(f"  - {qtype:<28} {sum(scores) / len(scores):.3f}（{len(scores)} 题）")
    print(f"明细已写入 {out}")


# ---------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="用官方 BEAM 链路本地测试本模板")
    commands = root.add_subparsers(dest="command", required=True)

    p = commands.add_parser("ingest", help="把 BEAM 会话回放进服务（POST /aml/add）")
    p.add_argument("--data", required=True, help="conversations.json")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--messages-per-add", type=int, default=50)
    p.add_argument("--workers", type=int, default=8)

    p = commands.add_parser("search", help="逐问题检索并生成评测输入 jsonl")
    p.add_argument("--data", required=True, help="questions.jsonl")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--out", default="eval_input.jsonl")
    p.add_argument("--limit", type=int, default=0, help="只测前 N 题（调试用）")
    p.add_argument("--workers", type=int, default=8)

    p = commands.add_parser("score", help="调用回答+评审模型打分（复用官方 pipeline 提示词）")
    p.add_argument("--input", required=True, help="search 生成的 eval_input.jsonl")
    p.add_argument("--out", default="scored.jsonl")
    p.add_argument("--pipeline", required=True, help="官方 data/beam/pipeline.py 路径")
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--judge-max-tokens", type=int, default=1024)
    p.add_argument("--workers", type=int, default=4)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    {"ingest": cmd_ingest, "search": cmd_search, "score": cmd_score}[arguments.command](arguments)
