"""Test the shipped EXE in an isolated directory without Python on its PATH."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx


def main() -> None:
    archive = Path(sys.argv[1]).resolve()
    checks = []
    with zipfile.ZipFile(archive) as package:
        expected = {"aml-server.exe", "start.cmd", ".env.example", "README-Windows.md", "THIRD-PARTY-NOTICES.txt"}
        assert set(package.namelist()) == expected, package.namelist()
        checks.append("Archive contains only approved release files; no data or secrets")
        with tempfile.TemporaryDirectory(prefix="aml-exe-check-") as temporary:
            root = Path(temporary)
            release = root / "中文 空白服务器"
            release.mkdir()
            package.extractall(release)
            unrelated = root / "different working directory"
            unrelated.mkdir()
            runtime_temp = root / "fresh-temp"
            runtime_temp.mkdir()
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("AML_", "RETRIEVAL_", "PYTHON"))
                and key not in {"MEMORY_DB_PATH", "VIRTUAL_ENV"}
            }
            env.update(PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
                       TEMP=str(runtime_temp), TMP=str(runtime_temp))
            executable = str(release / "aml-server.exe")
            version = subprocess.run([executable, "--version"], cwd=unrelated, env=env,
                                     capture_output=True, text=True, timeout=60)
            assert version.returncode == 0 and "1.0.0" in version.stdout, version.stderr
            checks.append("EXE starts without Python/runtime directories on PATH")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            output_path = root / "process.log"

            def launch():
                output = output_path.open("ab")
                process = subprocess.Popen(
                    [executable, "--host", "127.0.0.1", "--port", str(port)],
                    cwd=unrelated, env=env, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return process, output

            def stop(process, output):
                if process.poll() is None:
                    # Onefile has a bootloader parent and an application child.
                    # Stop only children identified by this isolated server log;
                    # no WMI/taskkill permissions or system process scan needed.
                    output.flush()
                    log_text = output_path.read_text(encoding="utf-8", errors="replace")
                    child_pids = re.findall(r"Started server process \[(\d+)\]", log_text)
                    for pid in child_pids[-1:]:
                        try:
                            os.kill(int(pid), signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=20)
                output.close()

            def ready(client, process):
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(output_path.read_text(encoding="utf-8", errors="replace"))
                    try:
                        response = client.get("/health")
                        if response.status_code == 200:
                            return response.json()
                    except httpx.TransportError:
                        pass
                    time.sleep(0.2)
                raise AssertionError("Readiness timeout: " + output_path.read_text(encoding="utf-8", errors="replace"))

            process, output = launch()
            try:
                with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
                    assert ready(client, process) == {"status": "ok", "memories": 0, "users": 0}
                    checks.append("Health endpoint, fresh SQLite database and FTS5 initialization")
                    config = (release / ".env").read_text(encoding="utf-8")
                    key = next(line.partition("=")[2] for line in config.splitlines() if line.startswith("AML_API_KEY="))
                    assert len(key) == 64
                    headers = {"Authorization": "Bearer " + key}
                    payload = {"request_id": "exe-chinese-1", "user_id": "exe-a", "session_id": "session-1",
                               "messages": [{"role": "user", "content": "我在上海使用 PostgreSQL 数据库。", "timestamp": 1710000000000}]}
                    assert client.post("/aml/add", json=payload).status_code == 401
                    assert client.post("/aml/add", json=payload, headers={"X-Api-Key": "wrong"}).status_code == 401
                    response = client.post("/aml/add", json=payload, headers=headers)
                    assert response.status_code == 200, response.text
                    assert response.json() == {"success": True, "request_id": "exe-chinese-1", "user_id": "exe-a", "session_id": "session-1"}
                    assert client.post("/aml/add", json=payload, headers={"X-Api-Key": key}).status_code == 200
                    assert client.get("/health").json()["memories"] == 1
                    conflict = {**payload, "session_id": "changed"}
                    assert client.post("/aml/add", json=conflict, headers=headers).status_code == 409
                    query = {"user_id": "exe-a", "query": "上海 数据库", "top_k": 5}
                    response = client.post("/aml/search", json=query, headers={"Authorization": "Token " + key})
                    assert response.status_code == 200 and any("PostgreSQL" in item["content"] for item in response.json()["data"]), response.text
                    assert client.post("/aml/search", json={**query, "user_id": "exe-b"}, headers=headers).json() == {"data": []}
                    checks.append("Chinese dictionary/FTS search, all three auth headers, idempotency/conflict and user isolation")
                    assert (release / "data" / "memory.db").exists()
                    assert (release / "logs" / "http.log").stat().st_size > 0
                    assert not (unrelated / "data").exists() and not (unrelated / ".env").exists()
                    checks.append("Unicode/spaced EXE path and unrelated current directory; external configuration/data/logs")
            finally:
                stop(process, output)

            # Verify that external .env changes are read before importing app.config.
            (release / ".env").write_text(config.replace("AML_HOST=0.0.0.0", "AML_HOST=127.0.0.1")
                                         .replace("AML_PORT=8000", f"AML_PORT={port}"), encoding="utf-8-sig")
            with output_path.open("ab") as output:
                process = subprocess.Popen([executable], cwd=unrelated, env=env,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
                        assert ready(client, process)["memories"] == 1
                        assert client.post("/aml/search", json=query, headers=headers).json()["data"]
                        assert key in (release / ".env").read_text(encoding="utf-8-sig")
                    checks.append("Restart preserves API key and memory; UTF-8 BOM .env host/port settings load correctly")
                finally:
                    stop(process, output)
    report = {"archive": archive.name, "result": "passed", "checks": checks}
    report_path = Path(__file__).resolve().parents[1] / "build" / "windows" / "verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for check in checks:
        print("PASS:", check)


if __name__ == "__main__":
    main()
