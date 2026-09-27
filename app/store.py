"""In-memory, thread-safe state for the bot (contexts, conversations, suppression, per-merchant state)."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started = time.time()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict] = {}
            self.conversations: dict[str, dict] = {}
            self.merchant_state: dict[str, dict] = {}
            self.customer_state: dict[str, dict] = {}
            self.sent_suppression: dict[str, str] = {}   # suppression_key -> conversation_id
            self.deferred: dict[str, float] = {}         # trigger_id -> first seen (epoch)
            self.last_proactive: dict[str, datetime] = {}  # merchant_id -> sim time of last proactive send

    # -- contexts -----------------------------------------------------------
    def put_context(self, scope: str, cid: str, version: int, payload: dict) -> tuple[bool, Optional[int]]:
        with self.lock:
            key = (scope, cid)
            cur = self.contexts.get(key)
            if cur is not None and cur["version"] >= version:
                return False, cur["version"]
            self.contexts[key] = {"version": version, "payload": payload,
                                  "stored_at": datetime.now(timezone.utc).isoformat()}
            return True, None

    def get(self, scope: str, cid: Optional[str]) -> Optional[dict]:
        if not cid:
            return None
        with self.lock:
            rec = self.contexts.get((scope, cid))
            return rec["payload"] if rec else None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SCOPES}
        with self.lock:
            for (scope, _), _v in self.contexts.items():
                out[scope] = out.get(scope, 0) + 1
        return out

    def triggers_for_merchant(self, merchant_id: str) -> list[dict]:
        with self.lock:
            return [rec["payload"] for (scope, _), rec in self.contexts.items()
                    if scope == "trigger" and (rec["payload"] or {}).get("merchant_id") == merchant_id]

    def category_for(self, merchant: Optional[dict]) -> Optional[dict]:
        if not merchant:
            return None
        slug = merchant.get("category_slug")
        return self.get("category", slug) if slug else None

    # -- per-recipient state --------------------------------------------------
    def mstate(self, merchant_id: Optional[str]) -> dict:
        with self.lock:
            return self.merchant_state.setdefault(merchant_id or "_unknown", {})

    def cstate(self, customer_id: Optional[str]) -> dict:
        with self.lock:
            return self.customer_state.setdefault(customer_id or "_unknown", {})


STORE = Store()
