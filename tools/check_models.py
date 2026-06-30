# GENERATED — do not edit here. Source: model-registry/check_models.py
# Re-sync with: model-registry/sync.sh /Users/bryanniyonzima/AppsinTesting/swingtrader
#!/usr/bin/env python3
"""
check_models.py — LLM model-ID linter.

CANONICAL COPY lives in the `model-registry` repo. When this file is vendored
into another repo (tools/check_models.py), it is a generated copy — DO NOT edit
it there. Edit the canonical file and run `./sync.sh` to push updates out.

What it does: scans a codebase for hardcoded LLM model-ID strings and flags any
that are retired, deprecated, a preview alias, or eligible for a free upgrade
(same price, newer model). Catches the failure mode where a model gets retired
upstream and a deployed service starts 404-ing, or where you're paying for an
older model when a same-price better one exists.

Usage:
    python check_models.py [PATH]            # scan PATH (default: .)
    python check_models.py . --strict        # also fail on preview/upgrade warnings
    python check_models.py . --json          # machine-readable output

Exit codes:
    0  clean (or only warnings, without --strict)
    1  found retired/deprecated models (or any finding with --strict)

Registry last reviewed: 2026-06-30. Sources: claude-api skill (Anthropic,
authoritative) + web research (Google Gemini, June 2026).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ─────────────────────────────────────────────────────────────────────────────
# REGISTRY — the single source of truth.
#
# status: "current"    — fine, use freely
#         "preview"    — preview/experimental alias; pin to the GA id (`replace`)
#         "deprecated" — still works but scheduled for retirement; migrate
#         "retired"    — 404s now; MUST migrate
# replace:  recommended target id (migration / pin target)
# upgrade:  same-price newer model — free quality win (not urgent)
# note:     short human context (price, retire date, etc.)
# ─────────────────────────────────────────────────────────────────────────────
REGISTRY: dict[str, dict] = {
    # ── Anthropic ───────────────────────────────────────────────────────────
    "claude-fable-5":            {"provider": "anthropic", "status": "current", "note": "flagship, $10/$50"},
    "claude-mythos-5":           {"provider": "anthropic", "status": "current", "note": "Project Glasswing only"},
    "claude-opus-4-8":           {"provider": "anthropic", "status": "current", "note": "current Opus, $5/$25"},
    "claude-opus-4-7":           {"provider": "anthropic", "status": "current", "upgrade": "claude-opus-4-8", "note": "$5/$25; 4-8 is same price"},
    "claude-opus-4-6":           {"provider": "anthropic", "status": "current", "upgrade": "claude-opus-4-8", "note": "$5/$25; 4-8 is same price, free win"},
    "claude-opus-4-5":           {"provider": "anthropic", "status": "current", "upgrade": "claude-opus-4-8", "note": "legacy-active"},
    "claude-opus-4-1":           {"provider": "anthropic", "status": "deprecated", "replace": "claude-opus-4-8", "note": "retires 2026-08-05"},
    "claude-opus-4-0":           {"provider": "anthropic", "status": "deprecated", "replace": "claude-opus-4-8", "note": "retires 2026-06-15"},
    "claude-sonnet-4-6":         {"provider": "anthropic", "status": "current", "note": "current Sonnet, $3/$15"},
    "claude-sonnet-4-5":         {"provider": "anthropic", "status": "current", "upgrade": "claude-sonnet-4-6", "note": "legacy-active; 4-6 is same price"},
    "claude-sonnet-4-0":         {"provider": "anthropic", "status": "deprecated", "replace": "claude-sonnet-4-6", "note": "retires 2026-06-15"},
    "claude-haiku-4-5":          {"provider": "anthropic", "status": "current", "note": "current Haiku, $1/$5"},
    "claude-3-7-sonnet":         {"provider": "anthropic", "status": "retired", "replace": "claude-sonnet-4-6", "note": "retired 2026-02-19"},
    "claude-3-5-sonnet":         {"provider": "anthropic", "status": "retired", "replace": "claude-sonnet-4-6", "note": "retired 2025-10-28"},
    "claude-3-5-haiku":          {"provider": "anthropic", "status": "retired", "replace": "claude-haiku-4-5", "note": "retired 2026-02-19"},
    "claude-3-opus":             {"provider": "anthropic", "status": "retired", "replace": "claude-opus-4-8", "note": "retired 2026-01-05"},
    "claude-3-haiku":            {"provider": "anthropic", "status": "deprecated", "replace": "claude-haiku-4-5", "note": "retires 2026-04-19"},
    # ── Google Gemini ─────────────────────────────────────────────────────────
    "gemini-3.5-flash":          {"provider": "google", "status": "current", "note": "newest Flash, $1.50/$9"},
    "gemini-3.1-pro":            {"provider": "google", "status": "current", "note": "flagship, $2/$12"},
    "gemini-3.1-pro-preview":    {"provider": "google", "status": "preview", "replace": "gemini-3.1-pro", "note": "preview alias — pin to GA"},
    "gemini-3-flash":            {"provider": "google", "status": "current", "note": "$0.50/$3"},
    "gemini-3-flash-preview":    {"provider": "google", "status": "preview", "replace": "gemini-3-flash", "note": "preview alias; pin to GA (gemini-3.5-flash is newer)"},
    "gemini-3.1-flash-lite":     {"provider": "google", "status": "current", "note": "cheap tier, $0.25/$1.50"},
    "gemini-3.1-flash-lite-preview": {"provider": "google", "status": "preview", "replace": "gemini-3.1-flash-lite", "note": "preview alias — pin to GA"},
    "gemini-2.5-pro":            {"provider": "google", "status": "current", "upgrade": "gemini-3.1-pro", "note": "prev-gen, active"},
    "gemini-2.5-flash":          {"provider": "google", "status": "current", "note": "active, $0.30/$2.50"},
    "gemini-2.5-flash-lite":     {"provider": "google", "status": "current", "note": "cheapest, $0.10 in"},
    "gemini-2.5-flash-image":    {"provider": "google", "status": "current", "note": "image generation"},
    "gemini-2.0-flash-exp":      {"provider": "google", "status": "deprecated", "replace": "gemini-2.5-flash", "note": "experimental, being removed"},
    "gemini-2.0-flash":          {"provider": "google", "status": "deprecated", "replace": "gemini-2.5-flash", "note": "deprecating 2026-06-01"},
}

# Order matters: longer / more-specific ids first so "gemini-2.0-flash-exp"
# wins over "gemini-2.0-flash", and "claude-3-5-sonnet" is matched before a bare
# "claude-3" prefix would be. We sort keys by length descending at match time.
_KNOWN = sorted(REGISTRY.keys(), key=len, reverse=True)

# A loose pattern to catch *any* model-looking string, so we can warn on ids we
# don't recognise (newly shipped, or a typo) rather than silently passing them.
_MODELISH = re.compile(
    r"\b(?:claude-(?:opus|sonnet|haiku|fable|mythos|instant|[0-9])[a-z0-9.\-]*"
    r"|gemini-[0-9][a-z0-9.\-]*|gpt-[0-9o][a-z0-9.\-]*|o[1345]-[a-z0-9.\-]+)\b",
    re.IGNORECASE,
)

# `claude-3-5-sonnet-latest`, `claude-opus-4-8`, `gemini-3.1-pro-preview`, dated
# snapshots like `claude-haiku-4-5-20251001` all normalise to a registry key by
# stripping a trailing `-latest`, `-YYYYMMDD`, or `@YYYYMMDD`.
_DATE_SUFFIX = re.compile(r"(?:[-@]\d{8}|[-@]\d{4}-\d{2}-\d{2}|-latest)$", re.IGNORECASE)

SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".yaml", ".yml", ".sh"}
SCAN_ENV_PREFIX = ".env"
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "env", "dist", "build", "__pycache__",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", "vendor",
    ".worktrees", ".hermes-worktrees", "worktrees", "logs",
}
SKIP_FILE_SUBSTR = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "check_models.py", "models.json")


def _normalise(token: str) -> str:
    return _DATE_SUFFIX.sub("", token.lower())


def classify(token: str):
    """Return (registry_key, entry) for a found model-ish token, or (None, None)."""
    norm = _normalise(token)
    if norm in REGISTRY:
        return norm, REGISTRY[norm]
    # substring fall-through: e.g. an id embedded in a URL path
    for key in _KNOWN:
        if key in norm:
            return key, REGISTRY[key]
    return None, None


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if any(s in fn for s in SKIP_FILE_SUBSTR):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in SCAN_EXTS or fn.startswith(SCAN_ENV_PREFIX):
                yield os.path.join(dirpath, fn)


def scan(root: str):
    findings = []
    for path in iter_files(root):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for m in _MODELISH.finditer(line):
                token = m.group(0)
                key, entry = classify(token)
                rel = os.path.relpath(path, root)
                if entry is None:
                    findings.append({
                        "level": "unknown", "file": rel, "line": lineno,
                        "token": token, "status": "unknown", "key": None,
                        "message": "not in registry — verify it's a real, current id",
                    })
                    continue
                status = entry["status"]
                if status == "retired":
                    lvl = "error"
                    msg = f"RETIRED ({entry.get('note','')}) → use {entry.get('replace','?')}"
                elif status == "deprecated":
                    lvl = "error"
                    msg = f"DEPRECATED ({entry.get('note','')}) → migrate to {entry.get('replace','?')}"
                elif status == "preview":
                    lvl = "warn"
                    msg = f"PREVIEW alias ({entry.get('note','')}) → pin to {entry.get('replace','?')}"
                elif "upgrade" in entry:
                    lvl = "warn"
                    msg = f"free upgrade available → {entry['upgrade']} ({entry.get('note','')})"
                else:
                    lvl = "ok"
                    msg = entry.get("note", "current")
                findings.append({
                    "level": lvl, "file": rel, "line": lineno, "token": token,
                    "status": status, "key": key, "message": msg,
                })
    return findings


_ICON = {"error": "❌", "warn": "⚠️ ", "unknown": "❓", "ok": "✅"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Lint hardcoded LLM model IDs against the registry.")
    p.add_argument("path", nargs="?", default=".", help="directory to scan (default: .)")
    p.add_argument("--strict", action="store_true", help="fail on preview/upgrade/unknown warnings too")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--quiet", action="store_true", help="only print problems, not ✅ lines")
    args = p.parse_args(argv)

    findings = scan(args.path)

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        shown = [f for f in findings if not (args.quiet and f["level"] == "ok")]
        if not shown:
            print("✅ no model-ID issues found")
        for f in sorted(shown, key=lambda x: (x["file"], x["line"])):
            icon = _ICON.get(f["level"], "•")
            print(f"{icon} {f['file']}:{f['line']}  {f['token']}  — {f['message']}")

    errors = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] in ("warn", "unknown")]
    if not args.json:
        print(f"\nsummary: {len(errors)} error(s), {len(warns)} warning(s)")
    if errors:
        return 1
    if args.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
