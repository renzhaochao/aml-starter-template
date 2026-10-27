"""Standalone Windows entry point; load external configuration before the app."""

from __future__ import annotations

import argparse
from copy import deepcopy
from logging.handlers import RotatingFileHandler
import logging
import multiprocessing
import os
from pathlib import Path
import secrets
import sys


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="AML Memory Server (Windows x64)")
    parser.add_argument("--host", help="Listen address (default: AML_HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, help="Listen port (default: AML_PORT or 8000)")
    parser.add_argument("--env-file", default=".env", help="Configuration file, relative to the EXE")
    parser.add_argument("--version", action="version", version="AML Memory Server 1.0.0")
    args = parser.parse_args()

    root = application_directory()
    os.chdir(root)
    log_directory = root / "logs"
    log_directory.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        log_directory / "server.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), handler], force=True,
    )
    logger = logging.getLogger("aml.launcher")
    try:
        from dotenv import load_dotenv

        env_file = Path(args.env_file).resolve()
        if not env_file.exists():
            if args.env_file != ".env":
                raise FileNotFoundError(f"Configuration file does not exist: {env_file}")
            # The archive contains no reusable credential or development data.
            # Each deployment gets its own persisted key on first start.
            env_file.write_text(
                "# Generated on first start. Environment variables take priority.\n"
                f"AML_API_KEY={secrets.token_hex(32)}\n"
                "AML_AUTH_MODE=bearer\n"
                "AML_HOST=0.0.0.0\nAML_PORT=8000\n"
                "MEMORY_DB_PATH=./data/memory.db\n"
                "RETRIEVAL_MODE=hybrid\nRETRIEVAL_CANDIDATE_LIMIT=200\n"
                "RETRIEVAL_CONTEXT_WINDOW=1\nRETRIEVAL_CHAR_BUDGET=24000\n",
                encoding="utf-8",
            )
            logger.info("Created %s. Read AML_API_KEY from this file.", env_file)
        load_dotenv(env_file, override=False, encoding="utf-8-sig")
        host = args.host or os.environ.get("AML_HOST", "0.0.0.0")
        port = args.port if args.port is not None else int(os.environ.get("AML_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")

        import uvicorn
        from uvicorn.config import LOGGING_CONFIG
        from app.main import app, get_store

        # Verify writable storage and FTS5 before reporting the server as ready.
        store = get_store()
        config = deepcopy(LOGGING_CONFIG)
        config["handlers"]["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_directory / "http.log"),
            "maxBytes": 10 * 1024 * 1024, "backupCount": 5,
            "encoding": "utf-8", "formatter": "default",
        }
        config["loggers"]["uvicorn"]["handlers"].append("file")
        config["loggers"]["uvicorn.access"]["handlers"].append("file")
        logger.info("Data directory: %s", root / "data")
        logger.info("Starting on %s:%d; configuration: %s", host, port, env_file)
        try:
            # Explicit implementations avoid optional dynamic imports in a frozen app.
            uvicorn.run(
                app, host=host, port=port, workers=1,
                loop="asyncio", http="h11", ws="none", lifespan="on",
                log_config=config,
            )
        finally:
            store.close()
        return 0
    except Exception:
        logger.exception("Startup failed. Check configuration, permissions and port availability.")
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
