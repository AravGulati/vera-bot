"""Contract + behaviour tests. Run: pytest -q"""
import json
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.server import app  # noqa: E402
from app.store import STORE  # noqa: E402
from app.validate import has_qualifier, has_url  # noqa: E402
from bot import compose  # noqa: E402
from scripts.dataset import load  # noqa: E402

CATS, MS, CS, TS, PAIRS = load()
client = TestClient(app)


def push(scope, cid, payload, version=1):
    return client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


@pytest.fixture(autouse=True)
def fresh():
    STORE.reset()
    for s, c in CATS.items():
        push("category", s, c)
    for k, v in MS.items():
        push("merchant", k, v)
    for k, v in CS.items():
        push("customer", k, v)
    for k, v in TS.items():
        push("trigger", k, v)
    yield


def reply(conv, mid, msg, role="merchant", cid=None, turn=2):
    return client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid, "from_role": role,
                                          "message": msg, "received_at": "2026-04-26T10:30:00Z", "turn_number": turn}).json()


# -- contract ---------------------------------------------------------------
def test_healthz_counts():
    h = client.get("/v1/healthz").json()
    assert h["status"] == "ok"
    assert h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 100}


def test_metadata_fields():
    m = client.get("/v1/metadata").json()
    for k in ["team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"]:
        assert k in m


def test_context_idempotency_and_versioning():
    r = push("merchant", "m_001_drmeera_dentist_delhi", MS["m_001_drmeera_dentist_delhi"], version=1)
    assert r.status_code == 409 and r.json()["reason"] == "stale_version" and r.json()["current_version"] == 1
    r = push("merchant", "m_001_drmeera_dentist_delhi", MS["m_001_drmeera_dentist_delhi"], version=2)
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert client.post("/v1/context", json={"scope": "nope", "context_id": "x", "version": 1, "payload": {}}).status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "x", "version": 1, "payload": []}).status_code == 400


def test_tick_empty_and_contract():
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": []}).json() == {"actions": []}
    out = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": list(TS)[:20]}).json()
    assert out["actions"]
    for a in out["actions"]:
        for k in ["conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
                  "template_params", "body", "cta", "suppression_key", "rationale"]:
            assert k in a
        assert a["body"].strip() and not has_url(a["body"])
    mids = [a["merchant_id"] for a in out["actions"] if a["send_as"] == "vera"]
    assert len(mids) == len(set(mids)), "at most one merchant-facing send per merchant per tick"


def test_tick_suppression_no_resend():
    t = ["trg_001_research_digest_dentists"]
    a1 = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": t}).json()["actions"]
    a2 = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": t}).json()["actions"]
    assert len(a1) == 1 and a2 == []


def test_customer_without_consent_is_skipped():
    cust = dict(CS["c_001_priya_for_m001"])
    cust["preferences"] = dict(cust["preferences"], reminder_opt_in=False)
    push("customer", "c_001_priya_for_m001", cust, version=2)
    out = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_003_recall_due_priya"]}).json()
    assert out["actions"] == []


# -- composition quality ------------------------------------------------------
@pytest.mark.parametrize("tid", sorted(TS))
def test_compose_every_trigger(tid):
    t = TS[tid]
    m = MS[t["merchant_id"]]
    c = CS.get(t.get("customer_id")) if t.get("customer_id") else None
    cat = CATS[m["category_slug"]]
    r1 = compose(cat, m, t, c)
    r2 = compose(cat, m, t, c)
    assert r1 == r2, "must be deterministic"
    body = r1["body"]
    assert body and len(body) < 1200
    assert not has_url(body)
    assert "{" not in body and "None" not in body
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", body), f"internal jargon leaked: {body}"
    for taboo in cat["voice"].get("vocab_taboo", []):
        assert re.sub(r"\(.*?\)", "", taboo).strip().lower() not in body.lower()
    assert r1["send_as"] == ("merchant_on_behalf" if t["scope"] == "customer" else "vera")
    assert r1["suppression_key"] == t["suppression_key"]
    assert r1["rationale"]


def test_dentist_salutation_and_specifics():
    r = compose(CATS["dentists"], MS["m_001_drmeera_dentist_delhi"], TS["trg_001_research_digest_dentists"])
    assert r["body"].startswith("Dr. Meera")
    assert "2,100" in r["body"] and "38%" in r["body"] and "JIDA" in r["body"] and "124" in r["body"]


