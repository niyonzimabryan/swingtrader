"""attest — v2 of Problem B: the eval's verdict as a *verifiable artifact*
(design §6 "v2 — attestation + CI gate", Decision 4).

v1 was report-driven: a human reads the report and edits config. v2 makes the
link checkable: a powered PROMOTE is written as an attestation JSON committed
to the app repo, and CI verifies that the model the config commits to is
backed by a still-valid attestation. An attestation invalidates when:

  - the committed model doesn't match the attested one,
  - it has expired (default TTL 90 days),
  - the registry changed (`models_json_hash` stale — ties Problem B back to A),
  - the judge rubric changed (`rubric_hash` stale, when a judge gated the call),
  - its corpus was below the task's statistical floor (n < min_n),
  - the payload was edited after sealing.

Integrity vs authenticity: every attestation carries a SHA-256 `seal` over the
canonical payload (tamper-evidence, verifiable with no secrets). When
MODEL_EVALS_ATTEST_KEY is configured, an HMAC-SHA256 `signature` is added and
verify enforces it — real authenticity for when the artifact crosses repos.

Only PROMOTE verdicts may be attested. HOLD/REJECT advise; UNDERPOWERED must
never gate a swap (design §8, fail-loud).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
from datetime import date, timedelta

from .catalog import normalise

VERSION = 1
DEFAULT_TTL_DAYS = 90
_SEALED_FIELDS_EXCLUDED = ("seal", "signature")


class AttestationError(ValueError):
    pass


def rubric_hash(text: str) -> str:
    """Stable short hash for a judge rubric / scorer definition (design §5:
    'rubric fixed + versioned (hash in report)')."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _eval_commit() -> str:
    """Provenance default — same source report.render uses for its stamp."""
    from .report import _git_short_rev

    return _git_short_rev()


def _canonical(att: dict) -> bytes:
    body = {k: v for k, v in att.items() if k not in _SEALED_FIELDS_EXCLUDED}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _seal(att: dict) -> str:
    return hashlib.sha256(_canonical(att)).hexdigest()


def _sign(att: dict, key: str) -> str:
    return _hmac.new(key.encode("utf-8"), _canonical(att), hashlib.sha256).hexdigest()


def write_attestation(
    result,
    catalog,
    *,
    config_file: str,
    config_key: str,
    rubric: str | None = None,
    now: date | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    eval_commit: str | None = None,
    key: str | None = None,
) -> dict:
    """Turn a PROMOTE Result into a sealed attestation dict.

    config_file/config_key say where the app commits this model (e.g.
    "config/pipeline.json" / "stages.stage1.model") so the CI gate can check
    the *actual* committed value, not a convention.

    `key` defaults to MODEL_EVALS_ATTEST_KEY; when present the attestation is
    also HMAC-signed.
    """
    if result.verdict != "PROMOTE":
        raise AttestationError(
            f"only PROMOTE may be attested; got {result.verdict} for {result.task!r} "
            f"({result.reason})"
        )
    issued = now or date.today()
    att = {
        "version": VERSION,
        "task": result.task,
        "model": result.candidate_model,
        "incumbent_model": result.incumbent_model,
        "verdict": result.verdict,
        "mode": result.mode,
        "primary_metric": result.primary_metric,
        "value": result.value,
        "ci": [result.ci_low, result.ci_high],
        "n": result.n,
        "min_n": result.min_n,
        "models_json_hash": catalog.hash(),
        "rubric_hash": rubric_hash(rubric) if rubric else "",
        "eval_commit": eval_commit or _eval_commit(),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(days=ttl_days)).isoformat(),
        "config_file": config_file,
        "config_key": config_key,
    }
    att["seal"] = _seal(att)
    key = key if key is not None else os.environ.get("MODEL_EVALS_ATTEST_KEY")
    if key:
        att["signature"] = _sign(att, key)
    return att


