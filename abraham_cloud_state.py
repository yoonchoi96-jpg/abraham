from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

STATE_PATHS = (
    "state/abraham.db",
    "state/korean_presentation_cache_v50.json",
    "state/readwise",
    "state/reports",
    "state/vault",
)
MAGIC = b"ABRAHAM1"
SALT_SIZE = 16
NONCE_SIZE = 12

def _key() -> bytes:
    secret = os.getenv("ABRAHAM_STATE_KEY", "").strip()
    if not secret:
        raise RuntimeError("ABRAHAM_STATE_KEY is required")
    return hashlib.sha256(secret.encode("utf-8")).digest()

def _safe_extract(tar: tarfile.TarFile, root: Path) -> None:
    root = root.resolve()
    for member in tar.getmembers():
        target = (root / member.name).resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise RuntimeError(f"Unsafe archive path: {member.name}")
    tar.extractall(root)

def pack(output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "state.tar.gz"
        with tarfile.open(raw, "w:gz") as tar:
            for rel in STATE_PATHS:
                p = Path(rel)
                if p.exists():
                    tar.add(p, arcname=rel)

        salt = os.urandom(SALT_SIZE)
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = AESGCM(_key()).encrypt(nonce, raw.read_bytes(), MAGIC + salt)
        output.write_bytes(MAGIC + salt + nonce + ciphertext)
    return output

def unpack(archive: Path) -> None:
    archive = Path(archive).resolve()
    blob = archive.read_bytes()
    if len(blob) < len(MAGIC) + SALT_SIZE + NONCE_SIZE + 16:
        raise RuntimeError("State archive is too small or corrupt")
    if blob[:len(MAGIC)] != MAGIC:
        raise RuntimeError("Invalid Abraham state archive format")

    offset = len(MAGIC)
    salt = blob[offset:offset + SALT_SIZE]
    offset += SALT_SIZE
    nonce = blob[offset:offset + NONCE_SIZE]
    offset += NONCE_SIZE
    ciphertext = blob[offset:]

    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "state.tar.gz"
        plaintext = AESGCM(_key()).decrypt(nonce, ciphertext, MAGIC + salt)
        raw.write_bytes(plaintext)
        with tarfile.open(raw, "r:gz") as tar:
            _safe_extract(tar, Path.cwd())

def verify() -> None:
    db = Path("state/abraham.db")
    cache = Path("state/korean_presentation_cache_v50.json")
    vault = Path("state/vault")
    if not db.exists() or db.stat().st_size == 0:
        raise RuntimeError("Restored state is missing state/abraham.db")
    if not cache.exists():
        raise RuntimeError("Restored state is missing Korean translation cache")
    if not vault.exists():
        raise RuntimeError("Restored state is missing state/vault")
    print(f"[STATE] verified db={db.stat().st_size} bytes cache={cache.stat().st_size} bytes")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["pack", "unpack", "verify"])
    p.add_argument("--archive", default="abraham-state.tar.gz.enc")
    args = p.parse_args()
    if args.action == "pack":
        print(pack(Path(args.archive)))
    elif args.action == "unpack":
        unpack(Path(args.archive))
    else:
        verify()
