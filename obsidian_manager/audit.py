from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .governance import audit_field_ownership

WIKILINK_RE = re.compile(r"\[\[([^|\]#]+)(?:#[^|\]]+)?(?:\|[^\]]+)?\]\]")
MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")

@dataclass
class Issue:
    kind: str
    path: str
    detail: str

def iter_markdown(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.md"):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        yield path

def parse_frontmatter(text: str) -> tuple[dict[str, str], str | None]:
    if not text.startswith("---"):
        return {}, None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..." }), None)
    if end is None:
        return {}, "frontmatter_unclosed"
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return data, f"frontmatter_invalid_line:{line.strip()[:120]}"
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            return data, "frontmatter_empty_key"
        data[key] = value.strip()
    return data, None

def resolve_wikilink(root: Path, source: Path, target: str) -> bool:
    target = target.strip().replace("\\", "/")
    if not target:
        return True
    candidates = [
        root / target, root / f"{target}.md",
        source.parent / target, source.parent / f"{target}.md",
    ]
    return any(p.is_file() for p in candidates)

def resolve_md_link(root: Path, source: Path, target: str) -> bool:
    target = target.strip()
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return True
    target = target.split("#", 1)[0]
    if not target:
        return True
    return (source.parent / target).resolve().is_file() or (root / target).resolve().is_file()

def audit(root: Path) -> dict:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Vault directory does not exist: {root}")

    files = list(iter_markdown(root))
    issues: list[Issue] = []
    stems: defaultdict[str, list[str]] = defaultdict(list)
    ownership: Counter[str] = Counter()
    folders: Counter[str] = Counter()

    for path in files:
        rel = path.relative_to(root).as_posix()
        stems[path.stem.casefold()].append(rel)
        folder = str(path.parent.relative_to(root)) if path.parent != root else "."
        folders[folder] += 1
        content = path.read_text(encoding="utf-8", errors="replace")

        if not content.strip():
            issues.append(Issue("empty_markdown", rel, "Markdown file is empty."))

        frontmatter, fm_error = parse_frontmatter(content)
        if fm_error:
            issues.append(Issue("frontmatter_error", rel, fm_error))

        file_ownership = audit_field_ownership(frontmatter)
        for owner, keys in file_ownership.items():
            ownership[owner] += len(keys)

        for key in file_ownership["unknown"]:
            issues.append(Issue(
                "unknown_metadata_owner",
                rel,
                f"Metadata key {key!r} has no declared owner.",
            ))

        for match in WIKILINK_RE.finditer(content):
            target = match.group(1).strip()
            if not resolve_wikilink(root, path, target):
                issues.append(Issue("broken_wikilink", rel, target))

        for match in MD_LINK_RE.finditer(content):
            target = match.group(1).strip()
            if not resolve_md_link(root, path, target):
                issues.append(Issue("broken_markdown_link", rel, target))

    for stem, paths in stems.items():
        if len(paths) > 1:
            for path in paths:
                issues.append(Issue("duplicate_stem", path, f"Stem {stem!r} appears {len(paths)} times."))

    return {
        "manager_name": "Hendrix",
        "manager_version": "0.2.0",
        "mode": "read_only",
        "vault": str(root),
        "summary": {
            "markdown_files": len(files),
            "issue_count": len(issues),
            "issue_types": dict(Counter(i.kind for i in issues)),
            "folders": len(folders),
        },
        "metadata_ownership": dict(sorted(ownership.items())),
        "issues": [asdict(i) for i in issues],
        "folder_counts": dict(sorted(folders.items())),
        "safety_contract": {
            "abraham_metadata_writable_by_hendrix": False,
            "abel_metadata_writable_by_hendrix": False,
            "unknown_metadata_writable_by_hendrix": False,
            "destructive_operations_enabled": False,
        },
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Hendrix Obsidian Vault audit.")
    parser.add_argument("vault", type=Path)
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--fail-on-issues", action="store_true")
    args = parser.parse_args()

    report = audit(args.vault)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 1 if args.fail_on_issues and report["summary"]["issue_count"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
