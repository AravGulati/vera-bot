"""Offline replay of the magicpin judge lifecycle against a running bot (no LLM needed).

Usage:  BOT_URL=http://localhost:8080 python scripts/local_judge.py [--quiet]

Phases:
  1. warmup     — healthz, metadata, push 5 categories + 50 merchants + 200 customers; verify counts; idempotency (409)
  2. test window — push 100 triggers, 12 simulated 5-min ticks, reply to every action with a scripted persona
  3. injection  — version-bump a category digest + merchant perf mid-test; confirm acceptance + use
  4. replays    — auto-reply hell, intent transition, hostile→off-topic, customer slot booking, language switch
Checks every response against the contract and prints PASS/FAIL.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.dataset import load  # noqa: E402
from app.validate import has_url, has_qualifier  # noqa: E402

BOT = os.getenv("BOT_URL", "http://localhost:8080").rstrip("/")
QUIET = "--quiet" in sys.argv
FAILS: list[str] = []
PASSES = 0
LAT: list[float] = []


def call(method: str, path: str, body=None, expect=(200,)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BOT + path, data=data, method=method, headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code, out = r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        code, out = e.code, json.loads(e.read().decode() or "{}")
    LAT.append((time.time() - t) * 1000)
    if code not in expect:
        fail(f"{method} {path} -> HTTP {code}: {out}")
    return code, out


def ok(cond: bool, label: str):
    global PASSES
    if cond:
        PASSES += 1
    else:
        fail(label)


def fail(label: str):
    FAILS.append(label)
    print("  FAIL:", label)


def show(*a):
    if not QUIET:
        print(*a)


ACTION_KEYS = ["conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
               "template_params", "body", "cta", "suppression_key", "rationale"]


def check_action(a: dict):
    for k in ACTION_KEYS:
        ok(k in a, f"action missing {k}")
    ok(bool(a.get("body", "").strip()), "empty body")
    ok(not has_url(a.get("body", "")), f"URL in body {a.get('trigger_id')}")
    ok(a.get("send_as") in ("vera", "merchant_on_behalf"), "bad send_as")
    ok("_" not in "".join(w for w in a["body"].split() if w.islower() and "_" in w), f"snake_case jargon in {a['trigger_id']}")


def check_reply(r: dict):
    ok(r.get("action") in ("send", "wait", "end"), f"bad reply action {r}")
    if r.get("action") == "send":
        ok(bool(r.get("body", "").strip()), "reply send with empty body")
        ok(not has_url(r.get("body", "")), "URL in reply")
    if r.get("action") == "wait":
        ok(isinstance(r.get("wait_seconds"), int), "wait without wait_seconds")
    ok(bool(r.get("rationale")), "reply without rationale")


PERSONAS = [
    "Yes please, go ahead",
    "Thank you for contacting us! Our team will respond shortly.",
    "Haan theek hai, kar do",
    "How much will this cost?",
    "Not interested. Stop messaging me.",
    "Busy right now, later",
    "Can you also help me file my GST this month?",
    "ok",
]


def main():
    cats, ms, cs, ts, pairs = load()
    base = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)
    print(f"== Phase 1: warmup against {BOT}")
    call("POST", "/v1/teardown", {})
    _, h = call("GET", "/v1/healthz")
    ok(h.get("status") == "ok", "healthz not ok")
    _, md = call("GET", "/v1/metadata")
    for k in ["team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"]:
        ok(k in md, f"metadata missing {k}")
    for slug, c in cats.items():
        call("POST", "/v1/context", {"scope": "category", "context_id": slug, "version": 1, "payload": c, "delivered_at": base.isoformat()})
    for mid, m in ms.items():
        call("POST", "/v1/context", {"scope": "merchant", "context_id": mid, "version": 1, "payload": m, "delivered_at": base.isoformat()})
    for cid, c in cs.items():
        call("POST", "/v1/context", {"scope": "customer", "context_id": cid, "version": 1, "payload": c, "delivered_at": base.isoformat()})
    _, h = call("GET", "/v1/healthz")
    ok(h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}, f"counts wrong {h['contexts_loaded']}")
    print("   contexts_loaded:", h["contexts_loaded"])
    code, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 1,
                                            "payload": ms["m_001_drmeera_dentist_delhi"], "delivered_at": base.isoformat()}, expect=(409,))
    ok(code == 409 and r.get("reason") == "stale_version", "idempotency 409 missing")
    code, r = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}}, expect=(400,))
    ok(code == 400, "invalid scope should be 400")

    print("== Phase 2: test window (100 triggers, 12 ticks x 5 min)")
    for tid, t in ts.items():
        call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1, "payload": t, "delivered_at": base.isoformat()})
    tids = sorted(ts)
    all_actions = []
    for i in range(12):
        now = base + timedelta(minutes=5 * i)
        batch = tids[i * 9:(i + 1) * 9] if i < 11 else tids[99:]
        if i == 6:  # Phase 3 injection mid-window
            dent = json.loads(json.dumps(cats["dentists"]))
            dent["digest"].insert(0, {"id": "d_NEW_perio_study", "kind": "research", "title": "Scaling every 4 months cuts periodontal bleeding 31% vs 12-month",
                                      "source": "IJDR Nov 2026, p.7", "trial_n": 860, "patient_segment": "high_risk_adults",
                                      "summary": "Randomised trial shows 31% lower bleeding-on-probing with 4-month scaling in adults with gingivitis history.",
                                      "actionable": "Offer a 4-month scaling recall to gingivitis-history patients"})
            code, _ = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 2, "payload": dent, "delivered_at": now.isoformat()})
            ok(code == 200, "category v2 not accepted")
            m1 = json.loads(json.dumps(ms["m_001_drmeera_dentist_delhi"]))
            m1["performance"]["views"] = 2580
            call("POST", "/v1/context", {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 2, "payload": m1, "delivered_at": now.isoformat()})
            newt = {"id": "trg_NEW_research_perio", "scope": "merchant", "kind": "research_digest", "source": "external",
                    "merchant_id": "m_013_dr_neha_dentist_jaipur", "customer_id": None, "payload": {"category": "dentists", "top_item_id": "d_NEW_perio_study"},
                    "urgency": 2, "suppression_key": "research:dentists:NEW", "expires_at": "2026-12-01T00:00:00Z"}
            call("POST", "/v1/context", {"scope": "trigger", "context_id": newt["id"], "version": 1, "payload": newt, "delivered_at": now.isoformat()})
            batch = batch + [newt["id"]]
        _, out = call("POST", "/v1/tick", {"now": now.isoformat().replace("+00:00", "Z"), "available_triggers": batch})
        acts = out.get("actions", [])
        ok(len(acts) <= 20, "more than 20 actions")
        mids = [a["merchant_id"] for a in acts if a["send_as"] == "vera"]
        ok(len(mids) == len(set(mids)), f"tick {i}: >1 merchant-facing message to one merchant")
        for a in acts:
            check_action(a)
            all_actions.append(a)
            if a["trigger_id"] == "trg_NEW_research_perio":
                ok("IJDR" in a["body"] and "31%" in a["body"], "injected digest item not used")
                show("   [injected digest used] ", a["body"][:160])
        show(f"   tick {i:2d} @ {now:%H:%M}: {len(batch):2d} offered -> {len(acts):2d} sent")
    ok(len({a["conversation_id"] for a in all_actions}) == len(all_actions), "duplicate conversation_id")
    ok(len({a["suppression_key"] for a in all_actions}) == len(all_actions), "suppression key re-sent")
    print(f"   total proactive sends: {len(all_actions)} (of 101 triggers; rest deferred/suppressed by restraint rules)")

    # replies with personas
    turns = 0
    for idx, a in enumerate(all_actions[:40]):
        persona = PERSONAS[idx % len(PERSONAS)]
        _, r = call("POST", "/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "customer_id": a["customer_id"],
                                          "from_role": "customer" if a["send_as"] == "merchant_on_behalf" else "merchant",
                                          "message": persona, "received_at": base.isoformat(), "turn_number": 2})
        check_reply(r)
        turns += 1
    print(f"   persona replies checked: {turns}")

    print("== Phase 4: replay scenarios")
    # 4.1 auto-reply hell (same conversation)
    tick_conv = next(a for a in all_actions if a["merchant_id"] == "m_001_drmeera_dentist_delhi")
    conv = "conv_replay_auto"
    auto = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    seq = []
    for turn in range(2, 6):
        _, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_002_bharat_dentist_mumbai", "from_role": "merchant",
                                          "message": auto, "received_at": (base + timedelta(hours=5, minutes=turn)).isoformat(), "turn_number": turn})
        check_reply(r)
        seq.append(r["action"])
        show(f"   auto-reply turn {turn}: {r['action']}  {r.get('body', '')[:90]}")
        if r["action"] == "end":
            break
    ok(seq[:3] == ["send", "wait", "end"], f"auto-reply ladder wrong: {seq}")

    # 4.1b auto-reply across different conversation ids (how judge_simulator.py does it)
    seq = []
    for i in range(1, 5):
        _, r = call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}", "merchant_id": "m_009_apollo_pharmacy_jaipur", "from_role": "merchant",
                                          "message": "Thank you for contacting us! Our team will respond shortly.", "received_at": base.isoformat(), "turn_number": i + 1})
        seq.append(r["action"])
        if r["action"] == "end":
            break
    ok("end" in seq, f"cross-conversation auto-reply never ended: {seq}")
    show("   auto-reply across conv ids:", seq)

    # 4.2 intent transition
    conv = "conv_replay_intent"
    for turn, msg in enumerate(["Hmm what exactly is this about?", "Which patients would it go to?", "Ok lets do it. Whats next?"], start=2):
        _, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                          "message": msg, "received_at": base.isoformat(), "turn_number": turn})
        check_reply(r)
        show(f"   intent turn {turn} [{msg}] -> {r['action']}: {r.get('body', '')[:220]}")
    ok(r["action"] == "send" and not has_qualifier(r["body"]), "intent transition: still qualifying")
    ok(any(w in r["body"].lower() for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"]), "intent transition: no action words")
    _, r2 = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                       "message": "CONFIRM", "received_at": base.isoformat(), "turn_number": 5})
    show(f"   intent turn 5 [CONFIRM] -> {r2['action']}: {r2.get('body', '')[:160]}")
    ok(r2["action"] == "send" and r2["body"] != r["body"], "confirm step failed")

    # 4.3 hostile then off-topic
    conv = "conv_replay_hostile"
    _, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_005_pizzajunction_restaurant_delhi", "from_role": "merchant",
                                      "message": "You people are useless, always wasting my time.", "received_at": base.isoformat(), "turn_number": 2})
    show(f"   hostile -> {r['action']}: {r.get('body', '')[:160]}")
    ok(r["action"] in ("send", "end"), "hostile not handled")
    _, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_005_pizzajunction_restaurant_delhi", "from_role": "merchant",
                                      "message": "Btw can you help me file my GST?", "received_at": base.isoformat(), "turn_number": 3})
    show(f"   off-topic -> {r['action']}: {r.get('body', '')[:180]}")
    ok(r["action"] == "send" and "CA" in r["body"], "off-topic not redirected")
    _, r = call("POST", "/v1/reply", {"conversation_id": "conv_hostile_stop", "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow", "from_role": "merchant",
                                      "message": "Stop messaging me. This is useless spam.", "received_at": base.isoformat(), "turn_number": 2})
    ok(r["action"] == "end", "explicit stop must end")

    # 4.4 customer slot booking
    prya = next((a for a in all_actions if a["trigger_id"] == "trg_003_recall_due_priya"), None)
    if prya:
        _, r = call("POST", "/v1/reply", {"conversation_id": prya["conversation_id"], "merchant_id": prya["merchant_id"], "customer_id": prya["customer_id"],
                                          "from_role": "customer", "message": "2", "received_at": base.isoformat(), "turn_number": 2})
        show(f"   customer picks '2' -> {r.get('body', '')}")
        ok("Thu 6 Nov" in r.get("body", ""), "slot 2 not booked")

    # 4.5 language switch mid-conversation
    conv = "conv_lang"
    _, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_007_powerhouse_gym_bangalore", "from_role": "merchant",
                                      "message": "Bhai yeh kitna time lagega, mujhe samajh nahi aaya?", "received_at": base.isoformat(), "turn_number": 2})
    show(f"   hindi turn -> {r.get('body', '')[:160]}")
    ok(any(w in r.get("body", "") for w in ["hai", "hoon", "karein", "kijiye"]), "did not switch to Hinglish")

    # anti-repetition
    conv = "conv_repeat"
    bodies = []
    for turn in range(2, 5):
        _, r = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_003_studio11_salon_hyderabad", "from_role": "merchant",
                                          "message": "hmm", "received_at": base.isoformat(), "turn_number": turn})
        if r.get("body"):
            bodies.append(r["body"])
    ok(len(bodies) == len(set(bodies)), "repeated body in same conversation")

    _, h = call("GET", "/v1/healthz")
    print(f"\nlatency: max {max(LAT):.0f} ms, avg {sum(LAT) / len(LAT):.1f} ms over {len(LAT)} calls")
    print(f"RESULT: {PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
