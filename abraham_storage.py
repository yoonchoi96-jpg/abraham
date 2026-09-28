from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from abraham_sqlite import get_db_path


def _path_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser()


DB_PATH = get_db_path()
VAULT_PATH = _path_env("ABRAHAM_VAULT_PATH", Path.home() / "Invest Insight")
REPORT_DIR = _path_env("ABRAHAM_REPORT_DIR", VAULT_PATH / "90_Dashboard" / "auto_reports")
LOG_DIR = _path_env("ABRAHAM_LOG_DIR", REPORT_DIR / "logs")
BACKUP_ROOT = _path_env("ABRAHAM_BACKUP_PATH", REPORT_DIR / "backups")
ARCHIVE_ROOT = _path_env("ABRAHAM_ARCHIVE_PATH", BACKUP_ROOT / "archive")

WARN_PCT = float(os.getenv("ABRAHAM_STORAGE_WARN_PCT", "80"))
CRITICAL_PCT = float(os.getenv("ABRAHAM_STORAGE_CRITICAL_PCT", "90"))
BACKUP_RETENTION = int(os.getenv("ABRAHAM_BACKUP_RETENTION", "14"))
LEGACY_REPORT_RETENTION_DAYS = int(
    os.getenv("ABRAHAM_LEGACY_REPORT_RETENTION_DAYS", "7")
)
V5_REPORT_RETENTION_DAYS = int(
    os.getenv("ABRAHAM_V5_REPORT_RETENTION_DAYS", "3")
)
LOG_RETENTION_DAYS = int(os.getenv("ABRAHAM_LOG_RETENTION_DAYS", "14"))
BACKUP_COMPRESS_DAYS = int(os.getenv("ABRAHAM_BACKUP_COMPRESS_DAYS", "2"))


def size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def disk_usage(path: Path) -> dict:
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    used_pct = usage.used / usage.total * 100 if usage.total else 0
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_pct": round(used_pct, 2),
        "warning": used_pct >= WARN_PCT,
        "critical": used_pct >= CRITICAL_PCT,
    }


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def storage_status() -> dict:
    roots = {
        "sqlite": DB_PATH,
        "vault": VAULT_PATH,
        "reports": REPORT_DIR,
        "logs": LOG_DIR,
        "backups": BACKUP_ROOT,
        "archive": ARCHIVE_ROOT,
    }
    result = {"version": "5.6.0", "disk": disk_usage(VAULT_PATH), "paths": {}}
    for name, path in roots.items():
        result["paths"][name] = {
            "path": str(path),
            "exists": path.exists(),
            "bytes": size_bytes(path),
            "human": _human(size_bytes(path)),
        }
    return result


def sqlite_backup(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _compress_file(path: Path) -> Path:
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists() and gz.stat().st_mtime >= path.stat().st_mtime:
        return gz
    with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return gz


def _prune_report_files(dry_run: bool) -> dict:
    """
    Report 전용 retention.

    절대 건드리지 않는 것:
    - SQLite DB
    - Vault markdown
    - caches
    - backup directories
    - diagnostic / translation cache

    정리 대상:
    - legacy readwise_auto_v*.json -> 7일
    - V5.6 readwise_auto_v5.6_*.json -> 3일
    """
    if not REPORT_DIR.exists():
        return {
            "legacy_removed": 0,
            "v5_removed": 0,
            "legacy_candidates": 0,
            "v5_candidates": 0,
        }

    now = time.time()
    legacy_cutoff = now - LEGACY_REPORT_RETENTION_DAYS * 86400
    v5_cutoff = now - V5_REPORT_RETENTION_DAYS * 86400

    legacy_removed = 0
    v5_removed = 0
    legacy_candidates = 0
    v5_candidates = 0

    for p in REPORT_DIR.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue

        name = p.name

        # V5.6 reports: only exact generated pattern.
        if name.startswith("readwise_auto_v5.6_"):
            try:
                if p.stat().st_mtime < v5_cutoff:
                    v5_candidates += 1
                    if not dry_run:
                        p.unlink()
                    v5_removed += 1
            except OSError:
                pass
            continue

        # Legacy V2/V3/V4 reports only.
        if name.startswith(("readwise_auto_v2_", "readwise_auto_v3_",
                            "readwise_auto_v3.", "readwise_auto_v4_",
                            "readwise_auto_v4.1_", "readwise_auto_v4.2_",
                            "readwise_auto_v4.2.1_", "readwise_auto_v4.3_",
                            "readwise_auto_v4.4_", "readwise_auto_v4.4.9_",
                            "readwise_auto_v4.5_", "readwise_auto_v4.5.2_",
                            "readwise_auto_v4.6_")):
            try:
                if p.stat().st_mtime < legacy_cutoff:
                    legacy_candidates += 1
                    if not dry_run:
                        p.unlink()
                    legacy_removed += 1
            except OSError:
                pass

    return {
        "legacy_removed": legacy_removed,
        "v5_removed": v5_removed,
        "legacy_candidates": legacy_candidates,
        "v5_candidates": v5_candidates,
    }


def _prune_old_files(root: Path, older_than_days: int, dry_run: bool) -> int:
    if not root.exists():
        return 0
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                if not dry_run:
                    p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _prune_backups(dry_run: bool) -> int:
    if not BACKUP_ROOT.exists():
        return 0
    candidates = [p for p in BACKUP_ROOT.iterdir() if p.name.startswith("v5_backup_") and p.is_dir()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in candidates[BACKUP_RETENTION:]:
        if not dry_run:
            shutil.rmtree(old)
        removed += 1
    return removed


def _compress_old_backups(dry_run: bool) -> int:
    if not BACKUP_ROOT.exists():
        return 0
    cutoff = time.time() - BACKUP_COMPRESS_DAYS * 86400
    count = 0
    for p in BACKUP_ROOT.rglob("*"):
        if not p.is_file() or p.suffix == ".gz":
            continue
        try:
            if p.stat().st_mtime < cutoff:
                if not dry_run:
                    _compress_file(p)
                count += 1
        except OSError:
            pass
    return count


def run_storage_maintenance(*, dry_run: bool = True) -> dict:
    before = storage_status()
    actions = {
        "compressed_old_backup_files": _compress_old_backups(dry_run),
        "removed_old_backup_snapshots": _prune_backups(dry_run),
        "report_prune": _prune_report_files(dry_run),
        "removed_old_logs": _prune_old_files(LOG_DIR, LOG_RETENTION_DAYS, dry_run),
    }
    after = storage_status() if not dry_run else before
    return {
        "version": "5.6.0",
        "dry_run": dry_run,
        "before": before,
        "actions": actions,
        "after": after,
        "policy": {
            "warning_pct": WARN_PCT,
            "critical_pct": CRITICAL_PCT,
            "backup_retention": BACKUP_RETENTION,
            "legacy_report_retention_days": LEGACY_REPORT_RETENTION_DAYS,
            "v5_report_retention_days": V5_REPORT_RETENTION_DAYS,
            "log_retention_days": LOG_RETENTION_DAYS,
            "backup_compress_days": BACKUP_COMPRESS_DAYS,
        },
    }
