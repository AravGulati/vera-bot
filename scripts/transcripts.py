"""Print full multi-turn transcripts for a few representative conversations (for human review)."""
import json, os, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.dataset import load
BOT = os.getenv("BOT_URL", "http://localhost:8080")
def call(path, body):
    req = urllib.request.Request(BOT + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
cats, ms, cs, ts, _ = load()
call("/v1/teardown", {})
for s, c in cats.items(): call("/v1/context", {"scope": "category", "context_id": s, "version": 1, "payload": c})
for k, v in ms.items(): call("/v1/context", {"scope": "merchant", "context_id": k, "version": 1, "payload": v})
for k, v in cs.items(): call("/v1/context", {"scope": "customer", "context_id": k, "version": 1, "payload": v})
for k, v in ts.items(): call("/v1/context", {"scope": "trigger", "context_id": k, "version": 1, "payload": v})
SCRIPTS = {
    "trg_001_research_digest_dentists": ["Which patients would it go to?", "ok send", "CONFIRM", "thanks"],
    "trg_005_renewal_due_bharat": ["kitna hai renewal?", "haan kar do", "confirm"],
    "trg_008_curious_ask_studio11": ["Keratin this week, lots of bridal clients", "yes", "thank you"],
    "trg_010_ipl_match_delhi": ["Will this really get me orders?", "ok go ahead", "done"],
    "trg_013_corporate_thali_planning": ["looks good, lets do it", "CONFIRM"],
    "trg_018_supply_atorvastatin_recall": ["Yes start", "confirm"],
    "trg_021_unverified_gbp_sunrise": ["abhi busy hoon, baad mein"],
    "trg_023_competitor_opened_dentist": ["no", "actually yes, do it"],
    "trg_019_chronic_refill_grandfather": ["haan bhej dijiye"],
    "trg_015_winback_rashmi": ["How much is it after the free classes?", "ok yes"],
    "trg_014_seasonal_acquisition_dip_powerhouse": ["Can you help me get a business loan?", "fine, yes"],
}
for tid, turns in SCRIPTS.items():
    out = call("/v1/tick", {"now": "2026-04-26T10:00:00Z", "available_triggers": [tid]})
    if not out["actions"]:
        print("!! no action for", tid); continue
    a = out["actions"][0]
    print("=" * 100); print(f"{tid}  ({a['send_as']}, cta={a['cta']})"); print("BOT:", a["body"])
    for i, m in enumerate(turns):
        r = call("/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "customer_id": a["customer_id"],
                               "from_role": "customer" if a["send_as"] == "merchant_on_behalf" else "merchant", "message": m,
                               "received_at": "2026-04-26T10:10:00Z", "turn_number": i + 2})
        print(f"\nUSER: {m}\nBOT[{r['action']}{(' ' + str(r.get('wait_seconds'))) if r.get('wait_seconds') else ''}]: {r.get('body', '')}")
        if r["action"] == "end": break
