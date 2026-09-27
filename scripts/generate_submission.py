"""Generate submission.jsonl for the 30 canonical test pairs (dataset/expanded/test_pairs.json)."""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts.dataset import load
from bot import compose

cats, ms, cs, ts, pairs = load()
out_path = os.path.join(ROOT, "submission.jsonl")
with open(out_path, "w", encoding="utf-8") as f:
    for p in pairs:
        t = ts[p["trigger_id"]]
        m = ms[p["merchant_id"]]
        c = cs.get(p.get("customer_id")) if p.get("customer_id") else None
        r = compose(cats[m["category_slug"]], m, t, c)
        row = {"test_id": p["test_id"], "trigger_id": p["trigger_id"], "merchant_id": p["merchant_id"],
               "customer_id": p.get("customer_id"), **r}
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
print(f"wrote {len(pairs)} rows -> {out_path}")
