from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

from abraham_cloud_state import pack

def copy_if_exists(src: Path, dst: Path, required: bool = False) -> None:
    if not src.exists():
        if required:
            raise FileNotFoundError(f"Required state path not found: {src}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)

def main() -> None:
    p = argparse.ArgumentParser(description="Build the initial encrypted Abraham cloud state.")
    p.add_argument("--db", default=os.getenv("ABRAHAM_DB_PATH", ""))
    p.add_argument("--vault", default=os.getenv("ABRAHAM_VAULT_PATH", ""))
    p.add_argument("--cache", default=os.getenv("ABRAHAM_KOREAN_CACHE_PATH", ""))
    p.add_argument("--reports", default=os.getenv("ABRAHAM_REPORT_DIR", ""))
    p.add_argument("--output", default="abraham-state-bootstrap.tar.gz.enc")
    args = p.parse_args()

    if not args.db:
        raise SystemExit("--db or ABRAHAM_DB_PATH is required")
    if not args.vault:
        raise SystemExit("--vault or ABRAHAM_VAULT_PATH is required")
    if not args.cache:
        raise SystemExit("--cache or ABRAHAM_KOREAN_CACHE_PATH is required")

    db = Path(args.db).expanduser().resolve()
    vault = Path(args.vault).expanduser().resolve()
    cache = Path(args.cache).expanduser().resolve()
    reports = Path(args.reports).expanduser().resolve() if args.reports else None

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        os.chdir(root)
        copy_if_exists(db, root / "state/abraham.db", required=True)
        copy_if_exists(cache, root / "state/korean_presentation_cache_v50.json", required=True)
        copy_if_exists(vault, root / "state/vault", required=True)
        if reports:
            copy_if_exists(reports, root / "state/reports")
        out = Path(args.output).expanduser().resolve()
        pack(out)
        print(f"BOOTSTRAP_STATE={out}")
        print(f"SIZE_BYTES={out.stat().st_size}")

if __name__ == "__main__":
    main()
