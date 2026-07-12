# GENERATED — do not edit here. Source: model-registry/tools/check_attestations.py
# Re-sync with: model-registry/sync_evals.sh /Users/bryanniyonzima/AppsinTesting/swingtrader
#!/usr/bin/env python3
"""attestation-check — the CI side of Problem B v2 (design §6, Decision 4).

Verifies that every model swap an app repo has committed is backed by a
still-valid eval attestation. Stdlib-only; the verify logic itself comes from
the vendored model_evals package (evals/vendor/ — synced by sync_evals.sh),
so this script never drifts from the library that wrote the attestation.

For each evals/attestations/*.json:
  1. resolve the model the repo actually commits: att.config_file (a JSON
     file) at att.config_key (dotted path, digits index lists);
  2. verify the attestation against the *vendored* models.json — a registry
     re-sync that changes the export invalidates every attestation (Problem B
     ties back to Problem A);
  3. enforce the rubric rule: evals/rubrics.json supplies the current hash;
     a judge-gated attestation with no rubrics entry FAILS (the rule can't be
     disabled by deleting its input);
  4. enforce HMAC when MODEL_EVALS_ATTEST_KEY is set (repo/CI secret).

Anything under evals/attestations/ that isn't a top-level *.json is an error
— an attestation "archived" into a subfolder must not silently un-gate.

No attestations directory ⇒ exit 0 with a note (the gate engages once the
first attestation lands — incremental adoption, same as the model linter).

Known limits (attestation-driven gating, accepted for the solo-dev threat
model — see PR #2 review): deleting an attestation un-gates its task; the
vendored verify logic and the unkeyed seal are editable by anyone who can
commit. The HMAC key raises the bar when configured, but GitHub does not
expose secrets to fork PRs, so enforcement there happens post-merge.

Usage: python3 tools/check_attestations.py [repo-root] [--now YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import date


def _fail(msg: str):
    print(f"attestation-check: error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _resolve_committed_model(root: str, config_file: str, config_key: str):
    path = os.path.join(root, config_file)
    if not os.path.exists(path):
        return None, f"config file {config_file!r} not found"
    try:
        with open(path, "r", encoding="utf-8") as f:
            node = json.load(f)
    except Exception as e:  # unparseable config is a gate failure, not a crash
        return None, f"config file {config_file!r} unreadable: {e}"
    for part in config_key.split("."):
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None, f"config key {config_key!r} not found in {config_file!r}"
    if not isinstance(node, str):
        return None, f"config key {config_key!r} in {config_file!r} is not a string model id"
    return node, None


def _strays(att_dir: str, matched: list[str]) -> list[str]:
    """Anything under evals/attestations/ that the glob didn't match."""
    matched_set = {os.path.abspath(p) for p in matched}
    out = []
    for dirpath, _dirnames, filenames in os.walk(att_dir):
        for name in filenames:
            p = os.path.abspath(os.path.join(dirpath, name))
            if p not in matched_set and name != ".gitkeep":
                out.append(os.path.relpath(p, att_dir))
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="check_attestations",
        description="Verify committed model swaps against eval attestations.",
    )
    p.add_argument("root", nargs="?", default=".", help="repo root (default: cwd)")
    p.add_argument("--now", default=None, metavar="YYYY-MM-DD",
                   help="override today, for testing expiry")
    args = p.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        _fail(f"repo root {args.root!r} does not exist")
    now = date.fromisoformat(args.now) if args.now else None

    att_dir = os.path.join(root, "evals", "attestations")
    att_paths = sorted(glob.glob(os.path.join(att_dir, "*.json")))
    if not os.path.isdir(att_dir) or not (att_paths or _strays(att_dir, att_paths)):
        print("attestation-check: no evals/attestations/*.json — gate not engaged (OK)")
        return 0

    vendor = os.path.join(root, "evals", "vendor")
    if not os.path.isdir(os.path.join(vendor, "model_evals")):
        _fail("evals/vendor/model_evals missing — run model-registry/sync_evals.sh on this repo")
    sys.path.insert(0, vendor)
    from model_evals.attest import load, verify_attestation  # noqa: E402
    from model_evals.catalog import Catalog  # noqa: E402

    models_json = os.path.join(vendor, "models.json")
    if not os.path.exists(models_json):
        _fail("evals/vendor/models.json missing — run model-registry/sync_evals.sh on this repo")
    catalog_hash = Catalog.load(models_json).hash()

    if not os.environ.get("MODEL_EVALS_ATTEST_KEY"):
        print("attestation-check: note: MODEL_EVALS_ATTEST_KEY unset — "
              "verifying integrity (seal) only, not signatures")

    rubrics = {}
    rubrics_path = os.path.join(root, "evals", "rubrics.json")
    if os.path.exists(rubrics_path):
        with open(rubrics_path, "r", encoding="utf-8") as f:
            rubrics = json.load(f)

    bad = 0

    for stray in _strays(att_dir, att_paths):
        print(f"✗ evals/attestations/{stray}: unrecognized file — attestations must be "
              "top-level *.json (moving one aside does not un-gate its task)")
        bad += 1

    for path in att_paths:
        rel = os.path.relpath(path, root)
        try:
            att = load(path)
            committed, err = _resolve_committed_model(
                root, att.get("config_file", ""), att.get("config_key", "")
            )
            if err:
                print(f"✗ {rel}: {err}")
                bad += 1
                continue
            failures = verify_attestation(
                att,
                committed_model=committed,
                catalog_hash=catalog_hash,
                current_rubric_hash=rubrics.get(att.get("task", "")),
                now=now,
            )
        except Exception as e:
            # One malformed attestation must fail loudly without aborting the
            # scan of the rest.
            print(f"✗ {rel}: verification crashed: {type(e).__name__}: {e}")
            bad += 1
            continue

        if failures:
            print(f"✗ {rel}: {att.get('task')} → {att.get('model')} INVALID")
            for f_ in failures:
                print(f"    - {f_}")
            bad += 1
        else:
            print(f"✓ {rel}: {att.get('task')} → {committed} "
                  f"(expires {att.get('expires_at')}, n={att.get('n')})")

    total = len(att_paths)
    if bad:
        print(f"\nattestation-check: {bad} problem(s) across {total} attestation(s). "
              "Re-run the eval (model-registry/model_evals) and refresh the attestation, "
              "or revert the config to the incumbent.")
        return 1
    print(f"attestation-check: {total} attestation(s) valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
