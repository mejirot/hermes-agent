"""Inspect a prepared sender without importing Hermes or installing dependencies."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import sys
import tomllib


def inventory(root: Path) -> dict:
    lock = root / "uv.lock"
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    expected = {row["name"]: row["version"] for row in packages if "registry" in row.get("source", {})}
    actual = {re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != expected:
        raise ValueError("prepared sender distributions differ from its lock")
    if sys.version_info[:2] != (3, 14) or platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("sender interpreter must be Linux x86_64 CPython 3.14")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schema": 1, "component": "hermes-web-push-sender", "python": platform.python_version(),
            "platform": "linux/amd64", "lock_sha256": sha(lock),
            "script_sha256": sha(root / "send.py"), "packages": actual}


if __name__ == "__main__":
    print(json.dumps(inventory(Path(sys.prefix)), sort_keys=True, separators=(",", ":")))
