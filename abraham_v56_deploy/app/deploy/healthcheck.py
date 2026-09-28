from __future__ import annotations
import os, sqlite3, sys, time
from pathlib import Path


def env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


def main() -> int:
    db = env_path("ABRAHAM_DB_PATH", "/opt/abraham/data/abraham.db")
    vault = env_path("ABRAHAM_VAULT_PATH", "/opt/abraham/vault")
    reports = env_path("ABRAHAM_REPORT_DIR", "/opt/abraham/reports")
    lock = reports / ".abraham_v5.lock"
    failures = []

    if not db.is_file(): failures.append(f"DB missing: {db}")
    else:
        try:
            with sqlite3.connect(db, timeout=5) as conn:
                conn.execute("PRAGMA quick_check").fetchone()
                row = conn.execute("SELECT MAX(completed_at) FROM document_stages WHERE status='completed'").fetchone()
                print("last_completed_stage:", row[0] if row else None)
        except Exception as exc:
            failures.append(f"DB check failed: {exc}")

    for label, path in (("vault", vault), ("reports", reports)):
        if not path.exists(): failures.append(f"{label} missing: {path}")
        elif not os.access(path, os.W_OK): failures.append(f"{label} not writable: {path}")

    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        print(f"lock_age_seconds: {age:.0f}")
        if age > 3 * 3600:
            failures.append(f"stale lock: {lock}")

    usage = __import__('shutil').disk_usage(reports if reports.exists() else Path('/'))
    free_gb = usage.free / (1024**3)
    print(f"free_disk_gb: {free_gb:.2f}")
    if free_gb < 5: failures.append("less than 5 GB free disk")

    if failures:
        print("HEALTH: FAIL")
        for x in failures: print("-", x)
        return 1
    print("HEALTH: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
