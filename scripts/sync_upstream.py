#!/usr/bin/env python3
"""Vendor a filtered snapshot of tsinghua-fib-lab/agentsociety into upstream/agentsociety.

The upstream repository is Apache-2.0 *except* for content that we are not allowed
to redistribute. This script copies the repository at a pinned ref, drops the
excluded paths listed in EXCLUDES (each with the reason it is excluded), and writes
upstream/UPSTREAM.md so the provenance of every vendored file is auditable.

Usage:
    python3 scripts/sync_upstream.py --ref <commit-or-tag> [--source /path/to/clone]

Without --source the script clones https://github.com/tsinghua-fib-lab/agentsociety.
Our own modifications are never made to the vendored tree directly; they live in
upstream/patches/*.patch and are applied at build time (see apply_upstream_patches.sh).
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

UPSTREAM_URL = "https://github.com/tsinghua-fib-lab/agentsociety.git"
REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "upstream" / "agentsociety"
GITIGNORE_RENAMED = ".gitignore.upstream"

# (glob relative to the upstream root, category, reason). Order does not matter.
EXCLUDES: list[tuple[str, str, str]] = [
    (
        "packages/agentsociety/agentsociety/commercial",
        "LICENSE",
        "Proprietary commercial-license folder (Casdoor auth, Kubernetes executor, billing). "
        "Explicitly excluded from Apache-2.0 by the upstream README. Never vendor or read it; "
        "the platform ships clean-room replacements.",
    ),
    (
        "extension/skills/docx",
        "LICENSE",
        "Anthropic document skill marked 'license: Proprietary' (LICENSE.txt not shipped upstream).",
    ),
    (
        "extension/skills/pdf",
        "LICENSE",
        "Anthropic document skill marked 'license: Proprietary' (LICENSE.txt not shipped upstream).",
    ),
    (
        "extension/skills/pptx",
        "LICENSE",
        "Anthropic document skill marked 'license: Proprietary' (LICENSE.txt not shipped upstream).",
    ),
    (
        "extension/skills/xlsx",
        "LICENSE",
        "Anthropic document skill marked 'license: Proprietary' (LICENSE.txt not shipped upstream).",
    ),
    (
        "packages/agentsociety/docs",
        "SIZE",
        "v1 Sphinx docs (~154 MB of screenshots/GIFs). Published at agentsociety.readthedocs.io.",
    ),
    (
        "frontend/public/monaco-editor",
        "SIZE",
        "Pre-built copy of the MIT monaco-editor npm package (~13 MB). Restore with "
        "scripts/restore_frontend_monaco.sh after `npm ci`.",
    ),
    (
        "extension/resources/readme/*.gif",
        "SIZE",
        "Marketplace README demo GIFs (~11 MB); only referenced from the upstream README.",
    ),
    (
        "extension/resources/walkthrough/images/gif/init-0*.gif",
        "SIZE",
        "Walkthrough GIFs (~33 MB) that the walkthrough links to on github.com, not embeds.",
    ),
    (".git", "VCS", "Upstream git metadata."),
    (".github", "CI", "Upstream CI/Dependabot configuration (not used by this platform)."),
    (".gitlab-ci.yml", "CI", "Upstream GitLab CI configuration (not used by this platform)."),
    ("**/__pycache__", "BUILD", "Python bytecode caches."),
    ("**/*.pyc", "BUILD", "Python bytecode."),
    ("**/node_modules", "BUILD", "Installed npm dependencies."),
]


# Credentials that upstream hard-codes. They belong to upstream's own accounts, so they
# are neither redistributed nor used here; the code paths read our own values from
# configuration instead (see upstream/patches). (pattern, replacement, reason)
REDACTIONS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\b(?:pk|sk|tk)\.eyJ[A-Za-z0-9._-]{20,}"),
        "",
        "Mapbox access token of the upstream project's account (GitHub push protection flags it). "
        "Provide your own via MAPBOX_ACCESS_TOKEN (extension build) / VITE_MAPBOX_ACCESS_TOKEN (frontend).",
    ),
]
REDACT_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".py", ".json", ".md", ".yaml", ".yml", ".toml", ".html"}


def _redact_tree(root: Path) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix not in REDACT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = text
        for pattern, replacement, reason in REDACTIONS:
            new_text, count = pattern.subn(replacement, new_text)
            if count:
                hits.append((path.relative_to(root).as_posix(), count, reason))
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
    return hits


def _is_excluded(rel: str) -> tuple[str, str, str] | None:
    rel_posix = rel.replace(os.sep, "/")
    for pattern, category, reason in EXCLUDES:
        if pattern.startswith("**/"):
            tail = pattern[3:]
            if fnmatch.fnmatch(rel_posix.rsplit("/", 1)[-1], tail):
                return pattern, category, reason
        if fnmatch.fnmatch(rel_posix, pattern) or rel_posix == pattern:
            return pattern, category, reason
        if rel_posix.startswith(pattern.rstrip("/") + "/"):
            return pattern, category, reason
    return None


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _copy_filtered(src_root: Path, dst_root: Path) -> tuple[int, dict[str, int]]:
    copied = 0
    skipped: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel_dir = os.path.relpath(dirpath, src_root)
        rel_dir = "" if rel_dir == "." else rel_dir
        kept_dirs = []
        for d in dirnames:
            rel = os.path.join(rel_dir, d) if rel_dir else d
            hit = _is_excluded(rel)
            if hit:
                skipped[hit[0]] = skipped.get(hit[0], 0) + 1
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for f in filenames:
            rel = os.path.join(rel_dir, f) if rel_dir else f
            hit = _is_excluded(rel)
            if hit:
                skipped[hit[0]] = skipped.get(hit[0], 0) + 1
                continue
            src = Path(dirpath) / f
            dst = dst_root / rel
            if f == ".gitignore":
                # Upstream ignore rules would hide upstream's own tracked files once
                # nested in this repo; keep their content under a neutral name.
                dst = dst.with_name(GITIGNORE_RENAMED)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                target = os.readlink(src)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                os.symlink(target, dst)
            else:
                shutil.copy2(src, dst)
            copied += 1
    return copied, skipped


def _write_provenance(
    sha: str,
    committed_at: str,
    ref: str,
    copied: int,
    skipped: dict[str, int],
    redacted: list[tuple[str, int, str]],
) -> None:
    lines = [
        "# Vendored upstream: tsinghua-fib-lab/agentsociety",
        "",
        "This directory is a **filtered, unmodified snapshot** of the upstream AgentSociety",
        "repository. It is regenerated by `scripts/sync_upstream.py`; do not edit files under",
        "`upstream/agentsociety/` by hand. Platform-specific changes live in",
        "`upstream/patches/*.patch` and are applied at build time.",
        "",
        "| Field | Value |",
        "| ----- | ----- |",
        f"| Source | {UPSTREAM_URL} |",
        f"| Requested ref | `{ref}` |",
        f"| Commit | `{sha}` |",
        f"| Commit date | {committed_at} |",
        f"| Synced at | {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} |",
        f"| Files copied | {copied} |",
        "",
        "## License",
        "",
        "Apache License 2.0 — see `agentsociety/LICENSE` (copyright FIB LAB, Tsinghua University).",
        "The upstream README states the whole repository is Apache-2.0 **except**",
        "`packages/agentsociety/commercial`, which is excluded below.",
        "",
        "## Excluded paths",
        "",
        "| Path pattern | Category | Entries skipped | Reason |",
        "| ------------ | -------- | --------------- | ------ |",
    ]
    for pattern, category, reason in EXCLUDES:
        lines.append(f"| `{pattern}` | {category} | {skipped.get(pattern, 0)} | {reason} |")
    lines += [
        "",
        "Entries under `LICENSE` must never be re-added: they are not licensed for redistribution.",
        "",
        "## Other deviations from a byte-for-byte copy",
        "",
        f"* Every nested `.gitignore` is stored as `{GITIGNORE_RENAMED}`. Upstream's ignore",
        "  rules would otherwise hide files that upstream itself tracks (e.g. `examples/`,",
        "  `package.json`). `upstream/.gitignore` defines what is ignored inside this tree.",
        "* Hard-coded credentials of upstream's own accounts are replaced by an empty string:",
        "",
        "| File | Occurrences | Reason |",
        "| ---- | ----------- | ------ |",
    ]
    lines += [f"| `{path}` | {count} | {reason} |" for path, count, reason in redacted] or ["| (none) | 0 | |"]
    lines += [""]
    (REPO_ROOT / "upstream" / "UPSTREAM.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", required=True, help="Upstream commit SHA or tag to vendor")
    parser.add_argument("--source", help="Existing local clone to read from (skips cloning)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="agentsociety-sync-") as tmp:
        if args.source:
            src = Path(args.source).resolve()
        else:
            src = Path(tmp) / "agentsociety"
            subprocess.run(["git", "clone", "--quiet", UPSTREAM_URL, str(src)], check=True)
        _git(["checkout", "--quiet", args.ref], src)
        sha = _git(["rev-parse", "HEAD"], src)
        committed_at = _git(["log", "-1", "--format=%cI"], src)

        if TARGET.exists():
            shutil.rmtree(TARGET)
        TARGET.mkdir(parents=True)
        copied, skipped = _copy_filtered(src, TARGET)
        redacted = _redact_tree(TARGET)

    # Defence in depth: the proprietary folders must never be present after a sync.
    for forbidden in (
        TARGET / "packages/agentsociety/agentsociety/commercial",
        TARGET / "extension/skills/docx",
        TARGET / "extension/skills/pdf",
        TARGET / "extension/skills/pptx",
        TARGET / "extension/skills/xlsx",
    ):
        if forbidden.exists():
            print(f"refusing to continue: {forbidden} was vendored", file=sys.stderr)
            return 1

    _write_provenance(sha, committed_at, args.ref, copied, skipped, redacted)
    print(f"Vendored {copied} files from {sha[:12]} into {TARGET.relative_to(REPO_ROOT)}")
    for pattern, count in sorted(skipped.items()):
        print(f"  skipped {count:>4} x {pattern}")
    for path, count, _ in redacted:
        print(f"  redacted {count} credential(s) in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