def verify_attestation(
    att: dict,
    *,
    committed_model: str,
    catalog_hash: str,
    current_rubric_hash: str | None = None,
    now: date | None = None,
    key: str | None = None,
) -> list[str]:
    """Return the list of invalidation reasons (empty ⇒ valid).

    All rules are checked and *all* failures reported — a stale attestation
    with a mismatched model should say both, not whichever tripped first.
    `current_rubric_hash=None` skips the rubric rule (no judge gated this
    task); pass "" or a hash to enforce it.
    """
    failures: list[str] = []
    today = now or date.today()

    if att.get("version") != VERSION:
        failures.append(f"unsupported attestation version {att.get('version')!r} (expected {VERSION})")

    if att.get("seal") != _seal(att):
        failures.append("seal mismatch: payload was edited after sealing")

    key = key if key is not None else os.environ.get("MODEL_EVALS_ATTEST_KEY")
    if key:
        sig = att.get("signature", "")
        if not isinstance(sig, str) or not sig:
            failures.append("key provided but attestation is unsigned")
        elif not _hmac.compare_digest(sig, _sign(att, key)):
            failures.append("HMAC signature mismatch (wrong key or tampered payload)")

    if att.get("verdict") != "PROMOTE":
        failures.append(f"verdict is {att.get('verdict')!r}: only PROMOTE attests a swap")

    if normalise(committed_model) != normalise(att.get("model", "")):
        failures.append(
            f"committed model {committed_model!r} ≠ attested model {att.get('model')!r}"
        )

    try:
        expires = date.fromisoformat(att.get("expires_at", ""))
        if today > expires:
            failures.append(f"expired {att.get('expires_at')} (today {today.isoformat()})")
    except (TypeError, ValueError):
        # TypeError too: a JSON null/number here must be an invalidation
        # reason, not a crash that aborts the whole CI gate mid-scan.
        failures.append(f"unparseable expires_at {att.get('expires_at')!r}")

    if att.get("models_json_hash") != catalog_hash:
        failures.append(
            f"registry changed: attested against models.json {att.get('models_json_hash')!r}, "
            f"current is {catalog_hash!r} — re-run the eval"
        )

    att_rubric = att.get("rubric_hash", "")
    if current_rubric_hash is not None:
        if att_rubric != current_rubric_hash:
            failures.append(
                f"rubric changed: attested {att_rubric!r}, current {current_rubric_hash!r}"
            )
    elif att_rubric:
        # A judge gated this call: the rule must not be disablable by simply
        # not supplying the current hash (e.g. deleting the rubrics.json entry).
        failures.append(
            "judge-gated attestation (rubric_hash set) but no current rubric hash "
            "supplied — add the task to evals/rubrics.json or re-run the eval"
        )

    n, min_n = att.get("n", 0), att.get("min_n", 0)
    if not isinstance(n, int) or not isinstance(min_n, int):
        failures.append(f"malformed n/min_n: n={n!r}, min_n={min_n!r}")
    elif n < min_n:
        failures.append(f"below statistical floor: n={n} < min_n={min_n} (must never gate)")

    return failures


def save(att: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(att, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _main(argv: list[str]) -> int:
    """CLI: python -m model_evals.attest verify <att.json> --model <committed>
    [--models-json <path>] [--rubric-hash <hash>] [--now YYYY-MM-DD]"""
    import argparse

    from .catalog import Catalog

    p = argparse.ArgumentParser(prog="model_evals.attest")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify one attestation file")
    v.add_argument("attestation")
    v.add_argument("--model", required=True, help="the committed model id to check")
    v.add_argument("--models-json", default=None, help="registry export (default: Catalog search path)")
    v.add_argument("--rubric-hash", default=None, help="current rubric hash (omit to skip the rubric rule)")
    v.add_argument("--now", default=None, help="override today (YYYY-MM-DD), for testing")
    v.add_argument("--key", default=None,
                   help="HMAC key (default: MODEL_EVALS_ATTEST_KEY env)")
    v.add_argument("--no-key", action="store_true",
                   help="ignore MODEL_EVALS_ATTEST_KEY — verify the seal only")
    args = p.parse_args(argv)

    att = load(args.attestation)
    catalog = Catalog.load(args.models_json)
    failures = verify_attestation(
        att,
        committed_model=args.model,
        catalog_hash=catalog.hash(),
        current_rubric_hash=args.rubric_hash,
        now=date.fromisoformat(args.now) if args.now else None,
        key="" if args.no_key else args.key,
    )
    if failures:
        print(f"INVALID: {att.get('task')} → {att.get('model')}")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print(f"VALID: {att.get('task')} → {att.get('model')} "
          f"(expires {att.get('expires_at')}, n={att.get('n')})")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
