"""HTTP surface for the magicpin judge harness.

Endpoints: POST /v1/context, POST /v1/tick, POST /v1/reply, GET /v1/healthz, GET /v1/metadata
(+ POST /v1/teardown, GET / for humans).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .composer import COMPOSER_VERSION, Ctx, compose
from .replies import default_action, respond
from .store import SCOPES, STORE
from .util import parse_dt

app = FastAPI(title="Vera merchant assistant", version="2.0.0")

MAX_ACTIONS_PER_TICK = 20
MERCHANT_GAP_MIN = int(os.getenv("MERCHANT_GAP_MINUTES", "30"))  # min sim-minutes between proactive sends to one merchant
URGENT_BYPASS = int(os.getenv("URGENT_BYPASS_LEVEL", "5"))       # urgency >= this ignores the gap


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bad(reason: str, details: str = "") -> JSONResponse:
    return JSONResponse(status_code=400, content={"accepted": False, "reason": reason, "details": details})


# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"service": "vera-bot", "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"]}


@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - STORE.started), "contexts_loaded": STORE.counts()}


@app.get("/v1/metadata")
async def metadata():
    members = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Arav Gulati").split(",") if m.strip()]
    return {
        "team_name": os.getenv("TEAM_NAME", "Nightcrawler"),
        "team_members": members,
        "model": os.getenv("BOT_MODEL", "deterministic-composer (no LLM at runtime)"),
        "approach": ("Deterministic 4-context composer: per-trigger-kind handlers anchored on verifiable context facts, "
                     "category voice + taboo guardrails, language-aware (Hinglish/English per merchant & per turn), "
                     "restraint-aware tick scheduler, and a multi-turn state machine (auto-reply detection, "
                     "intent→action switch, opt-out/hostile exits, off-topic redirect)."),
        "contact_email": os.getenv("CONTACT_EMAIL", "aravgulati200515@gmail.com"),
        "version": f"2.0.0 ({COMPOSER_VERSION})",
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-27T00:00:00Z"),
    }


@app.post("/v1/teardown")
async def teardown():
    STORE.reset()
    return {"ok": True, "wiped_at": _now_iso()}


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _bad("malformed_json")
    if not isinstance(body, dict):
        return _bad("malformed_body")
    scope = body.get("scope")
    cid = body.get("context_id")
    version = body.get("version")
    payload = body.get("payload")
    if scope not in SCOPES:
        return _bad("invalid_scope", f"scope must be one of {list(SCOPES)}")
    if not isinstance(cid, str) or not cid.strip():
        return _bad("invalid_context_id")
    try:
        version = int(version)
    except (TypeError, ValueError):
        return _bad("invalid_version")
    if not isinstance(payload, dict):
        return _bad("invalid_payload", "payload must be an object")
    ok, current = STORE.put_context(scope, cid, version, payload)
    if not ok:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current})
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _now_iso()}


# ---------------------------------------------------------------------------
def _extras_for(trigger: dict) -> dict:
    """Cross-trigger facts (e.g. an active supply recall for a refill)."""
    extras: dict[str, Any] = {}
    for t in STORE.triggers_for_merchant(trigger.get("merchant_id")):
        if t.get("kind") == "supply_alert":
            p = t.get("payload") or {}
            extras["recall_alert"] = {"molecule": p.get("molecule", ""), "batches": p.get("affected_batches") or []}
    return extras


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    now = parse_dt(body.get("now")) or datetime.now(timezone.utc)
    available = [t for t in (body.get("available_triggers") or []) if isinstance(t, str)]

    with STORE.lock:
        # The judge's available_triggers is the source of truth for what is active right now;
        # anything we held back earlier is only re-sent if the judge lists it again.
        candidate_ids = list(dict.fromkeys(available))
        candidates = []
        for tid in candidate_ids:
            trg = STORE.get("trigger", tid)
            if not trg:
                continue
            mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
            merchant = STORE.get("merchant", mid)
            category = STORE.category_for(merchant)
            if not (merchant and category):
                continue
            cust = None
            if trg.get("scope") == "customer":
                cust = STORE.get("customer", trg.get("customer_id"))
                if not cust:
                    STORE.deferred.setdefault(tid, time.time())  # customer context may arrive later
                    continue
            skey = trg.get("suppression_key") or f"{trg.get('kind')}:{mid}:{trg.get('customer_id')}"
            if skey in STORE.sent_suppression:
                STORE.deferred.pop(tid, None)
                continue
            if STORE.mstate(mid).get("opted_out") and trg.get("scope") != "customer":
                STORE.deferred.pop(tid, None)
                continue
            if cust:
                cs = STORE.cstate(cust.get("customer_id"))
                prefs = cust.get("preferences") or {}
                consent = (cust.get("consent") or {}).get("scope") or []
                if cs.get("opted_out") or prefs.get("reminder_opt_in") is False or not consent:
                    STORE.deferred.pop(tid, None)
                    continue
            candidates.append((trg, merchant, category, cust, skey, tid, mid))

        candidates.sort(key=lambda c: (-(c[0].get("urgency") or 0), c[5]))
        actions = []
        vera_sent_to: set[str] = set()
        cust_sent_to: set[str] = set()
        for trg, merchant, category, cust, skey, tid, mid in candidates:
            if len(actions) >= MAX_ACTIONS_PER_TICK:
                STORE.deferred.setdefault(tid, time.time())
                continue
            urgency = trg.get("urgency") or 0
            if cust is None:
                ms = STORE.mstate(mid)
                cooldown_until = ms.get("cooldown_until")
                if cooldown_until and now < cooldown_until and urgency < 5:
                    STORE.deferred.setdefault(tid, time.time())
                    continue
                last = STORE.last_proactive.get(mid)
                recent = last is not None and (now - last) < timedelta(minutes=MERCHANT_GAP_MIN)
                if mid in vera_sent_to or (recent and urgency < URGENT_BYPASS):
                    STORE.deferred.setdefault(tid, time.time())
                    continue
            else:
                if cust.get("customer_id") in cust_sent_to:
                    STORE.deferred.setdefault(tid, time.time())
                    continue
            try:
                out = compose(category, merchant, trg, cust, now=now, extras=_extras_for(trg))
            except Exception:
                continue
            if not out.get("body"):
                continue
            conv_id = f"conv_{mid}_{tid}"
            n = 2
            while conv_id in STORE.conversations:
                conv_id = f"conv_{mid}_{tid}_{n}"
                n += 1
            STORE.conversations[conv_id] = {
                "id": conv_id, "merchant_id": mid, "customer_id": (cust or {}).get("customer_id"),
                "trigger_id": tid, "kind": trg.get("kind"), "send_as": out["send_as"],
                "action": out["_action"], "action_data": out["_action_data"], "stage": 0, "status": "open",
                "mode": out["_mode"], "bot_bodies": [out["body"]], "merchant_msgs": [], "started_at": now.isoformat(),
            }
            STORE.sent_suppression[skey] = conv_id
            STORE.deferred.pop(tid, None)
            if cust is None:
                vera_sent_to.add(mid)
                STORE.last_proactive[mid] = now
            else:
                cust_sent_to.add(cust.get("customer_id"))
            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": (cust or {}).get("customer_id"),
                "send_as": out["send_as"],
                "trigger_id": tid,
                "template_name": out["template_name"],
                "template_params": out["template_params"],
                "body": out["body"],
                "cta": out["cta"],
                "suppression_key": skey,
                "rationale": out["rationale"],
            })
    return {"actions": actions}


# ---------------------------------------------------------------------------
@app.post("/v1/reply")
async def reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "malformed_json"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "malformed_body"})
    conv_id = str(body.get("conversation_id") or "conv_unknown")
    message = str(body.get("message") or "")
    from_role = str(body.get("from_role") or "merchant")
    now = parse_dt(body.get("received_at")) or datetime.now(timezone.utc)

    with STORE.lock:
        conv = STORE.conversations.get(conv_id)
        mid = body.get("merchant_id") or (conv or {}).get("merchant_id")
        cid = body.get("customer_id") or (conv or {}).get("customer_id")
        merchant = STORE.get("merchant", mid) or {"merchant_id": mid, "identity": {}}
        category = STORE.category_for(merchant) or {}
        customer = STORE.get("customer", cid) if cid else None
        trigger = STORE.get("trigger", (conv or {}).get("trigger_id")) or {"kind": "reply", "payload": {}}
        ctx = Ctx(category, merchant, trigger, customer, now=now)
        if conv is None:
            action, data = default_action(ctx)
            conv = {"id": conv_id, "merchant_id": mid, "customer_id": cid, "trigger_id": None, "kind": "inbound",
                    "send_as": "merchant_on_behalf" if from_role == "customer" else "vera",
                    "action": action if from_role != "customer" else "book_slot",
                    "action_data": data if from_role != "customer" else {"slots": []},
                    "stage": 0, "status": "open", "mode": ctx.mode, "bot_bodies": [], "merchant_msgs": []}
            STORE.conversations[conv_id] = conv
        state = STORE.cstate(cid) if from_role == "customer" else STORE.mstate(mid)
        try:
            result = respond(conv, ctx, message, state, from_role=from_role)
        except Exception:
            result = {"action": "send", "body": "Got it — I'll take it from here and update you shortly.",
                      "cta": "none", "rationale": "Fallback acknowledgement after an internal error."}
        if result.get("action") == "wait":
            state["cooldown_until"] = now + timedelta(seconds=int(result.get("wait_seconds") or 0))
        if result.get("action") == "send" and not (result.get("body") or "").strip():
            result = {"action": "wait", "wait_seconds": 3600, "rationale": "Nothing useful to add yet."}
    return result
