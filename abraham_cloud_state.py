from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

STATE_ROOT = Path(os.getenv("ABRAHAM_STATE_ROOT", ".abraham_state")).resolve()
ARCHIVE_NAME = os.getenv("ABRAHAM_STATE_ARCHIVE", "abraham-state.tar.gz.enc")
KEY_ENV = "ABRAHAM_STATE_KEY"

STATE_PATHS = (
    "state/abraham.db",
    "state/korean_presentation_cache_v50.json",
    "state/readwise",
    "state/reports",
    "state/vault",
)

def _key() -> str:
    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"{KEY_ENV} is required")
    return key

def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def pack(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "state.tar.gz"
        with tarfile.open(raw, "w:gz") as tar:
            for rel in STATE_PATHS:
                p = Path(rel)
                if p.exists():
                    tar.add(p, arcname=rel)
        _run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
              "-pass", f"env:{KEY_ENV}", "-in", str(raw), "-out", str(output)])
    return output

def unpack(archive: Path) -> None:
    if not archive.exists():
        raise FileNotFoundError(archive)
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "state.tar.gz"
        _run(["openssl", "enc", "-d", "-aes-256-gcm", "-pbkdf2",
              "-pass", f"env:{KEY_ENV}", "-in", str(archive), "-out", str(raw)])
        with tarfile.open(raw, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                target = (Path.cwd() / m.name).resolve()
                if not str(target).startswith(str(Path.cwd().resolve()) + os.sep):
                    raise RuntimeError(f"Unsafe archive path: {m.name}")
            tar.extractall(Path.cwd())

def verify() -> None:
    db = Path("state/abraham.db")
    if not db.exists() or db.stat().st_size == 0:
        raise RuntimeError("Restored state is missing state/abraham.db")
    cache = Path("state/korean_presentation_cache_v50.json")
    if not cache.exists():
        raise RuntimeError("Restored state is missing Korean translation cache")
    print(f"[STATE] verified db={db.stat().st_size} bytes cache={cache.stat().st_size} bytes")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["pack", "unpack", "verify"])
    p.add_argument("--archive", default=ARCHIVE_NAME)
    args = p.parse_args()
    if args.action == "pack":
        print(pack(Path(args.archive)))
    elif args.action == "unpack":
        unpack(Path(args.archive))
    else:
        verify()
