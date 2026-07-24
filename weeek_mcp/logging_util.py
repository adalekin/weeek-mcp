"""Diagnostic logging to a file, not just stderr.

Opt-in via WEEEK_DEBUG_LOG=1 (see config.py): MCP hosts vary in whether they
capture a locally-run extension's stderr; Claude Desktop was observed wiring
the subprocess's fd 2 to /dev/null, which silently swallows debug output.
Writing to a file we control means it survives regardless.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def make_logger(log_path: Path | None, tag: str):
    if log_path is None:
        return lambda msg: None

    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{tag}] {msg}"
        try:
            with log_path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line, file=sys.stderr, flush=True)

    return _log
