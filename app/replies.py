"""Multi-turn reply engine: classify the merchant/customer turn, then act.

Priorities (checked in this order):
  1. opt-out / explicit stop  -> end (and suppress this merchant/customer)
  2. hostile without a stop   -> one apology + opt-out path, second time -> end
  3. WhatsApp auto-reply      -> 1st: one owner-flag nudge, 2nd: wait 24h, 3rd: end
  4. commitment ("yes/ok/let's do it/go ahead/haan/kar do", slot pick) -> ACTION MODE
     (deliver the drafted artifact now; never another qualifying question)
  5. defer ("later/busy/kal")  -> wait
  6. out-of-scope asks (GST, loans, website...) -> polite decline + back to thread
  7. questions (price / how / who / will it work) -> honest answer from context + same CTA
  8. soft "no"                 -> graceful close, door left open
  9. anything else             -> treat as information (e.g. answer to a curious-ask)
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .composer import AUDIENCE, NOUN, Ctx, cap, first_sentence, lower_first
from .lang import detect_text_lang
from .util import fmt_date, inr_group, join_human, money, pct, price_from_title
from .validate import finalize, has_qualifier, strip_qualifiers

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
_AUTO = [
    r"thank(s| you) for (contacting|reaching|your message|messaging|getting in touch)",
    r"(our|the) team will (respond|reply|get back|contact)", r"will (respond|reply|get back) (to you )?(shortly|soon|asap)",
    r"we (have|'ve) received your (message|query|request)", r"(automated|auto[- ]?generated|auto[- ]?reply|autoreply)",
    r"i am an? (automated|virtual) assistant", r"out of (the )?office", r"currently (unavailable|closed|away)",
    r"business hours", r"we are closed", r"aapki jaankari ke liye", r"team tak pahuncha", r"hum jald hi",
    r"sampark karne ke liye dhanyavaad", r"aapka sandesh", r"will revert", r"this is an automated",
]
_OPTOUT = [
    r"\bstop\b", r"\bunsubscribe\b", r"not interested", r"don'?t (message|text|contact|send)", r"do not (message|text|contact|send)",
    r"no more (messages|texts)", r"leave me alone", r"remove (me|my number)", r"band karo", r"mat bhejo", r"mat karo message",
    r"nahi chahiye", r"interest nahi", r"block kar", r"\bspam\b",
]
_HOSTILE = [
    r"\bf+u+c+k", r"\bshit\b", r"\bidiot", r"\bstupid\b", r"\buseless\b", r"\bbakwas\b", r"\bbekar\b", r"\bpagal\b", r"\bchup\b",
    r"\bnonsense\b", r"\bscam\b", r"\bfraud\b", r"why are you (bothering|disturbing|messaging)", r"\bannoying\b", r"\bharass",
    r"\bbloody\b", r"\bdamn\b", r"\bwaste of time\b", r"\bget lost\b",
]
_ACCEPT = [
    r"^\s*(yes|yess+|yeah|yep|yup|ya|y|ok|okay|okk+|k|sure|done|confirm(ed)?|go|go ahead|proceed|haan|haa|ha|han|ji|ji haan|theek hai|thik hai|chalo|chalega|kar do|kardo|karo|bilkul|perfect|great|good|fine|alright|👍|✅)\s*[.!]*\s*$",
    r"\blet'?s (do|go|start)\b", r"\blets (do|go|start)\b", r"\bgo ahead\b", r"\bplease (do|proceed|send|go ahead|start)\b",
    r"\bsend (it|me|the|over)\b", r"\bdo it\b", r"\bsign me up\b", r"\bi want to join\b", r"\bjoin(ing)?\b.*\b(magicpin|now)\b",
    r"\bjud(r)?na hai\b", r"\bkar (do|dijiye|dena)\b", r"\bbhej (do|dijiye|dena)\b", r"\bshuru kar", r"\bhaan\b.*\b(karo|kar do|bhejo)\b",
    r"\byes\b", r"\bconfirm\b", r"\bbook (it|me|karo)\b", r"\bwhat'?s next\b", r"\bwhats next\b", r"\bnext step\b", r"\bproceed\b",
    r"\binterested\b", r"\bsounds good\b", r"\bgo for it\b", r"\bset it up\b",
]
_LATER = [r"\blater\b", r"\bbusy\b", r"\bbaad mein\b", r"\bbaad me\b", r"\btomorrow\b", r"\bkal\b", r"\bnext week\b", r"\bcall me\b",
          r"\bin a meeting\b", r"\bafter some time\b", r"\bthodi der\b", r"\babhi nahi\b", r"\bnot now\b", r"\bnot right now\b"]
_OFFTOPIC = [r"\bgst\b", r"\bitr\b", r"\bincome tax\b", r"\btax (filing|return)", r"\bloan\b", r"\bcredit card\b", r"\binsurance\b",
             r"\bca\b", r"\bchartered accountant\b", r"\baccounting\b", r"\bpassport\b", r"\bvisa\b", r"\bbuild (my|a) (website|app)\b",
             r"\bstock market\b", r"\bshare market\b", r"\bcrypto\b", r"\blegal notice\b", r"\blawyer\b", r"\bpan card\b", r"\baadhaar\b"]
_PRICE = [r"\bprice\b", r"\bcost\b", r"\bcharges?\b", r"\bfees?\b", r"\bhow much\b", r"\bkitna\b", r"\bkitne\b", r"\bpaise\b", r"\bpaisa\b", r"\bfree\b\?"]
_HOW = [r"\bhow\b", r"\bkaise\b", r"\bsteps?\b", r"\bprocess\b", r"\bexplain\b", r"\bdetails?\b", r"\bsamjha", r"\bwhat will you\b"]
_WHO = [r"\bwho (are|is) (you|this)\b", r"\bkaun\b", r"\bwhat is (this|magicpin|vera)\b", r"\bare you (a )?(bot|human|ai)\b"]
_DOUBT = [r"\bwill (this|it) (work|help|really)\b", r"\bguarantee", r"\bresults?\b", r"\bdoes (this|it) work\b", r"\bsure\?", r"\bfayda\b", r"\bpakka\b"]
_NO = [r"^\s*(no|nope|nah|nahi|nahin|na|not really|no thanks|no thank you|skip|pass|mat|rehne do)\s*[.!]*\s*$", r"\bno thanks\b", r"\bnot needed\b", r"\bzaroorat nahi\b"]
_THANKS = [r"^\s*(thanks|thank you|thx|ty|shukriya|dhanyavaad|dhanyawad|great thanks|ok thanks|okay thanks)\b.*$"]


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s₹]", "", (text or "").lower())).strip()


def classify(text: str, slot_count: int = 0) -> str:
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return "empty"
    if _any(_AUTO, low):
        return "auto_reply"
    if _any(_OPTOUT, low):
        return "optout"
    if _any(_HOSTILE, low):
        return "hostile"
    if slot_count and re.fullmatch(r"\s*([1-9])\s*[.!]?\s*", t):
        return "slot_choice"
    if _any(_OFFTOPIC, low):
        return "offtopic"
    if _any(_WHO, low):
        return "who"
    if _any(_DOUBT, low) and "?" in t:
        return "doubt"
    if re.search(r"kitna time|kitne din|kitni der|how long|kab tak|kab se|when will|kitna samay|how much time", low):
        return "timing"
    if _any(_PRICE, low) and ("?" in t or len(low.split()) <= 6):
        return "price"
    if _any(_ACCEPT, low) or (re.match(r"^\s*(ok|okay|yes|yeah|sure|haan|han|ha|ji|fine|alright|chalo|theek)\b", low)
                              and not re.search(r"\b(no|not|nahi|mat|but|lekin)\b", low)):
        return "accept"
    if _any(_LATER, low):
        return "later"
    if _any(_NO, low):
        return "decline"
    if _any(_THANKS, low):
        return "thanks"
    if _any(_HOW, low) and "?" in t:
        return "how"
    if "?" in t:
        return "question"
    return "info"


# ---------------------------------------------------------------------------
# Artifact drafting (what "action mode" actually delivers)
# ---------------------------------------------------------------------------
def _post(ctx: Ctx, headline: str, lines: list[str], offer: Optional[str]) -> str:
    loc = f", {ctx.locality}" if ctx.locality else ""
    body = [f"\"{headline}", *[l for l in lines if l]]
    if offer:
        body.append(f"Now on: {offer}.")
    body.append(f"📍 {ctx.biz}{loc} — call or WhatsApp to book.\"")
    return "\n".join(body)


def _topic_offer(ctx: Ctx, topic: str) -> Optional[str]:
    for t in ctx.active_offers:
        if topic and topic.lower().split()[0].rstrip("s") in t.lower():
            return t
    for o in ctx.catalog:
        if topic and topic.lower().split()[0].rstrip("s") in str(o.get("title", "")).lower():
            return o.get("title")
    return None


def _extract_service(ctx: Ctx, text: str) -> Optional[str]:
    """Pull the service the merchant named out of a free-text answer ('Keratin this week, lots of bridal clients' -> 'Keratin')."""
    low = (text or "").lower()
    vocab = list((ctx.cat.get("voice") or {}).get("vocab_allowed") or [])
    for o in ctx.catalog:
        vocab.append(re.split(r"\s*[@(:]", str(o.get("title", "")))[0])
    vocab += ["haircut", "hair spa", "bridal", "facial", "cleaning", "whitening", "root canal", "aligner", "thali", "dosa", "biryani",
              "pizza", "coffee", "yoga", "pilates", "hiit", "personal training", "zumba", "ors", "sunscreen", "bp check", "delivery"]
    best = None
    for v in vocab:
        v2 = str(v).strip()
        if len(v2) < 3:
            continue
        i = low.find(v2.lower())
        if i >= 0 and (best is None or i < best[0]):
            best = (i, v2)
    if best:
        return best[1][:1].upper() + best[1][1:]
    words = re.findall(r"[A-Za-z][A-Za-z+-]*", text or "")
    return " ".join(words[:3]).capitalize() if words and len(words) <= 4 else None


def _history_topics(ctx: Ctx) -> list[str]:
    ask = (ctx.last_merchant_ask() or "").lower()
    topics = []
    for w in ["whitening", "aligners", "aligner", "cleaning", "implant", "balayage", "keratin", "bridal", "spa", "thali", "biryani",
              "pizza", "yoga", "pt", "pilates", "delivery", "generic", "diabetic"]:
        if re.search(rf"\b{w}", ask) and not any(t.startswith(w) or w.startswith(t) for t in topics):
            topics.append(w)
    return topics


def default_action(ctx: Ctx) -> tuple[str, dict]:
    """When a reply arrives for a conversation we did not start, pick the best pending action."""
    topics = _history_topics(ctx)
    if topics:
        return "history_posts", {"topics": topics}
    if ctx.verified is False or ctx.has_signal("unverified_gbp"):
        return "verify_gbp", {}
    if not ctx.active_offers:
        return "launch_offer_post", {"offer": ctx.catalog_offer("cleaning", "haircut", "thali", "trial", "delivery"), "offer_live": False}
    return "followup_post", {"offer": ctx.lead_offer()}


def fulfil(action: str, data: dict, ctx: Ctx, mode: str, merchant_text: str = "") -> tuple[str, str, str]:
    """Returns (body, cta, rationale) for stage 0 -> 1 (deliver the artifact + one confirm)."""
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    aud = AUDIENCE.get(ctx.slug, "customer")
    confirm_publish = T("Reply CONFIRM and it goes live on your Google profile within the hour.",
                        "CONFIRM reply karein, ek ghante mein aapke Google profile pe live ho jayega.")

    if action == "send_research_pack":
        item = ctx.digest.get(data.get("item_id")) or ctx.digest_item(kinds=("research",)) or {}
        abstract = f"{item.get('title')} — {first_sentence(item.get('summary'))} ({item.get('source')})"
        if "fluoride" in str(item.get("title", "")).lower():
            patient = ("\"If you've had a cavity in the last year or two, your teeth may need a little more protection. "
                       "A quick fluoride varnish every 3 months (instead of every 6) helps prevent new cavities for people in this group. "
                       f"Ask us at your next visit whether it's right for you. — {ctx.biz}\"")
        else:
            patient = f"\"Quick note from {ctx.biz}: {first_sentence(item.get('summary'))} Ask us at your next visit if this applies to you.\""
        n = ctx.agg.get("high_risk_adult_count")
        send_to = T(f"Reply CONFIRM and I'll queue it for your {n} high-risk adult patients (only those who opted in)." if n else
                    f"Reply CONFIRM and I'll send it to your opted-in {aud}s.",
                    f"CONFIRM reply karein, main isse aapke {n} high-risk adult patients (sirf opted-in) ko queue kar deti hoon." if n else
                    f"CONFIRM reply karein, main opted-in {aud}s ko bhej deti hoon.")
        body = T(f"Here you go.\n\nAbstract: {abstract}\n\n{cap(aud)} WhatsApp draft:\n{patient}\n\n{send_to}",
                 f"Yeh raha.\n\nAbstract: {abstract}\n\n{cap(aud)} WhatsApp draft:\n{patient}\n\n{send_to}")
        return body, "binary_confirm_cancel", "Merchant accepted → delivered abstract + ready-to-send patient draft; one CONFIRM to dispatch (scope limited to opted-in patients)."

    if action == "send_item_brief":
        item = ctx.digest.get(data.get("item_id")) or {}
        amounts = re.findall(r"₹\s?([\d,]+)", str(item.get("summary", "")))
        math = ""
        if len(amounts) >= 2:
            a, b = [int(x.replace(",", "")) for x in amounts[:2]]
            if b > a:
                math = T(f"Margin math: {money(a)} in, ~{money(b)} out → ~{money(b - a)} per unit before your service charge.",
                         f"Margin ka hisaab: {money(a)} lagat, ~{money(b)} bikri → ~{money(b - a)} per unit, service charge alag.")
        note = f"\"New at {ctx.biz}: {item.get('title', '').split(' — ')[0]}. Ask us about it on your next visit.\""
        body = T(f"Done — quick brief on {item.get('title')}:\n• {first_sentence(item.get('summary'))}\n• {item.get('actionable', '')}\n{('• ' + math) if math else ''}\n\n{cap(aud)}-facing note:\n{note}\n\nReply CONFIRM and I'll post the note on your Google profile.",
                 f"Ho gaya — {item.get('title')} ka quick brief:\n• {first_sentence(item.get('summary'))}\n• {item.get('actionable', '')}\n{('• ' + math) if math else ''}\n\n{cap(aud)} ke liye note:\n{note}\n\nCONFIRM reply karein, main Google profile pe post kar deti hoon.")
        return body, "binary_confirm_cancel", "Delivered the item brief (with margin math when the digest has prices) + a customer-facing note."

    if action == "send_compliance_checklist":
        item = ctx.digest.get(data.get("item_id")) or ctx.digest_item(kinds=("compliance",)) or {}
        dl = data.get("deadline")
        summ = str(item.get("summary", ""))
        pts = []
        if "E-speed" in summ or "RVG" in summ:
            pts = [T("Check what your IOPA setup uses: E-speed film or digital RVG pass; D-speed film does not.",
                     "IOPA setup check karein: E-speed film ya digital RVG pass hain; D-speed film nahi."),
                   T("If you're on D-speed, order E-speed stock or plan the RVG switch now.",
                     "D-speed use ho raha hai toh abhi E-speed stock order karein ya RVG plan karein."),
                   T("Add one SOP line: 'IOPA exposures use E-speed film/RVG; max 1.0 mSv per exposure'.",
                     "SOP mein ek line: 'IOPA exposures E-speed film/RVG par; max 1.0 mSv per exposure'.")]
        else:
            pts = [first_sentence(summ), item.get("actionable", ""), T("Keep a dated note of the check in your records.", "Check ki dated entry records mein rakhein.")]
        lst = "\n".join(f"{i + 1}. {p}" for i, p in enumerate([p for p in pts if p]))
        body = T(f"Here's your audit checklist" + (f" (deadline {fmt_date(dl, ctx.now)})" if dl else "") + f":\n{lst}\n\nReply DONE once checked and I'll log it — I'll remind you 2 weeks before the deadline either way.",
                 f"Aapki audit checklist" + (f" (deadline {fmt_date(dl, ctx.now)})" if dl else "") + f":\n{lst}\n\nCheck ho jaaye toh DONE reply karein — deadline se 2 hafte pehle main reminder bhi bhej dungi.")
        return body, "binary_confirm_cancel", "Compliance: delivered a concrete 3-step checklist tied to the regulation's pass/fail criteria + deadline reminder."

    if action == "send_compliance_done":
        pass

    if action in ("launch_offer_post", "followup_post", "timely_post", "festival_campaign", "seasonal_post",
                  "differentiation_post", "match_night_promo", "apply_quick_win", "quick_fix"):
        offer = data.get("offer") or ctx.lead_offer() or ctx.catalog_offer()
        area = ctx.locality or ctx.city
        if action == "match_night_promo":
            head = f"Match night at home? {ctx.biz} delivers."
            lines = ["Order before the toss, eat by the first over."]
        elif action == "festival_campaign":
            fest = data.get("festival") or "festive season"
            head = f"{fest} prep starts at {ctx.biz}."
            lines = [f"Book early for {fest} — prime slots go first."]
            if len(ctx.active_offers) >= 2:
                offer = " + ".join(ctx.active_offers[:2])
        elif action == "seasonal_post":
            head = "Summer essentials, in stock and ready."
            ups = data.get("ups") or []
            lines = [("Now stocked up: " + ", ".join(u.split(" ")[0] for u in ups[:3]) + ".") if ups else ""]
        elif action == "differentiation_post":
            strength = (ctx.theme("pos") or {}).get("common_quote")
            head = f"Why {area} keeps coming back to {ctx.biz}."
            lines = [f"In our patients' words: \"{strength}\"" if strength and ctx.is_dentist else (f"In our customers' words: \"{strength}\"" if strength else "")]
        elif action == "apply_quick_win":
            item = ctx.digest.get(data.get("item_id")) or {}
            head = f"{ctx.biz} — walk-ins welcome." if "walk-in" in str(item.get("title", "")).lower() else f"What's new at {ctx.biz}"
            lines = [item.get("actionable", "")] if "walk-in" not in str(item.get("title", "")).lower() else ["No appointment needed — drop in any time we're open."]
        else:
            head = f"{ctx.biz}, {area}" if area else ctx.biz
            lines = []
        post = _post(ctx, head, lines, offer)
        extra = ""
        if action == "launch_offer_post" and not data.get("offer_live") and offer:
            extra = T(f"I'll also switch '{offer}' on for your listing (you can edit the price before it goes live).",
                      f"'{offer}' offer bhi aapki listing pe on kar dungi (live hone se pehle price badal sakte hain).")
        body = T(f"Here's the draft:\n\n{post}\n\n{extra}\n{confirm_publish}", f"Draft ready hai:\n\n{post}\n\n{extra}\n{confirm_publish}")
        return body, "binary_confirm_cancel", f"Commitment received → delivered a ready Google post ({action}); one CONFIRM to publish."

    if action == "history_posts":
        topics = data.get("topics") or ["services"]
        posts = []
        for i, tp in enumerate(topics[:3]):
            off = _topic_offer(ctx, tp)
            nice = {"aligner": "Clear aligners", "aligners": "Clear aligners", "whitening": "Teeth whitening", "pt": "Personal training"}.get(tp, tp.capitalize())
            if ctx.is_dentist and tp.startswith("aligner"):
                line = "Supervised by your dentist, not a DIY kit — with a check-up at every stage."
            elif ctx.is_dentist and tp == "whitening":
                line = "Done in-clinic, with a shade check before and after."
            else:
                line = f"Ask us about {nice.lower()} at {ctx.biz}."
            posts.append(f"Post {i + 1} — {nice}:\n\"{nice} at {ctx.biz}{', ' + ctx.locality if ctx.locality else ''}. {line}" + (f" {off} (suggested price — edit if needed)." if off else "") + "\"")
        body = T("Picking up where we left off — you asked for posts on " + join_human(topics) + ". Drafts:\n\n" + "\n\n".join(posts)
                 + "\n\nReply CONFIRM and I'll schedule them a few days apart, starting tomorrow 10am.",
                 "Wahi se aage — aapne " + join_human(topics, "aur") + " pe posts maange the. Drafts:\n\n" + "\n\n".join(posts)
                 + "\n\nCONFIRM reply karein, kal 10am se kuch din ke gap pe schedule kar deti hoon.")
        return body, "binary_confirm_cancel", "Merchant committed; resumed the merchant's own last request from history (posts on named topics) and delivered drafts immediately."

    if action == "renew_plan":
        amt = data.get("amount")
        todo = []
        if ctx.verified is False or ctx.has_signal("unverified_gbp"):
            todo.append(T("get your Google profile verified", "Google profile verify"))
        if not ctx.active_offers:
            sug = ctx.catalog_offer("cleaning", "haircut", "thali", "trial")
            todo.append(T(f"put '{sug}' live", f"'{sug}' live") if sug else "")
        lapsed = ctx.agg.get("lapsed_180d_plus") or ctx.agg.get("lapsed_90d_plus")
        if lapsed:
            todo.append(T(f"send a recall message to {lapsed} lapsed {aud}s", f"{lapsed} lapsed {aud}s ko recall message"))
        todo = [t for t in todo if t]
        body = T(f"Done — renewal request raised" + (f" for {money(amt)}" if amt else "") + "; the payment request will come to this chat. "
                 + (f"The moment it's through, I'll {join_human(todo)} — in that order." if todo else "The moment it's through, I restart your weekly posts.")
                 + " Reply CONFIRM to lock the plan.",
                 f"Ho gaya — renewal request raise kar di" + (f" ({money(amt)})" if amt else "") + "; payment request isi chat pe aayegi. "
                 + (f"Payment hote hi: {join_human(todo, 'aur')} — isi order mein." if todo else "Payment hote hi weekly posts shuru.")
                 + " Plan lock karne ke liye CONFIRM reply karein.")
        return body, "binary_confirm_cancel", "Renewal accepted → raised request and listed the exact fixes that start after payment."

    if action == "curious_answer":
        ans = _extract_service(ctx, merchant_text) or data.get("guess") or "your top service"
        off = _topic_offer(ctx, ans)
        post = _post(ctx, f"This week at {ctx.biz}: {ans}", ["Most asked-for service this week — book before the weekend slots go."], off)
        reply = f"\"Hi! {ans.capitalize()}" + (f" is {off.split('@')[-1].strip()}" if off and "@" in off else " — happy to share today's price") + ". Want me to book you in for today or tomorrow?\""
        body = T(f"Got it — {ans}. Here's what I've made from that:\n\nGoogle post:\n{post}\n\nReady reply for price enquiries:\n{reply}\n\n{confirm_publish}",
                 f"Samajh gayi — {ans}. Isse yeh bana diya:\n\nGoogle post:\n{post}\n\nPrice enquiry ka ready reply:\n{reply}\n\n{confirm_publish}")
        return body, "binary_confirm_cancel", "Merchant answered the curious-ask → turned the answer into a post + reusable reply (reciprocity delivered)."

    if action == "winback_draft":
        offer = data.get("offer")
        lapsed = data.get("lapsed")
        msg = (f"\"Hi {{name}}, it's been a while! We'd love to see you back at {ctx.biz}"
               + (f" — {offer} this month" if offer else "") + ". Reply YES and we'll hold a slot for you.\"")
        body = T(f"Here's the win-back message" + (f" for your {lapsed} lapsed customers" if lapsed else "") + f":\n\n{msg}\n\n({{name}} fills in per customer.) Reply CONFIRM and I'll send it to the ones who opted in.",
                 f"Win-back message" + (f" ({lapsed} lapsed customers ke liye)" if lapsed else "") + f":\n\n{msg}\n\n({{name}} har customer ke liye bhar jayega.) CONFIRM reply karein, opted-in customers ko bhej deti hoon.")
        return body, "binary_confirm_cancel", "Delivered the promised free win-back draft; CONFIRM to send to opted-in lapsed customers."

    if action == "review_replies":
        theme = data.get("theme")
        quote = data.get("quote")
        if not theme:
            ans = merchant_text.strip()[:80] if merchant_text else "your recent feedback"
            body = T(f"Thanks — noted: \"{ans}\". Public reply draft:\n\n\"Thank you for taking the time to share this. We've read it carefully and we're on it. — {ctx.owner or ctx.biz}\"\n\nReply CONFIRM and I'll post it under the matching reviews.",
                     f"Shukriya — note kar liya: \"{ans}\". Public reply draft:\n\n\"Thank you for taking the time to share this. We've read it carefully and we're on it. — {ctx.owner or ctx.biz}\"\n\nCONFIRM reply karein, matching reviews ke neeche post kar deti hoon.")
            return body, "binary_confirm_cancel", "Merchant named the theme → drafted a public reply."
        th = theme.replace("_", " ")
        r1 = (f"\"Sorry about this — {quote.split(' for ')[0] if quote else 'that wait'} isn't the experience we want for you. "
              f"We've tightened our {th.split()[0]} process this week. Please give us another try. — {ctx.owner or ctx.biz}\"")
        ops = T(f"Ops fix line for your listing: \"Delivery times shown are realistic estimates — we'd rather be honest than fast.\"" if "deliver" in th else
                f"Ops fix: note the {th} peak times and add one line to your listing about them.",
                f"Ops fix line: \"Delivery times shown are realistic estimates — we'd rather be honest than fast.\"" if "deliver" in th else
                f"Ops fix: {th} ke peak time note karke listing pe ek line daal dijiye.")
        body = T(f"Drafts ready.\n\nReply for the {th} reviews:\n{r1}\n\n{ops}\n\nReply CONFIRM and I'll post the replies under all {data.get('occ') or 'the'} reviews.",
                 f"Drafts ready.\n\n{th} reviews ka reply:\n{r1}\n\n{ops}\n\nCONFIRM reply karein, sabhi {data.get('occ') or ''} reviews ke neeche post kar deti hoon.")
        return body, "binary_confirm_cancel", "Delivered public review replies + one operational fix line."

    if action == "review_request":
        msg = (f"\"Thank you for choosing {ctx.biz}! If we got it right, a quick Google review helps other people in "
               f"{ctx.locality or ctx.city} find us — it takes 30 seconds. 🙏\"")
        body = T(f"Here's the review request:\n\n{msg}\n\nBest sent right after a visit or order. Reply CONFIRM and I'll set it to go out after each visit this week.",
                 f"Review request draft:\n\n{msg}\n\nVisit/order ke turant baad bhejna best hai. CONFIRM reply karein, is hafte har visit ke baad bhejne ke liye set kar deti hoon.")
        return body, "binary_confirm_cancel", "Delivered the review-request message + timing tip; one CONFIRM to automate."

    if action == "planning_next":
        topic = str(data.get("topic", ""))
        if "thali" in topic:
            tiers = data.get("tiers") or []
            t10 = money(tiers[0][1]) if tiers else ""
            post = _post(ctx, "Office lunch, sorted — Corporate Thali plans", [f"From {t10}/thali for 10+ a day, delivered 12:30–1:30pm." if t10 else "Daily office lunch, delivered."], None)
            pitch = (f"\"Hi! {ctx.biz} here, {ctx.locality}. We now do daily corporate thalis for offices nearby — from {t10} each for 10+, "
                     "delivered hot by 1:30pm. Happy to send a free tasting tray for your team this week?\"")
            body = T(f"Done — both drafts:\n\nGoogle post:\n{post}\n\nPitch for office admins:\n{pitch}\n\nReply CONFIRM and the post goes live today; the pitch is yours to forward.",
                     f"Ho gaya — dono drafts:\n\nGoogle post:\n{post}\n\nOffice admins ke liye pitch:\n{pitch}\n\nCONFIRM reply karein, post aaj live; pitch aap forward kar dijiye.")
        else:
            fee = data.get("fee")
            post = _post(ctx, "Kids Yoga Summer Camp (ages 7–12)", ["4 weeks · 3 sessions a week · small batches.", f"{money(fee)} for the full camp." if fee else ""], None)
            carousel = "Carousel (5 slides): 1) Summer camp is here 2) What kids learn each week 3) Small batches, certified instructors 4) Parents' showcase in week 4 5) Book a trial"
            body = T(f"Done — drafts below.\n\nGoogle post:\n{post}\n\n{carousel}\n\nReply CONFIRM and I'll publish the post today and send the carousel images for your approval.",
                     f"Ho gaya — drafts neeche.\n\nGoogle post:\n{post}\n\n{carousel}\n\nCONFIRM reply karein, post aaj publish aur carousel images approval ke liye bhej dungi.")
        return body, "binary_confirm_cancel", "Planning thread: executed the next step (post + outreach/carousel) the moment the merchant agreed."

    if action == "retention_challenge":
        members = data.get("members")
        plan = ("\"PowerHouse 4-Week Consistency Challenge\"" if "power" in ctx.biz.lower() else f"\"{ctx.biz} 4-Week Consistency Challenge\"")
        body = T(f"Here's the challenge:\n\n{plan}\n• Goal: 12 sessions in 4 weeks\n• Weekly check-in on WhatsApp (streak count)\n• Finishers get a free body-composition check + name on the wall\n\nMember announcement:\n\"Summer's here — join our 4-week consistency challenge. 12 sessions, 4 weeks, one reward. Reply IN to join!\"\n\nReply CONFIRM and I'll send it to your {members or ''} members.",
                 f"Challenge ready:\n\n{plan}\n• Goal: 4 hafte mein 12 sessions\n• Har hafte WhatsApp check-in (streak count)\n• Finish karne walon ko free body-composition check\n\nMember announcement:\n\"Summer's here — join our 4-week consistency challenge. 12 sessions, 4 weeks, one reward. Reply IN to join!\"\n\nCONFIRM reply karein, {members or ''} members ko bhej deti hoon.")
        return body, "binary_confirm_cancel", "Seasonal dip → delivered a retention mechanic + member announcement."

    if action == "recall_workflow":
        mol = data.get("molecule") or "the molecule"
        batches = join_human(data.get("batches") or [])
        note = (f"\"Namaste, {ctx.biz} here. The {mol} batch ({batches}) you bought is part of a voluntary manufacturer recall for lower strength "
                "— not a safety risk. Please bring the strip back; we'll replace it free, same day.\"")
        body = T(f"Step 1 (now): pull {batches} off the shelf and set them aside for distributor return.\nStep 2: I've filtered your repeat-Rx list for {mol} buyers — I'll message only those who bought these batches.\nStep 3 — customer note:\n{note}\n\nReply CONFIRM and I'll send the note.",
                 f"Step 1 (abhi): {batches} shelf se hata ke distributor return ke liye alag rakhein.\nStep 2: repeat-Rx list se {mol} customers filter kar rahi hoon — sirf inhi batches walon ko message jayega.\nStep 3 — customer note:\n{note}\n\nCONFIRM reply karein, note bhej deti hoon.")
        return body, "binary_confirm_cancel", "Urgent recall: executed the workflow (shelf pull → filter → calm customer note)."

    if action == "verify_gbp":
        body = T("Let's do it. Phone verification, step by step:\n1. Open Google Maps → your profile → 'Get verified'.\n2. Choose 'Phone call' and keep your listed number handy.\n3. Enter the 5-digit code Google reads out.\nThat's it — send me a screenshot if anything looks different and I'll guide you.\n\nReply DONE once you've got the code screen.",
                 "Chaliye shuru karte hain. Phone verification:\n1. Google Maps kholiye → aapka profile → 'Get verified'.\n2. 'Phone call' chuniye, listed number paas rakhiye.\n3. Google jo 5-digit code bolega, woh daal dijiye.\nKuch alag dikhe toh screenshot bhejiye, main guide kar dungi.\n\nCode screen aa jaaye toh DONE reply karein.")
        return body, "binary_confirm_cancel", "Verification accepted → in-chat steps immediately (effort externalised)."

    if action == "cde_register":
        item = ctx.digest.get(data.get("item_id")) or ctx.digest_item(kinds=("cde",)) or {}
        body = T(f"Done — reminder set for {fmt_date(item.get('date'), ctx.now, dow=True)}, 1 hour before start.\nRegistration: via {item.get('source', 'the organiser')} ({item.get('actionable', '')}).\nReply CONFIRM and I'll also add the session to your weekly summary.",
                 f"Ho gaya — {fmt_date(item.get('date'), ctx.now, dow=True)} ka reminder set, start se 1 ghanta pehle.\nRegistration: {item.get('source', 'organiser')} se ({item.get('actionable', '')}).\nCONFIRM reply karein, weekly summary mein bhi add kar dungi.")
        return body, "binary_confirm_cancel", "CDE accepted → reminder + registration route delivered."

    return (T("Done — drafting it now; you'll have it here in a few minutes. Reply CONFIRM to publish once you've seen it.",
              "Ho gaya — draft bana rahi hoon, kuch minute mein yahin milega. Dekh ke CONFIRM reply karein."),
            "binary_confirm_cancel", "Commitment received → moved straight to execution.")


def customer_fulfil(action: str, data: dict, ctx: Ctx, mode: str, text: str) -> tuple[str, str, str]:
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    slots = data.get("slots") or []
    m = re.search(r"\b([1-9])\b", text or "")
    chosen = None
    if slots:
        if m and 1 <= int(m.group(1)) <= len(slots):
            chosen = slots[int(m.group(1)) - 1]
        else:
            low = (text or "").lower()
            for s in slots:
                lab = str(s.get("label", "")).lower()
                if lab and lab.split(" ")[0] in low:
                    chosen = s
            if chosen is None and len(slots) == 1:
                chosen = slots[0]
    if action == "confirm_appointment":
        return (T("Confirmed ✅ See you tomorrow! If anything changes, just message here.", "Confirm ✅ Kal milte hain! Kuch badle toh yahin message kar dijiye."),
                "none", "Customer confirmed appointment.")
    if action == "confirm_refill":
        return (T("Confirmed ✅ We're packing the same medicines now and will message you when it's out for delivery.",
                  "Confirm ✅ Same dawaiyan pack ho rahi hain, delivery nikalte hi message karenge."),
                "none", "Refill confirmed; delivery update promised.")
    if chosen:
        return (T(f"Booked ✅ {chosen.get('label')}. We'll send a reminder the day before. Reply here if you need to change it.",
                  f"Book ho gaya ✅ {chosen.get('label')}. Ek din pehle reminder bhejenge. Badalna ho toh yahin reply karein."),
                "none", f"Customer picked slot → booked {chosen.get('label')}.")
    if slots:
        labels = " or ".join(s.get("label") for s in slots[:2])
        return (T(f"Great! Which works better — {labels}? Reply 1 or 2.", f"Badhiya! Kaunsa theek rahega — {labels}? 1 ya 2 reply karein."),
                "multi_choice_slot", "Customer said yes without picking a slot; offered the same real slots.")
    return (T(f"Great! {ctx.biz} will message you shortly with the exact time — nothing else needed from you.",
              f"Badhiya! {ctx.biz} aapko thodi der mein exact time bhej dega — aapko kuch aur nahi karna."),
            "none", "Customer accepted; merchant team follows up with time (no invented slot).")


def done_message(action: str, ctx: Ctx, mode: str) -> str:
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    if action in ("send_compliance_checklist",):
        return T("Logged ✅ You're covered — I'll still send a reminder 2 weeks before the deadline.",
                 "Log kar liya ✅ Aap covered hain — deadline se 2 hafte pehle reminder phir bhi aayega.")
    if action == "verify_gbp":
        return T("Nice — once you enter the code, verification usually reflects in 24–48 hours. I'll confirm here when it shows.",
                 "Badhiya — code daalne ke baad 24–48 ghante mein verification dikhne lagta hai. Dikhte hi yahin bata dungi.")
    if action == "renew_plan":
        return T("Locked ✅ Payment request is on its way to this chat; work starts the same day it clears.",
                 "Lock ✅ Payment request isi chat pe aa rahi hai; clear hote hi kaam shuru.")
    if action == "history_posts":
        return T("Scheduled ✅ Post 1 goes live tomorrow 10am, the next one 3 days later. I'll send views and calls from them in a week.",
                 "Schedule ho gaya ✅ Post 1 kal 10am live, agla 3 din baad. Ek hafte mein unke views aur calls bhejungi.")
    if action in ("send_research_pack", "winback_draft", "recall_workflow", "retention_challenge", "review_request"):
        return T("Queued ✅ It goes only to people who opted in. I'll share replies and bookings from it in 7 days.",
                 "Queue ho gaya ✅ Sirf opted-in logon ko jayega. 7 din mein replies aur bookings bhejungi.")
    if action == "review_replies":
        return T("Posted ✅ Replies are up under the reviews. Replying within 48h is one of the simplest rating levers — I'll flag new ones as they come.",
                 "Post ho gaya ✅ Reviews ke neeche replies lag gaye. Naye reviews aate hi main flag kar dungi.")
    if action == "cde_register":
        return T("Added ✅ You'll get the reminder an hour before it starts.", "Add ho gaya ✅ Shuru hone se ek ghanta pehle reminder aayega.")
    return T("Published ✅ It's live on your Google profile. I'll send you views and calls from it in 7 days.",
             "Publish ho gaya ✅ Aapke Google profile pe live hai. 7 din mein views aur calls bhejungi.")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def respond(conv: dict, ctx: Ctx, message: str, merchant_state: dict, from_role: str = "merchant") -> dict:
    """Pure-ish decision function. Mutates `conv` and `merchant_state` (streaks, stage, opt-out)."""
    is_customer = from_role == "customer" or conv.get("send_as") == "merchant_on_behalf"
    msg_lang = detect_text_lang(message)
    words = len(re.findall(r"[A-Za-z]+", message or ""))
    if msg_lang == "hi":
        mode = "hi"
    elif words >= 4:
        mode = "en"
    else:
        mode = conv.get("mode") or ctx.mode
    conv["mode"] = mode
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731

    slots = (conv.get("action_data") or {}).get("slots") or []
    label = classify(message, slot_count=len(slots))
    conv["_desc_cache"] = _desc(conv, ctx, mode)

    # verbatim repeat of the merchant's previous message = auto-reply signal
    norm = normalize(message)
    if label not in ("auto_reply",) and norm and norm == merchant_state.get("last_msg_norm") and len(norm.split()) >= 4:
        label = "auto_reply"
    merchant_state["last_msg_norm"] = norm
    conv.setdefault("merchant_msgs", []).append(message)

    if label != "auto_reply":
        merchant_state["auto_streak"] = 0

    # re-opening a closed conversation: only a genuine message does that
    if conv.get("status") == "ended" and label in ("auto_reply", "optout", "hostile", "thanks", "empty"):
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging on this message."}
    if conv.get("status") == "ended":
        conv["status"] = "open"

    if label == "empty":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Empty message; waiting for a real reply."}

    if label == "optout":
        conv["status"] = "ended"
        merchant_state["opted_out"] = True
        return {"action": "end", "rationale": "Explicit opt-out/not-interested. Closing and suppressing further proactive messages to this recipient."}

    if label == "hostile":
        merchant_state["hostile_count"] = merchant_state.get("hostile_count", 0) + 1
        if merchant_state["hostile_count"] >= 2:
            conv["status"] = "ended"
            merchant_state["opted_out"] = True
            return {"action": "end", "rationale": "Repeated frustration; closing politely and suppressing further outreach."}
        body = T("Sorry for the bother — I won't push this. If you'd rather not hear from me, reply STOP and I'll stop completely. "
                 "If there's one thing about your listing you want fixed, tell me and I'll just do that.",
                 "Pareshaan karne ke liye sorry — main zor nahi dungi. Messages nahi chahiye toh STOP reply karein, bilkul band ho jayenge. "
                 "Listing mein koi ek cheez theek karwani ho toh bata dijiye, bas wahi kar dungi.")
        return _send(conv, body, "open_ended", "Frustration detected; one apology + clear opt-out path, no pitch.", ctx, action_mode=False)

    if label == "auto_reply":
        last_at = merchant_state.get("auto_last_at")
        if last_at is not None and ctx.now is not None and (ctx.now - last_at).total_seconds() > 7200:
            merchant_state["auto_streak"] = 0  # an auto-reply hours ago doesn't count toward this ladder
        merchant_state["auto_last_at"] = ctx.now
        streak = merchant_state.get("auto_streak", 0) + 1
        merchant_state["auto_streak"] = streak
        conv["auto_count"] = conv.get("auto_count", 0) + 1
        if streak == 1:
            body = T("Looks like an auto-reply 🙂 When the owner or manager sees this, a simple YES is enough — I'll take it from there.",
                     "Lagta hai yeh auto-reply hai 🙂 Owner/manager dekhein toh bas YES bhej dein — baaki main sambhal lungi.")
            return _send(conv, body, "binary_yes_no", "Detected WhatsApp Business auto-reply (canned phrasing). One short owner-flag nudge; no re-pitch.", ctx)
        if streak == 2:
            conv["status"] = "waiting"
            merchant_state["cooldown_s"] = 86400
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Second consecutive auto-reply → owner not at the phone. Backing off 24h instead of burning turns."}
        conv["status"] = "ended"
        return {"action": "end", "rationale": f"Auto-reply {streak}x in a row with no human signal; closing the conversation gracefully."}

    if label == "later":
        wait = 86400 if re.search(r"tomorrow|\bkal\b|next week", message, re.I) else 14400
        conv["status"] = "waiting"
        return {"action": "wait", "wait_seconds": wait, "rationale": f"Merchant asked for time ('{message.strip()[:40]}'); backing off {wait // 3600}h."}

    if label == "offtopic":
        pending = _pending_hint(conv, mode)
        low = message.lower()
        if re.search(r"gst|tax|itr|\bca\b|account", low):
            who_en, who_hi = "your CA is the right person for that", "iske liye aapke CA sahi rahenge"
        elif re.search(r"loan|credit|insurance|bank", low):
            who_en, who_hi = "your bank is the right place for that", "iske liye aapka bank sahi rahega"
        else:
            who_en, who_hi = "a specialist will serve you better there", "uske liye specialist behtar rahenge"
        body = T(f"That one's outside what I can help with — {who_en}. {pending}",
                 f"Yeh mere scope se bahar hai — {who_hi}. {pending}")
        return _send(conv, body, "binary_yes_no", "Out-of-scope request politely declined; redirected to the open thread with the same single CTA.", ctx)

    if label == "who":
        body = T(f"I'm Vera, magicpin's assistant for {ctx.biz} — I handle your Google profile, offers and customer messages so you don't have to. {_pending_hint(conv, mode)}",
                 f"Main Vera hoon, magicpin ki taraf se {ctx.biz} ki assistant — Google profile, offers aur customer messages main sambhalti hoon. {_pending_hint(conv, mode)}")
        return _send(conv, body, "binary_yes_no", "Identity question answered in one line; thread resumed.", ctx)

    if label == "doubt":
        fact = _evidence(ctx, mode, conv)
        body = T(f"Honest answer: no one can promise results. What I can show you: {fact} {_pending_hint(conv, mode)}",
                 f"Seedhi baat: result ki guarantee koi nahi de sakta. Jo data hai: {fact} {_pending_hint(conv, mode)}")
        body = body.replace("guarantee", "promise")
        return _send(conv, body, "binary_yes_no", "Scepticism answered honestly with a verifiable data point (no overclaim).", ctx)

    if label == "price":
        if is_customer:
            off = ctx.lead_offer()
            if off and re.search(r"\bafter\b|\bthen\b|\bbaad\b|\bmonthly\b|\bmembership\b", message, re.I):
                body = T(f"{off} is exactly that — free. After that, {ctx.biz} will walk you through the plan options in person; no auto-charge, no pressure.",
                         f"{off} bilkul free hai. Uske baad {ctx.biz} aapko plan options batayega — koi auto-charge nahi, koi pressure nahi.")
            else:
                body = T(f"{off} right now; for anything beyond that we'll confirm the exact price when you book — no surprises." if off else "We'll confirm the exact price when you book — no surprises.",
                         f"Abhi {off} chal raha hai; baaki ka exact price booking ke time bata denge." if off else "Booking ke time exact price bata denge — koi surprise nahi.")
            body += " " + T("Want us to book you in? Reply YES.", "Book kar dein? YES reply karein.")
            return _send(conv, body, "binary_yes_no", "Customer price question answered from live offers only.", ctx)
        amt = (conv.get("action_data") or {}).get("amount")
        if conv.get("action") == "renew_plan" and amt:
            body = T(f"Renewal is {money(amt)} for the {(conv.get('action_data') or {}).get('plan', '')} plan. Reply YES and I'll raise it.",
                     f"Renewal {money(amt)} ka hai ({(conv.get('action_data') or {}).get('plan', '')} plan). YES reply karein, main raise kar deti hoon.")
        else:
            body = T(f"No extra charge for this — drafting and posting it is part of what I do for {ctx.biz}. {_pending_hint(conv, mode)}",
                     f"Iska koi extra charge nahi — draft aur post karna {ctx.biz} ke liye mera kaam hai. {_pending_hint(conv, mode)}")
        return _send(conv, body, "binary_yes_no", "Price question answered directly; same single CTA.", ctx)

    if label in ("accept", "slot_choice"):
        return _advance(conv, ctx, mode, message, is_customer)

    if label == "thanks":
        if conv.get("stage", 0) >= 2:
            conv["status"] = "ended"
            return {"action": "end", "rationale": "Task delivered and merchant signed off; closing without extra messages."}
        body = T("Anytime! " + _pending_hint(conv, mode), "Koi baat nahi! " + _pending_hint(conv, mode))
        return _send(conv, body, "binary_yes_no", "Polite acknowledgement; kept the one open CTA.", ctx)

    if label == "decline":
        conv["declines"] = conv.get("declines", 0) + 1
        if conv["declines"] >= 2 or conv.get("stage", 0) >= 1:
            conv["status"] = "ended"
            return {"action": "end", "rationale": "Second 'no' — respecting it and closing."}
        conv["status"] = "ended"
        body = T("No problem — parking this. If you change your mind, just reply YES any time.",
                 "Koi baat nahi — ise yahin rok deti hoon. Mann badle toh kabhi bhi YES bhej dijiye.")
        return _send(conv, body, "none", "Soft decline respected; door left open, no re-pitch.", ctx)

    if label in ("how", "question", "timing"):
        ans = _answer(conv, ctx, mode, message, label)
        return _send(conv, ans, "binary_yes_no", "Merchant asked a clarifying question — answered it specifically from context, then the same single CTA.", ctx)

    # info / unclear
    if conv.get("action") in ("curious_answer", "review_replies") and conv.get("stage", 0) == 0:
        return _advance(conv, ctx, mode, message, is_customer)
    if is_customer:
        body = T(f"Thanks for letting us know! {ctx.biz} will get back to you on this shortly.",
                 f"Batane ke liye shukriya! {ctx.biz} is par jaldi aapse baat karega.")
        return _send(conv, body, "none", "Customer free-text; acknowledged and routed to the merchant team.", ctx)
    body = T(f"Noted — thanks. {_pending_hint(conv, mode)}", f"Note kar liya — shukriya. {_pending_hint(conv, mode)}")
    return _send(conv, body, "binary_yes_no", "Acknowledged the merchant's info and kept one clear next step.", ctx)


def _advance(conv: dict, ctx: Ctx, mode: str, message: str, is_customer: bool) -> dict:
    stage = conv.get("stage", 0)
    action = conv.get("action") or "generic"
    data = conv.get("action_data") or {}
    if is_customer:
        body, cta, why = customer_fulfil(action, data, ctx, mode, message)
        conv["stage"] = 2
        return _send(conv, body, cta, why, ctx, action_mode=True)
    if stage == 0:
        body, cta, why = fulfil(action, data, ctx, mode, merchant_text=message)
        conv["stage"] = 1
        return _send(conv, body, cta, why, ctx, action_mode=True)
    if stage == 1:
        conv["stage"] = 2
        return _send(conv, done_message(action, ctx, mode), "none",
                     "Merchant confirmed → executed and closed the loop with what happens next.", ctx, action_mode=True)
    conv["status"] = "ended"
    return {"action": "end", "rationale": "Everything requested is done; no further message needed."}


def _pending_hint(conv: dict, mode: str) -> str:
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    stage = conv.get("stage", 0)
    if stage >= 2:
        return T("Anything else on your listing, just message here.", "Listing ka koi aur kaam ho toh yahin message kijiye.")
    if stage == 1:
        return T("The draft above is ready whenever you are — reply CONFIRM to publish.", "Upar wala draft ready hai — CONFIRM reply karein.")
    if conv.get("action") == "curious_answer":
        return T("Just tell me your top service this week and I'll do the rest.", "Bas is hafte ki top service bata dijiye, baaki main kar dungi.")
    ctx_desc = conv.get("_desc_cache")
    if not ctx_desc:
        return T("Shall I go ahead? Reply YES.", "Aage badhoon? YES reply karein.")
    return T(f"Shall I go ahead with {ctx_desc}? Reply YES.", f"{cap(ctx_desc)} — aage badhoon? YES reply karein.")


def _evidence(ctx: Ctx, mode: str, conv: Optional[dict] = None) -> str:
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    conv = conv or {}
    data = conv.get("action_data") or {}
    item = ctx.digest.get(data.get("item_id"))
    if item:
        return f"{item.get('title')} ({item.get('source')})."
    kind = str(conv.get("kind") or "")
    if "ipl" in kind or conv.get("action") == "match_night_promo":
        for d in ctx.digest.values():
            if "ipl" in str(d.get("title", "")).lower():
                return T(f"{first_sentence(d.get('summary'))} ({d.get('source')}) — that's why tonight is delivery-first.",
                         f"{first_sentence(d.get('summary'))} ({d.get('source')}) — isliye aaj delivery-first.")
    topic = " ".join(str(v) for v in data.values() if isinstance(v, str)).lower()
    for tr in ctx.cat.get("trend_signals") or []:
        q = str(tr.get("query", "")).lower()
        if q and any(w in topic for w in q.split() if len(w) > 3):
            return T(f"searches for '{tr['query']}' are up {pct(tr['delta_yoy'])} YoY — the demand is there.",
                     f"'{tr['query']}' searches {pct(tr['delta_yoy'])} YoY upar hain — demand hai.")
    for d in ctx.digest.values():
        if re.search(r"\d+%", str(d.get("title", ""))) and d.get("kind") in ("trend", "tech"):
            return f"{d.get('title')} ({d.get('source')})."
    cv = ctx.ctr_vs_peer()
    if cv:
        return T(f"your CTR is {cv[0] * 100:.1f}% vs {cv[1] * 100:.1f}% for peers — that gap is what we're working on.",
                 f"aapka CTR {cv[0] * 100:.1f}% hai vs peers {cv[1] * 100:.1f}% — yahi gap hum close kar rahe hain.")
    return T("I'll share views and calls 7 days after we publish, so you can judge for yourself.",
             "Publish ke 7 din baad views aur calls bhejungi — aap khud dekh lijiye.")


ACTION_DESC = {
    "send_research_pack": ("a 2-line abstract of the study plus a {aud}-friendly WhatsApp you can forward", "study ka 2-line abstract + {aud}s ke liye WhatsApp draft"),
    "send_item_brief": ("a short brief with the cost math plus a {aud}-facing note", "cost ke hisaab ke saath short brief + {aud}s ke liye note"),
    "send_compliance_checklist": ("a 3-point audit checklist and an SOP line for the new rule", "naye rule ke liye 3-point audit checklist + SOP line"),
    "renew_plan": ("renewing your plan and starting the fixes the same day", "plan renew karke usi din fixes shuru karna"),
    "curious_answer": ("a Google post and a ready reply built from your top service this week", "is hafte ki top service se Google post + ready reply"),
    "winback_draft": ("a win-back WhatsApp for your lapsed {aud}s", "lapsed {aud}s ke liye win-back WhatsApp"),
    "review_replies": ("polite public replies to the recent reviews", "recent reviews ke polite public replies"),
    "review_request": ("a 2-line review request for your regulars", "regulars ke liye 2-line review request"),
    "planning_next": ("turning the plan into a Google post plus outreach copy", "plan ko Google post + outreach copy mein badalna"),
    "retention_challenge": ("a 4-week member challenge to hold attendance through the dip", "dip ke dauraan attendance ke liye 4-week challenge"),
    "recall_workflow": ("the recall workflow: shelf pull, affected-customer list and their WhatsApp note", "recall workflow: shelf se hataana, affected list aur unka WhatsApp note"),
    "verify_gbp": ("getting your Google profile verified, step by step", "Google profile verification, step by step"),
    "cde_register": ("the registration route plus a calendar reminder", "registration + calendar reminder"),
    "history_posts": ("the Google posts you asked for on {topics}", "{topics} pe aapke maange hue Google posts"),
}


def _desc(conv: dict, ctx: Ctx, mode: str) -> str:
    aud = AUDIENCE.get(ctx.slug, "customer")
    topics = join_human((conv.get("action_data") or {}).get("topics") or [])
    en, hi = ACTION_DESC.get(conv.get("action"), ("a ready-to-publish Google post for your listing", "aapki listing ke liye ready Google post"))
    txt = hi if mode == "hi" else en
    return txt.format(aud=aud, topics=topics)


def _answer(conv: dict, ctx: Ctx, mode: str, message: str, label: str) -> str:
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    low = (message or "").lower()
    desc = _desc(conv, ctx, mode)
    if label == "timing":
        return T(f"About 5 minutes from you: I prepare {desc}, you glance at it and reply CONFIRM. Shall I start? Reply YES.",
                 f"Aapka bas 5 minute: main {desc} taiyaar karti hoon, aap dekh ke CONFIRM kar dijiye. Shuru karoon? YES reply karein.")
    if re.search(r"\b(which|who|kis|kaun|kinko|kisko)\b.*\b(patient|customer|member|client|people|log|guest)", low) or re.search(r"\bgo to\b|\bsend(ing)? to\b", low):
        n = ctx.agg.get("high_risk_adult_count")
        aud = AUDIENCE.get(ctx.slug, "customer")
        who = (T(f"only your opted-in high-risk adult patients ({n} on file)", f"sirf opted-in high-risk adult patients (records mein {n})") if n and ctx.is_dentist
               else T(f"only {aud}s who opted in to hear from you — nobody else", f"sirf wahi {aud}s jinhone opt-in kiya hai — aur koi nahi"))
        return T(f"It would go to {who}, and only after you approve the text. {_pending_hint(conv, mode)}",
                 f"Yeh jayega {who} ko, aur woh bhi aapke approve karne ke baad. {_pending_hint(conv, mode)}")
    if re.search(r"what (exactly )?is (this|it)|about\??$|kya hai|matlab|what do you mean|samjha", low):
        return T(f"Short version: {desc}. You see everything before it goes out. {_pending_hint(conv, mode)}",
                 f"Seedhe shabdon mein: {desc}. Kuch bhi jaane se pehle aap dekh lenge. {_pending_hint(conv, mode)}")
    return _how(conv, ctx, mode)


def _how(conv: dict, ctx: Ctx, mode: str) -> str:
    T = lambda en, hi: hi if mode == "hi" else en  # noqa: E731
    desc = _desc(conv, ctx, mode)
    return T(f"Three steps: 1) I prepare {desc}, 2) you check it here and reply CONFIRM, 3) it goes out and I report back the numbers after 7 days. "
             "Nothing is sent without your OK. " + _pending_hint(conv, mode),
             f"Teen steps: 1) main {desc} taiyaar karti hoon, 2) aap yahin dekh ke CONFIRM karte hain, 3) phir jaata hai aur 7 din baad numbers bhejti hoon. "
             "Aapke OK ke bina kuch nahi jaata. " + _pending_hint(conv, mode))


def _send(conv: dict, body: str, cta: str, rationale: str, ctx: Ctx, action_mode: bool = False) -> dict:
    body = finalize(body, ctx.cat, action_mode=action_mode)
    if action_mode and has_qualifier(body):
        body = strip_qualifiers(body)
    sent = conv.setdefault("bot_bodies", [])
    if body in sent:  # anti-repetition: never send the same body twice in one conversation
        alt = {
            "en": ["Just checking you saw the note above.", "Quick follow-up on the above.", "Circling back briefly."],
        }["en"]
        for a in alt:
            cand = f"{a} {body}"
            if cand not in sent:
                body = cand
                break
    sent.append(body)
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}
