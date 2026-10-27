"""端到端冒烟检查（模拟平台 Smoke 测试）。

用法：
    python scripts/smoke_check.py [base_url] [key]
本地无认证：python scripts/smoke_check.py http://127.0.0.1:8000
"""

from __future__ import annotations

import sys
import uuid

import httpx

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    key = sys.argv[2] if len(sys.argv) > 2 else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    with httpx.Client(base_url=base, headers=headers, timeout=30.0) as client:
        run_id = uuid.uuid4().hex
        user_a, user_b = f"smoke:{run_id}:a", f"smoke:{run_id}:b"
        r = client.get("/health")
        check("GET /health 200", r.status_code == 200, r.text[:120])

        payload = {
            "request_id": f"smoke:{run_id}:chunk-0",
            "messages": [
                {"role": "user", "content": "I moved to Porto two weeks ago."},
                {"role": "user", "content": "My passport number is XK123456."},
            ],
            "user_id": user_a,
            "session_id": "smoke:session:0",
        }
        r = client.post("/aml/add", json=payload)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check(
            "POST /aml/add 200 + 契约字段回显",
            r.status_code == 200
            and body.get("success") is True
            and body.get("request_id") == payload["request_id"]
            and body.get("user_id") == payload["user_id"]
            and body.get("session_id") == payload["session_id"],
            f"http={r.status_code}",
        )

        # 幂等重试
        before_retry = client.get("/health").json()["memories"]
        r2 = client.post("/aml/add", json=payload)
        health = client.get("/health").json()
        check("request_id 幂等重试不产生重复", r2.status_code == 200 and health["memories"] == before_retry)

        # 隔离：另一个用户搜不到他人护照号
        r = client.post(
            "/aml/search", json={"query": "passport number", "user_id": user_b, "top_k": 10}
        )
        data = r.json()["data"] if r.status_code == 200 else []
        check("user_id 隔离（不泄露他人数据）", r.status_code == 200 and data == [])

        r = client.post("/aml/search", json={"query": "Porto", "user_id": user_a, "top_k": 10})
        data = r.json().get("data", [])
        check("检索命中相关记忆", r.status_code == 200 and any("Porto" in d["content"] for d in data))
        check("得分降序排列", all(data[i]["score"] >= data[i + 1]["score"] for i in range(len(data) - 1)))

    print()
    print("全部冒烟检查通过。" if failures == 0 else f"{failures} 项失败。")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
