import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.dataset import load
from app.composer import compose
cats, ms, cs, ts, pairs = load()
sel = sys.argv[1:] 
for tid, t in sorted(ts.items()):
    if sel and not any(s in tid for s in sel): continue
    m = ms[t["merchant_id"]]; c = cs.get(t.get("customer_id")) if t.get("customer_id") else None
    out = compose(cats[m["category_slug"]], m, t, c)
    print(f"### {tid} [{t['kind']}] {out['send_as']} cta={out['cta']} mode={out['_mode']}")
    print(out["body"]); print("  >", out["rationale"][:160]); print()
