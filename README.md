# Nightcrawler — magicpin AI Challenge submission (Vera bot)

A merchant-engagement bot that composes WhatsApp messages from the four contexts (category, merchant, trigger, customer) and runs full conversations. The HTTP bot exposes `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz` and `/v1/metadata`. `submission.jsonl` holds the 30 canonical test pairs.

## Approach
**Deterministic composer, no LLM at runtime.** Each trigger `kind` has its own handler (25 kinds, plus a generic fallback for each scope). A handler reads only what the contexts contain. Every message follows the same shape: a **why-now fact**, then a **judgement**, then **one low-friction CTA**.
- **Specificity from real fields only.** Messages use trial size, source and page from the digest; the merchant's own deltas and peer averages; live offers; review quotes; and real slots. Customer messages never quote a price the merchant hasn't switched on.
- **Placeholder triggers.** 13 of the 30 test pairs carry an empty payload. For these the bot falls back to the merchant's own numbers and the category pack, without inventing an event. For example, a "perf_dip" merchant whose numbers are rising is told so, and pointed at the real gap.
- **Judgement, not templating.**
  - The IPL trigger on a Saturday gives a delivery-first recommendation (weekend matches cut dine-in covers).
  - A seasonal gym dip is reframed as a retention problem (churn vs peer × members = people lost each month).
  - A pharmacy refill reminder checks the active atorvastatin recall.
  - Planning intents get the finished artifact straight away (a tiered thali package, a kids-camp plan), not more questions.
- **Voice and language.**
  - Category taboos are stripped automatically. Dentists get "Dr." and clinical vocabulary.
  - Merchants whose first regional language is Hindi get natural Hindi-English code-mix; a Chennai studio gets English.
  - Language is detected on every turn, so a merchant who switches to Hindi gets Hinglish back.
- **Guardrails.** Every body passes through `finalize()`, which removes URLs, internal snake_case jargon, taboo phrases and template residue.

**Tick scheduler (restraint).**
- Only triggers the judge lists in `available_triggers` are considered.
- Each suppression key is sent once.
- A merchant gets at most one merchant-facing message per tick, with a 30-minute gap between messages to the same merchant. Only urgency-5 alerts skip the gap.
- The bot stays quiet after an opt-out or a "wait".
- Customer messages require opt-in consent.

**Reply state machine.** The bot classifies each turn, then acts:
- **Auto-reply:** first time, one owner-flag nudge; second, wait 24h; third, end. The count is kept per merchant across conversations.
- **Opt-out:** end, and suppress the merchant.
- **Hostile:** one apology with a STOP path.
- **Commitment:** switches to *action mode* and delivers the draft immediately; a validator blocks qualifying phrasing.
- **Defer:** wait.
- **Off-topic** (GST, loans): polite redirect.
- **Price / timing / "will it work?":** an honest answer from context.
- **Slot pick:** booking confirmed.

The same body is never sent twice in one conversation.

## Trade-offs
- **Determinism over generality.** Output is fully reproducible and takes under 15 ms per call, with no API cost or timeout risk. The cost is that unfamiliar trigger kinds fall back to generic handlers, and the wording is less varied than an LLM's.
- **In-memory state**, run with a single worker (the brief allows this). A restart wipes state until the judge pushes context again.
- **Some drafts contain suggested numbers**, such as the corporate-thali tiers derived from the live ₹149 price. They are labelled as drafts to edit, never as facts.

## What would have helped most
Real merchant slots and appointment times; review counts and ratings on every merchant; and a service catalogue with prices per merchant (not only per category). With those, customer reminders and placeholder triggers could be as specific as the seed ones.

## Run
```bash
pip install -r requirements-dev.txt
uvicorn bot:app --host 0.0.0.0 --port 8080        # the bot
pytest -q                                          # 120 contract + behaviour tests
python scripts/local_judge.py                      # full judge lifecycle replay (no LLM needed)
python scripts/generate_submission.py              # regenerates submission.jsonl
```
Metadata comes from environment variables: `TEAM_NAME`, `TEAM_MEMBERS` (comma-separated), `CONTACT_EMAIL`. See `DEPLOY.md` for getting a public URL.
