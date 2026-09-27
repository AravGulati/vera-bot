"""Submission entry point.

* ``compose(category, merchant, trigger, customer)`` — the pure function from challenge-brief §7.1.
* ``app`` — the FastAPI server the judge harness calls (``uvicorn bot:app --host 0.0.0.0 --port 8080``).
"""
from __future__ import annotations

from typing import Optional

from app.composer import compose as _compose
from app.server import app  # noqa: F401  (re-exported for uvicorn)


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None, now=None) -> dict:
    """Deterministic: same inputs -> same output. Returns body, cta, send_as, suppression_key, rationale
    (+ template_name / template_params for the first-touch WhatsApp template)."""
    out = _compose(category, merchant, trigger, customer, now=now)
    return {k: v for k, v in out.items() if not k.startswith("_")}
