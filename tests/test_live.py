"""Run the shipped smoke/BEAM clients against a real authenticated server."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx


def test_live_smoke_and_beam(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MEMORY_DB_PATH": str(tmp_path / "live.db"),
           "AML_API_KEY": "local-integration-key", "AML_AUTH_MODE": "bearer",
           "RETRIEVAL_MODE": "hybrid", "PYTHONIOENCODING": "utf-8"}
    # Local integration traffic must bypass system HTTP proxies, including in
    # the smoke/BEAM subprocesses that inherit this environment.
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    log_path = tmp_path / "uvicorn.log"
    server_log = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=root, env=env, stdout=server_log, stderr=subprocess.STDOUT,
        )
    except BaseException:
        server_log.close()
        raise
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            deadline = time.monotonic() + 30
            last_probe = "No health response received"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        f"Uvicorn exited before readiness (exit {process.returncode}). "
                        f"Log: {log_path}\n{log_path.read_text(encoding='utf-8', errors='replace')}"
                    )
                try:
                    response = client.get(url + "/health")
                    if response.status_code == 200:
                        break
                    last_probe = f"HTTP {response.status_code}: {response.text[:500]}"
                except httpx.TransportError as exc:
                    last_probe = repr(exc)
                time.sleep(0.1)
            else:
                raise AssertionError(
                    f"Uvicorn did not become ready at {url} within 30 seconds. "
                    f"Last probe: {last_probe}. Log: {log_path}\n"
                    f"{log_path.read_text(encoding='utf-8', errors='replace')}"
                )
        def run(*args):
            result = subprocess.run([sys.executable, *args], cwd=root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=40)
            assert result.returncode == 0, result.stdout + result.stderr
        run("scripts/smoke_check.py", url, env["AML_API_KEY"])
        run("scripts/eval_beam.py", "ingest", "--data", "tests/fixtures/beam_sample_conversations.json", "--server", url)
        output = tmp_path / "eval.jsonl"
        run("scripts/eval_beam.py", "search", "--data", "tests/fixtures/beam_sample_questions.jsonl", "--server", url, "--out", str(output))
        rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 3 and all(row["n_memories"] > 0 for row in rows)
        assert any("March 2024" in memory for memory in rows[0]["memories"])
        assert all("XK123456" not in memory for row in rows[:2] for memory in row["memories"])
    finally:
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        finally:
            server_log.close()
