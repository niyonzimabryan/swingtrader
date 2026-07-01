"""Catalog — the bridge to Problem A (the registry).

Loads models.json (exported by the registry), normalises dated/`-latest` ids to
their registry key, exposes price → $/run cost math, and refuses to evaluate a
candidate that the registry marks retired/deprecated (a swap onto a rotten id is
never valid, no matter how good the scores look).
"""
from __future__ import annotations

import hashlib
import json
import os
import re

_DATE_SUFFIX = re.compile(r"(?:[-@]\d{8}|[-@]\d{4}-\d{2}-\d{2}|-latest)$", re.IGNORECASE)


def normalise(model_id: str) -> str:
    """`claude-haiku-4-5-20251001` / `...-latest` → `claude-haiku-4-5`."""
    return _DATE_SUFFIX.sub("", (model_id or "").strip().lower())


def _default_paths() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.environ.get("MODEL_REGISTRY_JSON", ""),
        os.path.join(here, "models.json"),            # vendored beside the package
        os.path.join(os.path.dirname(here), "models.json"),  # registry repo root
        os.path.join(here, "..", "models.json"),
    ]


class UnknownModel(KeyError):
    pass


class RottenCandidate(ValueError):
    """Candidate is retired/deprecated per the registry — not eligible for a swap."""


class Catalog:
    def __init__(self, models: dict, source_path: str | None = None, raw: bytes | None = None):
        self.models = models
        self.source_path = source_path
        self._raw = raw if raw is not None else json.dumps(models, sort_keys=True).encode()

    @classmethod
    def load(cls, path: str | None = None) -> "Catalog":
        candidates = [path] if path else _default_paths()
        for p in candidates:
            if p and os.path.exists(p):
                with open(p, "rb") as f:
                    raw = f.read()
                return cls(json.loads(raw), source_path=os.path.abspath(p), raw=raw)
        raise FileNotFoundError(
            "models.json not found. Set MODEL_REGISTRY_JSON or vendor it beside model_evals/. "
            f"Looked in: {[c for c in candidates if c]}"
        )

    def hash(self) -> str:
        """Stable content hash — stamped into reports so staleness is detectable."""
        return hashlib.sha256(self._raw).hexdigest()[:16]

    def entry(self, model_id: str) -> dict:
        key = normalise(model_id)
        if key not in self.models:
            raise UnknownModel(f"{model_id!r} (normalised {key!r}) not in registry")
        return self.models[key]

    def status(self, model_id: str) -> str:
        return self.entry(model_id)["status"]

    def price(self, model_id: str) -> dict:
        e = self.entry(model_id)
        if "price" not in e:
            raise UnknownModel(f"{model_id!r} has no structured price in the registry")
        return e["price"]

    def require_eligible(self, model_id: str) -> None:
        """Raise if the candidate is retired/deprecated (rotten) per the registry."""
        st = self.status(model_id)
        if st in ("retired", "deprecated"):
            e = self.entry(model_id)
            raise RottenCandidate(
                f"{model_id!r} is {st} → {e.get('replace', '?')}. Refusing to evaluate a swap onto it."
            )

    def cost_per_run(self, model_id: str, in_tok: float, out_tok: float) -> float:
        """USD for one invocation given token counts. Prices are per 1M tokens."""
        p = self.price(model_id)
        return (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000.0


def estimate_tokens(obj) -> int:
    """Rough token estimate (~4 chars/token) for when exact usage isn't logged.
    Used only as a cost *proxy*; reports flag estimated costs as approximate."""
    if obj is None:
        return 0
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, len(text) // 4)