def test_no_fabricated_slots_or_prices_for_customers():
    t = TS["trg_076_appointment_tomorrow_m_019_karim_salon_lu"]
    m = MS[t["merchant_id"]]
    r = compose(CATS["salons"], m, t, CS[t["customer_id"]])
    assert "₹" not in r["body"], "merchant has no live offer -> no price in customer message"


def test_submission_file_has_30_rows():
    rows = [json.loads(l) for l in open(os.path.join(ROOT, "submission.jsonl"), encoding="utf-8")]
    assert len(rows) == 30 and {r["test_id"] for r in rows} == {f"T{i:02d}" for i in range(1, 31)}


# -- multi-turn --------------------------------------------------------------
def test_auto_reply_ladder_same_conversation():
    msg = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    acts = [reply("c1", "m_002_bharat_dentist_mumbai", msg, turn=i)["action"] for i in range(2, 5)]
    assert acts == ["send", "wait", "end"]


def test_auto_reply_across_conversations():
    acts = [reply(f"conv_auto_{i}", "m_009_apollo_pharmacy_jaipur", "Thank you for contacting us! Our team will respond shortly.")["action"]
            for i in range(1, 5)]
    assert "end" in acts


def test_verbatim_repeat_counts_as_auto_reply():
    msg = "We are open 10am to 9pm all days, visit us anytime"
    a = [reply("c_rep", "m_004_glamour_salon_pune", msg, turn=i)["action"] for i in range(2, 5)]
    assert a[-1] in ("wait", "end")


def test_intent_transition_goes_to_action():
    r = reply("c_int", "m_001_drmeera_dentist_delhi", "Ok lets do it. Whats next?")
    assert r["action"] == "send"
    assert not has_qualifier(r["body"])
    assert any(w in r["body"].lower() for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])


def test_stop_ends_and_suppresses():
    r = reply("c_stop", "m_010_sunrisepharm_pharmacy_lucknow", "Stop messaging me. This is useless spam.")
    assert r["action"] == "end"
    out = client.post("/v1/tick", json={"now": "2026-04-26T12:00:00Z", "available_triggers": ["trg_021_unverified_gbp_sunrise"]}).json()
    assert out["actions"] == []


def test_offtopic_redirect():
    r = reply("c_gst", "m_001_drmeera_dentist_delhi", "Btw can you also help me with my GST filing this month?")
    assert r["action"] == "send" and "CA" in r["body"]


def test_later_waits():
    r = reply("c_later", "m_003_studio11_salon_hyderabad", "busy right now, call me tomorrow")
    assert r["action"] == "wait" and r["wait_seconds"] >= 3600


def test_customer_slot_booking():
    out = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_003_recall_due_priya"]}).json()
    a = out["actions"][0]
    assert a["send_as"] == "merchant_on_behalf" and a["cta"] == "multi_choice_slot"
    r = reply(a["conversation_id"], a["merchant_id"], "2", role="customer", cid=a["customer_id"])
    assert "Thu 6 Nov" in r["body"]


def test_full_accept_confirm_close():
    out = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_001_research_digest_dentists"]}).json()
    conv = out["actions"][0]["conversation_id"]
    r1 = reply(conv, "m_001_drmeera_dentist_delhi", "Yes please send the abstract")
    assert r1["action"] == "send" and "Abstract" in r1["body"]
    r2 = reply(conv, "m_001_drmeera_dentist_delhi", "CONFIRM", turn=3)
    assert r2["action"] == "send" and r2["body"] != r1["body"]
    r3 = reply(conv, "m_001_drmeera_dentist_delhi", "thanks", turn=4)
    assert r3["action"] == "end"


def test_no_repeated_bodies():
    bodies = [reply("c_hmm", "m_003_studio11_salon_hyderabad", "hmm", turn=i).get("body") for i in range(2, 6)]
    bodies = [b for b in bodies if b]
    assert len(bodies) == len(set(bodies))


def test_hindi_turn_gets_hinglish():
    r = reply("c_hi", "m_007_powerhouse_gym_bangalore", "Bhai yeh kitna time lagega, mujhe samajh nahi aaya?")
    assert r["action"] == "send" and re.search(r"\b(hai|karein|kijiye|hoon|dijiye)\b", r["body"])
