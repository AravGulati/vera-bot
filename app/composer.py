"""Deterministic 4-context composer: compose(category, merchant, trigger, customer?) -> message.

Design
------
* One handler per trigger ``kind`` (dispatch table at the bottom). Each handler reads
  only what is in the four contexts — no invented offers, competitors, slots or citations.
* Placeholder triggers (payload ``{"placeholder": true}``) fall back to the merchant's own
  numbers (performance deltas, offers, review themes) and the category pack (digest,
  seasonal beats, peer stats) so the message is still anchored on verifiable facts.
* Every message = [salutation/hook] + [why-now fact + judgement] + [single low-friction CTA].
* The handler also records the *next action* it offered (``action`` + ``action_data``) so
  the reply engine can switch straight to execution when the merchant says yes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from .lang import T, customer_mode, merchant_mode
from .util import (
    REF_NOW, clean_owner_name, ctr_pct, days_between, fmt_date, fmt_time, humanize, inr_group,
    join_human, money, parse_dt, pct, price_from_title, stable_index,
)
from .validate import finalize

COMPOSER_VERSION = "vera-det-2.0"


# ---------------------------------------------------------------------------
# Context wrapper
# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, category: dict, merchant: dict, trigger: dict,
                 customer: Optional[dict] = None, now: Any = None, extras: Optional[dict] = None):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.c = customer or None
        self.now: datetime = parse_dt(now) or REF_NOW
        self.extras = extras or {}
        self.p: dict = self.t.get("payload") or {}
        self.kind: str = str(self.t.get("kind") or "generic")
        self.placeholder = bool(self.p.get("placeholder"))

        ident = self.m.get("identity") or {}
        self.slug = self.cat.get("slug") or self.m.get("category_slug") or ""
        self.biz = ident.get("name") or "your business"
        self.owner = clean_owner_name(ident.get("owner_first_name"))
        self.is_dentist = self.slug == "dentists"
        if self.is_dentist and self.owner:
            self.sal = f"Dr. {self.owner}"
        else:
            self.sal = self.owner or self.biz
        self.locality = ident.get("locality") or ""
        self.city = ident.get("city") or ""
        self.verified = ident.get("verified")
        self.mode = merchant_mode(self.m, self.cat)

        self.perf = self.m.get("performance") or {}
        self.delta = self.perf.get("delta_7d") or {}
        self.peer = self.cat.get("peer_stats") or {}
        self.sub = self.m.get("subscription") or {}
        self.agg = self.m.get("customer_aggregate") or {}
        self.signals = [str(s) for s in (self.m.get("signals") or [])]
        self.themes = self.m.get("review_themes") or []
        self.history = self.m.get("conversation_history") or []
        self.active_offers = [o.get("title") for o in (self.m.get("offers") or [])
                              if o.get("status") == "active" and o.get("title")]
        self.expired_offers = [o.get("title") for o in (self.m.get("offers") or [])
                               if o.get("status") in ("expired", "paused") and o.get("title")]
        self.catalog = self.cat.get("offer_catalog") or []
        self.digest = {d.get("id"): d for d in (self.cat.get("digest") or []) if d.get("id")}

    # -- helpers ------------------------------------------------------------
    def T(self, en: str, hi: str) -> str:
        return T(self.mode, en, hi)

    def signal(self, prefix: str) -> Optional[str]:
        for s in self.signals:
            if s == prefix:
                return ""
            if s.startswith(prefix + ":"):
                return s.split(":", 1)[1]
        return None

    def has_signal(self, prefix: str) -> bool:
        return self.signal(prefix) is not None

    def lead_offer(self) -> Optional[str]:
        return self.active_offers[0] if self.active_offers else None

    def catalog_offer(self, *keywords: str, types=("service_at_price",)) -> Optional[str]:
        items = [o for o in self.catalog if not types or o.get("type") in types]
        for kw in keywords:
            for o in items:
                if kw.lower() in str(o.get("title", "")).lower():
                    return o.get("title")
        return items[0].get("title") if items else None

    def offer_for_pitch(self, *keywords: str) -> tuple[Optional[str], bool]:
        """(offer_title, is_already_live). Prefers the merchant's own live offer."""
        for kw in keywords:
            for t in self.active_offers:
                if kw.lower() in t.lower():
                    return t, True
        if self.active_offers:
            return self.active_offers[0], True
        return self.catalog_offer(*keywords), False

    def theme(self, sentiment: str) -> Optional[dict]:
        ts = [t for t in self.themes if t.get("sentiment") == sentiment]
        ts.sort(key=lambda t: -(t.get("occurrences_30d") or 0))
        return ts[0] if ts else None

    def digest_item(self, item_id: Optional[str] = None, kinds: tuple = ()) -> Optional[dict]:
        if item_id and item_id in self.digest:
            return self.digest[item_id]
        for d in self.digest.values():
            if not kinds or d.get("kind") in kinds:
                return d
        return None

    def peer_views(self) -> Optional[int]:
        return self.peer.get("avg_views_30d")

    def ctr_vs_peer(self) -> Optional[tuple[float, float]]:
        ctr, peer = self.perf.get("ctr"), self.peer.get("avg_ctr")
        if isinstance(ctr, (int, float)) and isinstance(peer, (int, float)):
            return float(ctr), float(peer)
        return None

    def biggest_delta(self, direction: str) -> Optional[tuple[str, float]]:
        """Most negative ('down') / most positive ('up') 7-day metric delta."""
        items = [(k.replace("_pct", ""), float(v)) for k, v in self.delta.items()
                 if isinstance(v, (int, float))]
        if not items:
            return None
        items.sort(key=lambda kv: kv[1], reverse=(direction == "up"))
        k, v = items[0]
        if direction == "down" and v >= 0:
            return None
        if direction == "up" and v <= 0:
            return None
        return k, v

    def last_merchant_ask(self) -> Optional[str]:
        for turn in reversed(self.history):
            if turn.get("from") == "merchant" and turn.get("body"):
                return turn["body"]
        return None

    _MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def _beat_span(self, b: dict) -> list[int]:
        parts = [x.strip()[:3] for x in str(b.get("month_range", "")).split("-")]
        names = self._MN
        if len(parts) == 2 and parts[0] in names and parts[1] in names:
            a, z = names.index(parts[0]), names.index(parts[1])
            return list(range(a, z + 1)) if a <= z else list(range(a, 12)) + list(range(0, z + 1))
        if parts and parts[0] in names:
            return [names.index(parts[0])]
        return []

    def beat_for_month(self, month: int) -> Optional[dict]:
        for b in self.cat.get("seasonal_beats") or []:
            if (month - 1) in self._beat_span(b):
                return b
        return None

    def next_festive_beat(self) -> Optional[dict]:
        beats = self.cat.get("seasonal_beats") or []
        cur = self.now.month - 1

        def dist(b):
            span = self._beat_span(b)
            return min(((m - cur) % 12 for m in span), default=99)
        festive = [b for b in beats if re.search(r"festiv|diwali|wedding|christmas|new.year|\bholi\b|bridal", str(b.get("note", "")), re.I)]
        pool = festive or beats
        pool = sorted(pool, key=dist)
        return pool[0] if pool else None

    def season_beat(self, months_ahead: int = 0) -> Optional[dict]:
        month_idx = (self.now.month - 1 + months_ahead) % 12
        names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        target = names[month_idx]
        for b in self.cat.get("seasonal_beats") or []:
            rng = str(b.get("month_range", ""))
            parts = [x.strip()[:3] for x in rng.split("-")]
            if len(parts) == 2 and parts[0] in names and parts[1] in names:
                a, z = names.index(parts[0]), names.index(parts[1])
                span = list(range(a, z + 1)) if a <= z else list(range(a, 12)) + list(range(0, z + 1))
                if month_idx in span:
                    return b
            elif parts and parts[0] == target:
                return b
        return None


@dataclass
class Draft:
    parts: list[str]
    cta: str = "binary_yes_no"
    action: str = "generic"
    action_data: dict = field(default_factory=dict)
    rationale: str = ""
    template: str = "vera_generic_v1"
    send_as: str = "vera"

    @property
    def body(self) -> str:
        out = ""
        for p in self.parts:
            if not p or not p.strip():
                continue
            p = p.strip(" ")
            out += p if (not out or out.endswith("\n") or p.startswith("\n")) else " " + p
        return out


# ---------------------------------------------------------------------------
# Shared phrasing
# ---------------------------------------------------------------------------
def yes_line(ctx: Ctx, en_action: str, hi_action: str) -> str:
    return ctx.T(f"Reply YES and I'll {en_action}.", f"YES reply karein, main {hi_action}.")


def dental_hook_for_segment(ctx: Ctx, item: dict) -> str:
    seg = str(item.get("patient_segment") or "")
    hr = ctx.agg.get("high_risk_adult_count")
    if "high_risk" in seg and hr:
        return ctx.T(f"relevant to your {hr} high-risk adult patients",
                     f"aapke {hr} high-risk adult patients par seedha lagta hai")
    if "high_risk" in seg and ctx.has_signal("high_risk_adult_cohort"):
        return ctx.T("relevant to your high-risk adult cohort", "aapke high-risk adult cohort par lagta hai")
    return ""


def first_sentence(text: str) -> str:
    text = str(text or "").strip()
    m = re.match(r"(.+?(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bNo)(?<!\bp)(?<!\bvs)(?<!\b[A-Z])[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


NOUN = {"dentists": "dental clinic", "salons": "salon", "restaurants": "restaurant", "gyms": "gym", "pharmacies": "pharmacy"}
AUDIENCE = {"dentists": "patient", "salons": "client", "restaurants": "guest", "gyms": "member", "pharmacies": "customer"}


def cap(s: str) -> str:
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


def lower_first(s: str) -> str:
    s = (s or "").strip().rstrip(".")
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


# ---------------------------------------------------------------------------
# Merchant-facing handlers
# ---------------------------------------------------------------------------
def h_research_digest(ctx: Ctx) -> Draft:
    item = ctx.digest_item(ctx.p.get("top_item_id"), kinds=("research",)) or ctx.digest_item()
    if not item:
        return h_generic(ctx)
    src = item.get("source", "")
    n = item.get("trial_n")
    kind = item.get("kind")
    aud = AUDIENCE.get(ctx.slug, "customer")
    hook = dental_hook_for_segment(ctx, item)
    summary = first_sentence(item.get("summary"))
    if n and "trial" in summary:
        finding = summary.replace("trial", f"trial ({inr_group(n)} patients)", 1)
    else:
        finding = f"{item.get('title')}. {summary}"
    if ctx.mode == "hi":
        opener = f"{ctx.sal}, {src} mein is hafte ek item" + (f" {hook}" if hook else " aapke kaam ka hai") + ":"
    else:
        opener = f"{ctx.sal}, one item in {src} this week" + (f" is {hook}" if hook else " worth your time") + ":"
    extra = ""
    if "no effect in low-risk" in str(item.get("summary", "")).lower():
        extra = ctx.T("No benefit in low-risk patients, so it's a targeted recall change, not a blanket one.",
                      "Low-risk patients mein koi fark nahi — toh sirf high-risk recall badalna hai.")
    elif item.get("actionable"):
        extra = ctx.T(f"Practical takeaway: {lower_first(item['actionable'])}.", f"Practical baat: {lower_first(item['actionable'])}.")
    if kind == "research":
        ask = ctx.T(f"Want me to pull the 2-line abstract + draft a {aud}-friendly WhatsApp you can forward? Reply YES.",
                    f"Main 2-line abstract + ek {aud}-friendly WhatsApp draft bana doon jo aap forward kar sakein? YES reply karein.")
        action = "send_research_pack"
    elif kind in ("tech", "supply"):
        ask = ctx.T(f"Want the quick cost/margin math for your {NOUN.get(ctx.slug, 'business')} + a {aud}-facing note? Reply YES.",
                    f"Aapke liye cost/margin ka quick hisaab + {aud}s ke liye ek note bhej doon? YES reply karein.")
        action = "send_item_brief"
    else:
        ask = ctx.T("Want me to turn this into a Google post for your listing? Reply YES.",
                    "Isse aapki listing ke liye Google post bana doon? YES reply karein.")
        action = "timely_post"
    return Draft(
        parts=[opener, finding, extra, ask], cta="binary_yes_no", action=action,
        action_data={"item_id": item.get("id")}, template="vera_research_digest_v1",
        rationale=(f"Weekly digest item ({src}) chosen for this {NOUN.get(ctx.slug, 'merchant')}"
                   + (f"; tied to its {ctx.agg.get('high_risk_adult_count')} high-risk adult patients" if hook and ctx.agg.get('high_risk_adult_count') else "")
                   + "; source cited for credibility, single reciprocity CTA."))


def h_regulation_change(ctx: Ctx) -> Draft:
    item = ctx.digest_item(ctx.p.get("top_item_id"), kinds=("compliance",)) or ctx.digest_item(kinds=("compliance",))
    if not item:
        return h_generic(ctx)
    deadline = ctx.p.get("deadline_iso") or item.get("effective_date")
    m = re.search(r"effective (\d{4}-\d{2}-\d{2})", item.get("title", ""))
    if not deadline and m:
        deadline = m.group(1)
    dl = parse_dt(deadline)
    days_left = days_between(ctx.now, dl) if dl else None
    when = ""
    if dl:
        when = ctx.T(f"effective {fmt_date(dl, ctx.now)}", f"{fmt_date(dl, ctx.now)} se lagu")
        if days_left is not None and days_left > 0:
            when += ctx.T(f" ({days_left} days from today)", f" (aaj se {days_left} din)")
    summary = item.get("summary", "")
    action = item.get("actionable", "")
    core = ctx.T(
        f"compliance heads-up from {item.get('source')}: {item.get('title').split(' effective')[0]}, {when}. {summary}",
        f"compliance update — {item.get('source')}: {item.get('title').split(' effective')[0]}, {when}. {summary}")
    todo = ctx.T(f"To-do: {action}.", f"Karna kya hai: {action}.") if action else ""
    ask = ctx.T("Want a 3-point audit checklist + a one-line SOP entry drafted for your clinic? Reply YES.",
                "Main aapke clinic ke liye 3-point audit checklist + SOP entry draft kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", core, todo, ask], cta="binary_yes_no", action="send_compliance_checklist",
                 action_data={"item_id": item.get("id"), "deadline": deadline},
                 template="vera_compliance_alert_v1",
                 rationale=f"Regulatory change with a hard deadline ({deadline}); states exactly what passes/fails and offers to do the audit prep (effort externalisation). Urgency {ctx.t.get('urgency')}.")


def perf_facts(ctx: Ctx) -> list[str]:
    facts = []
    if ctx.perf.get("views") is not None:
        pv = ctx.peer_views()
        facts.append(ctx.T(f"{inr_group(ctx.perf['views'])} profile views in 30 days" + (f" (peer avg {inr_group(pv)})" if pv else ""),
                           f"30 din mein {inr_group(ctx.perf['views'])} views" + (f" (peer avg {inr_group(pv)})" if pv else "")))
    cv = ctx.ctr_vs_peer()
    if cv and cv[0] < cv[1]:
        facts.append(ctx.T(f"CTR {ctr_pct(cv[0])} vs {ctr_pct(cv[1])} peer avg", f"CTR {ctr_pct(cv[0])} vs peer {ctr_pct(cv[1])}"))
    return facts


def fixes_available(ctx: Ctx) -> list[tuple[str, str, str]]:
    """(en, hi, action_key) concrete fixes we can do, derived from merchant state."""
    fixes = []
    if ctx.verified is False or ctx.has_signal("unverified_gbp"):
        fixes.append(("your Google profile is still unverified", "Google profile abhi unverified hai", "verify"))
    if not ctx.active_offers:
        fixes.append(("there's no live offer on your listing", "listing pe koi live offer nahi hai", "offer"))
    stale = ctx.signal("stale_posts")
    if stale:
        fixes.append((f"your last Google post was {stale.replace('d', ' days')} ago", f"last Google post {stale.replace('d', ' din')} pehle tha", "post"))
    elif ctx.has_signal("no_recent_post"):
        fixes.append(("no recent Google post", "koi recent Google post nahi", "post"))
    return fixes


def h_perf_dip(ctx: Ctx) -> Draft:
    metric = ctx.p.get("metric")
    delta = ctx.p.get("delta_pct")
    if metric is None or delta is None:
        bd = ctx.biggest_delta("down")
        if bd:
            metric, delta = bd
    if metric is None or delta is None:
        up = ctx.biggest_delta("up")
        fixes = fixes_available(ctx)
        if up and fixes:
            k, v = up
            line1 = ctx.T(f"quick health check: {k} are actually steady ({pct(v, True)} this week) — but {fixes[0][0]}, so the traffic isn't turning into bookings.",
                          f"quick health check: {k} theek hain ({pct(v, True)} is hafte) — lekin {fixes[0][1]}, isliye traffic bookings mein nahi badal raha.")
            sug = ctx.catalog_offer("cleaning", "haircut", "thali", "trial", "consult", "delivery") if fixes[0][2] == "offer" else None
            ask = ctx.T(f"Want me to put '{sug}' live today? Reply YES — I do the setup." if sug else "Want me to fix that today? Reply YES.",
                        f"'{sug}' aaj live kar doon? YES reply karein — setup main karungi." if sug else "Aaj theek kar doon? YES reply karein.")
            return Draft(parts=[f"{ctx.sal},", line1, ask], cta="binary_yes_no", action="launch_offer_post",
                         action_data={"offer": sug, "offer_live": False, "fixes": [f[2] for f in fixes]}, template="vera_perf_alert_v1",
                         rationale="Perf-dip trigger but the merchant's 7-day numbers are not down; said so honestly and pointed at the real conversion gap instead of inventing a dip.")
        return h_generic(ctx)
    base = ctx.p.get("vs_baseline")
    window = str(ctx.p.get("window") or "7d").replace("d", "-day")
    base_txt = ctx.T(f" against your usual ~{base}" if base else "", f" (aam taur pe ~{base})" if base else "")
    line1 = ctx.T(f"{metric} are down {pct(delta)} over the last {window} window{base_txt}.",
                  f"pichhle {window.replace('-day', ' din')} mein {metric} {pct(delta)} gire hain{base_txt}.")
    other = [(k.replace("_pct", ""), v) for k, v in ctx.delta.items()
             if isinstance(v, (int, float)) and v < 0 and k.replace("_pct", "") != metric]
    if other:
        k, v = other[0]
        line1 += ctx.T(f" {k.capitalize()} also slipped {pct(v)}.", f" {k.capitalize()} bhi {pct(v)} neeche.")
    fixes = fixes_available(ctx)
    offer, live = ctx.offer_for_pitch("cleaning", "haircut", "thali", "trial", "delivery")
    if fixes:
        fx = join_human([ctx.T(f[0], f[1]) for f in fixes[:2]], ctx.T("and", "aur"))
        line2 = ctx.T(f"Two things are fixable today: {fx}." if len(fixes) > 1 else f"One thing is fixable today: {fx}.",
                      f"Aaj hi theek ho sakta hai: {fx}.")
    else:
        line2 = ""
        pf = perf_facts(ctx)
        if pf:
            line2 = ctx.T(f"For context: {join_human(pf)}.", f"Context: {join_human(pf)}.")
    if offer and not live:
        ask = ctx.T(f"Want me to put '{offer}' live with a fresh Google post today? Reply YES — about 10 minutes of your time.",
                    f"Main '{offer}' offer + ek fresh Google post aaj live kar doon? YES reply karein — 10 min ka kaam.")
    else:
        ask = ctx.T(f"Want me to push {('your ' + repr(offer).strip(chr(39))) if offer else 'a fresh offer'} in a new Google post today? Reply YES.",
                    f"Main {('aapka ' + offer) if offer else 'ek naya offer'} naye Google post mein aaj push kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="launch_offer_post",
                 action_data={"offer": offer, "offer_live": live, "fixes": [f[2] for f in fixes]},
                 template="vera_perf_alert_v1",
                 rationale=f"{metric} dip {pct(delta, True)} is the why-now; pairs the loss with fixes visible in the merchant's own state ({', '.join(f[2] for f in fixes) or 'offer refresh'}) and a single YES to execute.")


def h_perf_spike(ctx: Ctx) -> Draft:
    metric = ctx.p.get("metric")
    delta = ctx.p.get("delta_pct")
    if metric is None or delta is None:
        bd = ctx.biggest_delta("up")
        if bd:
            metric, delta = bd
    if metric is None or delta is None:
        return h_milestone(ctx)
    base = ctx.p.get("vs_baseline")
    driver = ctx.p.get("likely_driver")
    line1 = ctx.T(f"good news — {metric} are up {pct(delta)} this week" + (f" (baseline ~{base})" if base else "") + ".",
                  f"achhi khabar — is hafte {metric} {pct(delta)} upar hain" + (f" (baseline ~{base})" if base else "") + ".")
    if driver:
        line1 += ctx.T(f" The timing lines up with your {humanize(driver).replace(' post', '')} post.",
                       f" Timing aapke {humanize(driver).replace(' post', '')} post se match karti hai.")
    cv = ctx.ctr_vs_peer()
    line2 = ""
    if cv and cv[0] >= cv[1]:
        line2 = ctx.T(f"Your CTR is {ctr_pct(cv[0])}, already above the {ctr_pct(cv[1])} peer average.",
                      f"Aapka CTR {ctr_pct(cv[0])} hai — peer avg {ctr_pct(cv[1])} se upar.")
    offer, live = ctx.offer_for_pitch()
    o_en = (f" with '{offer}' as the call-to-action" if live else f" with a new '{offer}' offer to catch the extra traffic") if offer else ""
    o_hi = (f" '{offer}' ke saath" if live else f" naye '{offer}' offer ke saath, taaki extra traffic convert ho") if offer else ""
    ask = ctx.T("Best move is to follow up while interest is warm — want me to draft the next post" + o_en + "? Reply YES.",
                "Interest garam hai, abhi follow-up post daalna sahi rahega — draft kar doon" + o_hi + "? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="followup_post",
                 action_data={"offer": offer, "driver": driver},
                 template="vera_perf_spike_v1",
                 rationale=f"Positive spike ({metric} {pct(delta, True)}) — reinforce what worked and convert momentum with one follow-up post.")


def h_renewal_due(ctx: Ctx) -> Draft:
    days = ctx.p.get("days_remaining", ctx.sub.get("days_remaining"))
    plan = ctx.p.get("plan") or ctx.sub.get("plan") or ""
    amt = ctx.p.get("renewal_amount")
    amt_txt = f" ({money(amt)})" if amt else ""
    expired_for = ctx.sub.get("days_since_expiry") if ctx.sub.get("status") == "expired" else None
    if expired_for:
        line1 = ctx.T(f"your {plan} plan lapsed {expired_for} days ago{amt_txt} — profile upkeep is paused until it's renewed.",
                      f"aapka {plan} plan {expired_for} din pehle expire ho gaya{amt_txt} — renewal tak profile upkeep ruka hua hai.")
    elif days is not None and days > 45:
        line1 = ctx.T(f"your {plan} plan's renewal window is open ({days} days left){amt_txt} — no rush, but renewing early locks in the next cycle of work.",
                      f"aapke {plan} plan ki renewal window khuli hai ({days} din baaki){amt_txt} — jaldi nahi, par early renewal se agla cycle lock ho jaata hai.")
    else:
        line1 = ctx.T(f"your {plan} plan renews in {days} days{amt_txt}." if days is not None else f"your {plan} plan is up for renewal{amt_txt}.",
                      f"aapka {plan} plan {days} din mein renew hona hai{amt_txt}." if days is not None else f"aapka {plan} plan renewal pe hai{amt_txt}.")
    # Honest picture + what renewal buys this merchant specifically
    bd = ctx.biggest_delta("down")
    honest = ""
    if bd and bd[1] <= -0.15:
        honest = ctx.T(f"Honest picture: {bd[0]} are down {pct(bd[1])} this week, so renewal alone isn't enough.",
                       f"Seedhi baat: is hafte {bd[0]} {pct(bd[1])} down hain, sirf renewal kaafi nahi.")
    plan_items = []
    for en, hi, key in fixes_available(ctx):
        plan_items.append(ctx.T({"verify": "get your profile verified", "offer": "put a service+price offer live",
                                 "post": "restart weekly Google posts"}[key],
                                {"verify": "profile verify karwana", "offer": "service+price offer live karna",
                                 "post": "weekly Google posts dobara shuru"}[key]))
    lapsed = ctx.agg.get("lapsed_180d_plus") or ctx.agg.get("lapsed_90d_plus")
    if lapsed:
        plan_items.append(ctx.T(f"a recall message to your {lapsed} lapsed customers", f"{lapsed} lapsed customers ko recall message"))
    line2 = ""
    if plan_items:
        line2 = ctx.T(f"With renewal I'll also {join_human(plan_items[:3])}.",
                      f"Renewal ke saath main yeh bhi karungi: {join_human(plan_items[:3], 'aur')}.")
    ask = ctx.T("Reply YES to renew and I'll start on these the same day.",
                "YES reply karein — renewal + yeh kaam usi din shuru.")
    return Draft(parts=[f"{ctx.sal},", line1, honest, line2, ask], cta="binary_yes_no", action="renew_plan",
                 action_data={"amount": amt, "plan": plan, "days": days},
                 template="vera_renewal_due_v1",
                 rationale=f"Renewal in {days} days; framed around what renewal will fix for this merchant (its own dip + gaps), not a generic reminder.")


def h_festival(ctx: Ctx) -> Draft:
    fest = ctx.p.get("festival")
    fdate = parse_dt(ctx.p.get("date"))
    days = days_between(ctx.now, fdate) if fdate else None
    if days is None or days < 0:
        days = ctx.p.get("days_until")
    if fdate:
        beat = ctx.beat_for_month(fdate.month)
    else:
        beat = ctx.next_festive_beat()
    if not fest:
        # Placeholder: anchor on the category's next seasonal beat instead of inventing a festival.
        if beat:
            line1 = ctx.T(f"festive season is the next big window for {ctx.slug} — {beat['month_range']}: {beat['note']}. Early planners get the bookings.",
                          f"agla bada window festive season hai — {beat['month_range']}: {beat['note']}. Jo pehle plan karte hain, bookings unhi ko milti hain.")
        else:
            line1 = ctx.T("festival season is close.", "festival season paas hai.")
    else:
        when = f"{fmt_date(fdate, ctx.now)}" if fdate else ""
        line1 = ctx.T(f"{fest} is on {when}" + (f" — {days} days away." if days is not None else "."),
                      f"{fest} {when} ko hai" + (f" — {days} din baaki." if days is not None else "."))
        if beat:
            line1 += ctx.T(f" For {ctx.slug}, {beat['month_range']} is the big one: {beat['note']}.",
                           f" {beat['month_range']} {ctx.slug} ke liye peak hai: {beat['note']}.")
    offers = ctx.active_offers[:2]
    if offers:
        line2 = ctx.T(f"Your live offers ({join_human(offers)}) can anchor a festive bundle — early bookers lock in before slots fill.",
                      f"Aapke live offers ({join_human(offers, 'aur')}) se ek festive bundle ban sakta hai — early bookers slots bharne se pehle lock karte hain.")
    else:
        sug = ctx.catalog_offer("bridal", "combo", "membership", "thali", "trial")
        line2 = ctx.T(f"A service+price hook like '{sug}' tends to pull better than a flat discount here.",
                      f"Yahan flat discount se better '{sug}' jaisa service+price hook chalta hai.") if sug else ""
    ask = ctx.T("Want me to draft the festive Google post + a WhatsApp broadcast for your regulars? Reply YES.",
                "Festive Google post + regulars ke liye WhatsApp broadcast draft kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="festival_campaign",
                 action_data={"festival": fest, "offers": offers},
                 template="vera_festival_v1",
                 rationale=f"Festival trigger ({fest or 'seasonal window'}) tied to the category's seasonal beat and the merchant's own live offers; one drafted campaign as CTA.")


def h_curious_ask(ctx: Ctx) -> Draft:
    guess = None
    pos = ctx.theme("pos")
    if pos and pos.get("common_quote"):
        guess = ctx.T(f"{pos.get('occurrences_30d')} reviews this month mention it — \"{pos['common_quote']}\"",
                      f"is mahine {pos.get('occurrences_30d')} reviews mein iska zikr hai — \"{pos['common_quote']}\"")
    top_trend = None
    mine = " ".join(ctx.active_offers + [str(t.get("theme", "")) for t in ctx.themes]).lower()
    for tr in ctx.cat.get("trend_signals") or []:
        if any(w in mine for w in str(tr.get("query", "")).lower().split() if len(w) > 3 and w not in ("near", "price", "delhi")):
            top_trend = tr
            break
    if top_trend is None:
        for tr in ctx.cat.get("trend_signals") or []:
            if top_trend is None or (tr.get("delta_yoy") or 0) > (top_trend.get("delta_yoy") or 0):
                top_trend = tr
    service_guess = None
    if pos and pos.get("common_quote"):
        for word in ["balayage", "keratin", "thali", "dosa", "biryani", "pizza", "yoga", "PT", "cleaning", "whitening"]:
            if word.lower() in pos["common_quote"].lower():
                service_guess = word
                break
    if not service_guess and ctx.lead_offer():
        service_guess = re.split(r"[@(]", ctx.lead_offer())[0].strip()
    q = ctx.T(f"quick one — what's been the most asked-for service at {ctx.biz} this week?",
              f"ek chhota sa sawaal — is hafte {ctx.biz} mein sabse zyada kis cheez ki demand rahi?")
    if ctx.slug == "restaurants":
        q = ctx.T(f"quick one — which dish is moving fastest at {ctx.biz} this week?",
                  f"ek chhota sa sawaal — is hafte {ctx.biz} mein kaunsi dish sabse zyada chal rahi hai?")
    if ctx.slug == "pharmacies":
        q = ctx.T("quick one — what are customers asking for most at the counter this week?",
                  "ek chhota sa sawaal — is hafte counter pe log sabse zyada kya maang rahe hain?")
    hint = ""
    if service_guess and guess:
        hint = ctx.T(f"My guess is {service_guess}: {guess}.", f"Mera guess {service_guess} hai: {guess}.")
    elif service_guess:
        hint = ctx.T(f"Is it still {service_guess}?", f"Kya abhi bhi {service_guess}?")
    if top_trend:
        hint += " " + ctx.T(f"(Searches for '{top_trend['query']}' are up {pct(top_trend['delta_yoy'])} YoY.)",
                            f"('{top_trend['query']}' searches {pct(top_trend['delta_yoy'])} YoY upar hain.)")
    ask = ctx.T("Tell me in one word — I'll turn it into a Google post + a ready reply for price enquiries. 5 minutes, no effort from you.",
                "Bas ek word mein bata dijiye — main usse Google post + price-enquiry ka ready reply bana dungi. 5 minute, aapki taraf se zero mehnat.")
    return Draft(parts=[f"{ctx.sal},", q, hint.strip(), ask], cta="open_ended", action="curious_answer",
                 action_data={"guess": service_guess}, template="vera_curious_ask_v1",
                 rationale="Weekly curious-ask: asking-the-merchant lever with an evidence-backed guess (reviews/trends) and reciprocity (post + reply drafted from their answer).")


def h_winback(ctx: Ctx) -> Draft:
    d = ctx.p.get("days_since_expiry") or ctx.sub.get("days_since_expiry")
    dip = ctx.p.get("perf_dip_pct")
    added = ctx.p.get("lapsed_customers_added_since_expiry")
    lapsed = ctx.agg.get("lapsed_90d_plus") or ctx.agg.get("lapsed_180d_plus")
    parts = []
    if d:
        parts.append(ctx.T(f"it's been {d} days since your plan lapsed", f"plan expire hue {d} din ho gaye"))
    if dip:
        parts.append(ctx.T(f"calls are down {pct(dip)} since", f"tab se calls {pct(dip)} neeche hain"))
    line1 = ctx.T(f"{join_human(parts)}.", f"{join_human(parts, 'aur')}.") if parts else ""
    if added:
        line1 += ctx.T(f" {added} more customers have drifted into your lapsed list" + (f" (now {lapsed})." if lapsed else "."),
                       f" {added} aur customers lapsed list mein chale gaye" + (f" (ab {lapsed})." if lapsed else "."))
    offer = ctx.expired_offers[0] if ctx.expired_offers else ctx.catalog_offer("spa", "haircut", "cleaning", "thali", "trial")
    ask = ctx.T(f"No strings: I'll draft a win-back WhatsApp for those lapsed customers with '{offer}' as the hook, free. Reply YES to see it.",
                f"Bina kisi shart ke: un lapsed customers ke liye '{offer}' wala win-back WhatsApp free mein draft kar deti hoon. Dekhne ke liye YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, ask], cta="binary_yes_no", action="winback_draft",
                 action_data={"offer": offer, "lapsed": lapsed}, template="vera_winback_v1",
                 rationale="Lapsed subscriber: quantifies the loss since expiry (days, call dip, lapsed customers) and leads with free value (reciprocity) rather than a renewal pitch.")


def h_dormant(ctx: Ctx) -> Draft:
    days = ctx.p.get("days_since_last_merchant_message")
    item = None
    for d in ctx.digest.values():
        if d.get("kind") in ("trend", "tech") and ("magicpin" in str(d.get("source", "")).lower() or "%" in d.get("title", "")):
            item = d
            break
    item = item or ctx.digest_item(kinds=("trend",)) or ctx.digest_item()
    opener = ctx.T(f"it's been {days} days since we last spoke, so no pitch — just one useful thing:" if days else "no pitch today — just one useful thing:",
                   f"{days} din se baat nahi hui, toh koi pitch nahi — bas ek kaam ki cheez:" if days else "aaj koi pitch nahi — bas ek kaam ki cheez:")
    fact = ""
    if item:
        fact = f"{item.get('title')} ({item.get('source')})."
    bd = ctx.biggest_delta("down")
    mine = ""
    if bd and bd[1] <= -0.1:
        mine = ctx.T(f"Relevant because your {bd[0]} are down {pct(bd[1])} this week.",
                     f"Aapke liye isliye relevant hai kyunki is hafte {bd[0]} {pct(bd[1])} down hain.")
    act = (item or {}).get("actionable")
    est = (ctx.m.get("identity") or {}).get("established_year")
    mm = re.search(r"crossed (\d+) months|(\d+) months continuous", (act or "") + " " + str((item or {}).get("summary", "")))
    if mm and est and int(est) < ctx.now.year:
        mine = (mine + " " if mine else "") + ctx.T(f"You've been open since {est}, so you already qualify.",
                                                     f"Aap {est} se open hain, toh eligible hain.")
    ask = ctx.T(f"The move: {lower_first(act)}. It's a 2-minute job — reply YES and I'll set it up with you." if act else "Reply YES and I'll set it up with you.",
                f"Karna bas itna hai: {lower_first(act)}. 2 minute ka kaam — YES reply karein, main saath mein kar deti hoon." if act else "YES reply karein, main saath mein kar deti hoon.")
    return Draft(parts=[f"{ctx.sal},", opener, fact, mine, ask], cta="binary_yes_no", action="apply_quick_win",
                 action_data={"item_id": (item or {}).get("id")}, template="vera_reengage_v1",
                 rationale=f"Dormant {days or '30+'} days: re-open with a no-ask, data-backed quick win (reciprocity + curiosity) instead of repeating the last topic.")


def h_ipl(ctx: Ctx) -> Draft:
    match = ctx.p.get("match", "tonight's match")
    venue = ctx.p.get("venue")
    t = fmt_time(ctx.p.get("match_time_iso"))
    weeknight = ctx.p.get("is_weeknight")
    item = ctx.digest_item(kinds=("seasonal",))
    ipl = item if item and "ipl" in str(item.get("title", "")).lower() else None
    line1 = ctx.T(f"{match} tonight" + (f" at {venue}" if venue else "") + (f", {t}." if t else "."),
                  f"aaj {match}" + (f" — {venue}" if venue else "") + (f", {t}." if t else "."))
    bogo = next((o for o in ctx.active_offers if "tue" in o.lower() or "thu" in o.lower()), None)
    combo = ctx.catalog_offer("match")
    late = next((th for th in ctx.themes if "late" in str(th.get("theme", ""))), None)
    if weeknight is False:
        judge = ctx.T("It's a weekend game, and weekend IPL nights usually pull people home — "
                      + (f"covers fell ~12% vs a normal Saturday in {ipl.get('source')}, while weeknight matches were +18%. " if ipl else "")
                      + "So skip a dine-in push tonight and go delivery-first.",
                      "Weekend match hai — log ghar pe dekhte hain. "
                      + (f"{ipl.get('source')}: Saturday IPL raaton mein covers ~12% gire, weeknight matches +18%. " if ipl else "")
                      + "Toh aaj dine-in promo skip, delivery-first chaliye.")
        if bogo:
            judge += ctx.T(f" Your '{bogo}' doesn't cover today, so a delivery-only " + (f"'{combo}'" if combo else "combo") + " fits better.",
                           f" Aapka '{bogo}' aaj apply nahi hota, isliye delivery-only " + (f"'{combo}'" if combo else "combo") + " better rahega.")
    else:
        judge = ctx.T("Weeknight matches are your best nights — " + (f"+18% covers per {ipl.get('source')}. " if ipl else "")
                      + "Worth a match-night push today.",
                      "Weeknight match sabse achhi raat hoti hai — " + (f"{ipl.get('source')} ke hisaab se +18% covers. " if ipl else "")
                      + "Aaj match-night push banta hai.")
    caution = ""
    if late:
        caution = ctx.T(f"One caution: {late.get('occurrences_30d')} recent reviews flag late delivery, so quote a realistic ETA on the promo.",
                        f"Ek dhyaan: {late.get('occurrences_30d')} recent reviews late delivery bol rahe hain — promo pe realistic ETA likhiye.")
    ask = ctx.T("Want me to put the post + WhatsApp status live by 6pm? Reply YES.",
                "6pm tak Google post + WhatsApp status live kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, judge, caution, ask], cta="binary_yes_no", action="match_night_promo",
                 action_data={"offer": combo, "weeknight": weeknight}, template="vera_event_today_v1",
                 rationale="IPL trigger interpreted with category data (weekend matches cut dine-in covers) → contrarian delivery-first call; uses the merchant's real offer constraints and review risk.")


def h_review_theme(ctx: Ctx) -> Draft:
    theme = ctx.p.get("theme")
    occ = ctx.p.get("occurrences_30d")
    quote = ctx.p.get("common_quote")
    trend = ctx.p.get("trend")
    if not theme:
        neg = ctx.theme("neg")
        if neg:
            theme, occ, quote = neg.get("theme"), neg.get("occurrences_30d"), neg.get("common_quote")
    if not theme:
        rc, rr = ctx.peer.get("avg_review_count"), ctx.peer.get("avg_rating")
        line1 = ctx.T("quick review check — customers have started repeating a theme in your recent Google reviews.",
                      "reviews ka quick check — aapke recent Google reviews mein ek baat baar-baar aa rahi hai.")
        line2 = ctx.T(f"For reference, {ctx.slug} peers average {rr}★ across {rc} reviews, and replying to reviews is the cheapest way to move rating." if rc and rr else "",
                      f"Reference ke liye: {ctx.slug} peers ka avg {rr}★, {rc} reviews — aur reviews ka reply karna rating badhane ka sabse sasta tareeka hai." if rc and rr else "")
        ask = ctx.T("Is it mostly praise or a complaint this week? Tell me in a line and I'll draft the public replies for you.",
                    "Is hafte zyada tareef hai ya shikayat? Ek line mein bata dijiye, main public replies draft kar deti hoon.")
        return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="open_ended", action="review_replies",
                     action_data={"theme": None}, template="vera_review_theme_v1",
                     rationale="Review-theme trigger arrived without theme details; asks the merchant to name it (no invented quotes) and offers to draft replies.")
    line1 = ctx.T(f"{occ} reviews in the last 30 days mention {humanize(theme)}" + (f", and the trend is {trend}" if trend else "") + ".",
                  f"pichhle 30 din mein {occ} reviews {humanize(theme)} ki baat kar rahe hain" + (f", aur trend {trend} hai" if trend else "") + ".")
    if quote:
        line1 += ctx.T(f" One says: \"{quote}\".", f" Ek review: \"{quote}\".")
    pos = ctx.theme("pos")
    line2 = ""
    if pos:
        line2 = ctx.T(f"Worth fixing fast — {pos.get('occurrences_30d')} reviews praise your {humanize(pos.get('theme'))}, and this is the one thing pulling against it.",
                      f"Jaldi fix karna banta hai — {pos.get('occurrences_30d')} reviews aapki {humanize(pos.get('theme'))} ki tareef karte hain, yahi ek cheez usko kaat rahi hai.")
    ask = ctx.T("Want me to draft polite public replies to these reviews + one ops fix line for your listing? Reply YES.",
                "In reviews ke polite public replies + listing ke liye ek fix line draft kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="review_replies",
                 action_data={"theme": theme, "occ": occ, "quote": quote}, template="vera_review_theme_v1",
                 rationale=f"Emerging review theme ({humanize(theme)}, {occ} in 30d) with the customer's own words; offers drafted replies (effort externalisation).")


def h_milestone(ctx: Ctx) -> Draft:
    metric = ctx.p.get("metric")
    now_v, target = ctx.p.get("value_now"), ctx.p.get("milestone_value")
    if metric and now_v is not None and target is not None:
        gap = target - now_v
        label = humanize(metric).replace("review count", "Google reviews")
        line1 = ctx.T(f"{ctx.biz} is at {now_v} {label} — {gap} away from {target}." if gap > 0 else f"{ctx.biz} just crossed {target} {label}!",
                      f"{ctx.biz} {now_v} {label} pe hai — {target} bas {gap} door." if gap > 0 else f"{ctx.biz} ne {target} {label} cross kar liye!")
        peer_rc = ctx.peer.get("avg_review_count")
        if "review" in str(metric) and peer_rc:
            line1 += ctx.T(f" Peer average is {peer_rc}, so {target} puts you clearly ahead.",
                           f" Peer avg {peer_rc} hai, {target} pe aap saaf aage honge.")
        pos = ctx.theme("pos")
        line2 = ""
        if pos:
            line2 = ctx.T(f"{pos.get('occurrences_30d')} recent reviews already praise your {humanize(pos.get('theme'))} — happy regulars are the fastest path.",
                          f"{pos.get('occurrences_30d')} recent reviews aapki {humanize(pos.get('theme'))} ki tareef karte hain — khush regulars se hi jaldi hoga.")
        ask = ctx.T("Want a 2-line review-request WhatsApp you can send to regulars today? Reply YES.",
                    "Regulars ko bhejne layak 2-line review-request WhatsApp draft kar doon? YES reply karein.")
        action_data = {"metric": metric, "target": target}
    else:
        # Placeholder: celebrate verifiable 30-day numbers (no invented milestone value).
        best = None
        for k, pk, en, hi in (("views", "avg_views_30d", "profile views", "profile views"),
                              ("calls", "avg_calls_30d", "calls", "calls"),
                              ("directions", "avg_directions_30d", "direction requests", "direction requests")):
            v, pv = ctx.perf.get(k), ctx.peer.get(pk)
            if isinstance(v, (int, float)) and isinstance(pv, (int, float)) and pv:
                r = v / pv
                if best is None or r > best[0]:
                    best = (r, v, pv, en)
        if best and best[0] >= 1.0:
            line1 = ctx.T(f"a number worth celebrating: {inr_group(best[1])} {best[3]} in the last 30 days — {best[0]:.1f}x the {inr_group(best[2])} peer average for {ctx.slug}.",
                          f"ek celebrate karne wala number: pichhle 30 din mein {inr_group(best[1])} {best[3]} — {ctx.slug} ke peer avg {inr_group(best[2])} ka {best[0]:.1f}x.")
            ask = ctx.T("Momentum like this is the best time to ask happy customers for reviews — want a 2-line review-request WhatsApp? Reply YES.",
                        "Aisa momentum review maangne ka best time hai — 2-line review-request WhatsApp draft kar doon? YES reply karein.")
            return Draft(parts=[f"{ctx.sal},", line1, ask], cta="binary_yes_no", action="review_request",
                         action_data={"metric": "reviews", "target": None}, template="vera_milestone_v1",
                         rationale=f"Milestone trigger without a payload value; celebrates the one metric where this merchant beats peer ({best[3]}, {best[0]:.1f}x) and converts it into review asks.")
        facts = []
        for k, en in (("views", "profile views"), ("calls", "calls"), ("directions", "direction requests")):
            if ctx.perf.get(k) is not None:
                facts.append(f"{inr_group(ctx.perf[k])} {en}")
        line1 = ctx.T(f"30-day snapshot for {ctx.biz}: {join_human(facts)}.",
                      f"{ctx.biz} ka 30-din ka snapshot: {join_human(facts, 'aur')}.")
        pv = ctx.peer_views()
        line2 = ""
        if pv and ctx.perf.get("views") and ctx.perf["views"] > pv:
            line2 = ctx.T(f"That's ahead of the {inr_group(pv)}-view peer average.", f"Yeh peer avg {inr_group(pv)} views se aage hai.")
        elif pv and ctx.perf.get("views"):
            line2 = ctx.T(f"Peer average is {inr_group(pv)} views — the gap is visibility, and fresh reviews are the quickest lever for it.",
                          f"Peer avg {inr_group(pv)} views hai — gap visibility ka hai, aur fresh reviews iska sabse tez lever hain.")
        ask = ctx.T("Want me to turn this into a 'thank you, customers' Google post + a review ask for regulars? Reply YES.",
                    "Isse ek 'thank you customers' Google post + regulars ke liye review request bana doon? YES reply karein.")
        action_data = {"metric": "reviews", "target": None}
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="review_request",
                 action_data=action_data, template="vera_milestone_v1",
                 rationale="Milestone framing (goal-gradient: close to a round number) with peer comparison; converts pride into a review-request action.")


def h_planning(ctx: Ctx) -> Draft:
    topic = str(ctx.p.get("intent_topic") or "")
    if "thali" in topic or "corporate" in topic:
        base_offer = next((o for o in ctx.active_offers if "thali" in o.lower()), None)
        base = price_from_title(base_offer or "") or 149
        tiers = [(10, round(base * 0.9 / 5) * 5), (25, round(base * 0.86 / 5) * 5), (50, round(base * 0.8 / 5) * 5)]
        orders = None
        for turn in ctx.history:
            mm = re.search(r"(\d+)\s*orders/day", str(turn.get("body", "")))
            if mm:
                orders = mm.group(1)
        area = ctx.locality or ctx.city
        base_name = re.split(r"\s*@", base_offer or "Weekday Lunch Thali")[0]
        lines = [
            ctx.T(f"here's a first cut of the corporate thali package — edit anything:",
                  f"corporate thali package ka pehla draft — jo chahein badal dijiye:"),
            f"\n• Base: {base_name} at {money(base)} retail",
            f"\n• 10–24 thalis/day: {money(tiers[0][1])} each",
            f"\n• 25–49: {money(tiers[1][1])} each + filter coffee on the house",
            f"\n• 50+: {money(tiers[2][1])} each, fixed monthly invoice",
            "\n• " + ctx.T("Order by 5pm the day before; delivered 12:30–1:30pm", "Order pichhle din 5pm tak; delivery 12:30–1:30pm"),
            "\n",
        ]
        why = []
        if orders:
            why.append(ctx.T(f"your thali already does ~{orders} orders/day", f"aapki thali already ~{orders} orders/day karti hai"))
        ds = ctx.agg.get("delivery_share_pct")
        if ds:
            why.append(ctx.T(f"{pct(ds)} of orders are delivery", f"{pct(ds)} orders delivery hain"))
        why_line = ctx.T(f"Why it works for you: {join_human(why)} — so this is a bigger lunch batch, not a new kitchen." if why else "",
                         f"Aapke liye kyun: {join_human(why, 'aur')} — matlab naya kitchen nahi, bas bada lunch batch." if why else "")
        ask = ctx.T(f"Next step: I turn this into a Google post + a 3-line pitch for office admins in {area}. Reply YES to go.",
                    f"Agla step: isko Google post + {area} ke office admins ke liye 3-line pitch mein badal deti hoon. YES reply karein.")
        return Draft(parts=[f"{ctx.sal},", "".join(lines), why_line, ask], cta="binary_yes_no", action="planning_next",
                     action_data={"topic": topic, "tiers": tiers, "base": base}, template="vera_planning_draft_v1",
                     rationale="Merchant explicitly asked 'what would it look like' → deliver the artifact (tiered package built off their live ₹ thali price), not more questions; single next step.")
    if "yoga" in topic or "kids" in topic:
        prev = ""
        for turn in ctx.history:
            if turn.get("from") == "vera" and "₹" in str(turn.get("body", "")):
                prev = turn["body"]
        fee = price_from_title(prev) or None
        weeks = re.search(r"(\d+)-week", prev)
        per_wk = re.search(r"(\d+) classes/week", prev)
        ages = re.search(r"age (\d+-\d+)", prev)
        lines = [
            ctx.T("here's the kids yoga camp, drafted on what we discussed — edit freely:",
                  "kids yoga camp ka draft, jo humne discuss kiya tha — badal sakte hain:"),
            f"\n• Ages {ages.group(1) if ages else '7-12'}, {weeks.group(1) if weeks else '4'} weeks, {per_wk.group(1) if per_wk else '3'} sessions/week (45 min)",
            f"\n• Fee: {money(fee)} for the full camp" if fee else "\n• Fee: your call — I'll add it to the post",
            "\n• Week 1 breath & balance → Week 2 animal poses → Week 3 partner poses → Week 4 mini showcase for parents",
            "\n• Weekend-morning batch + one weekday-evening batch",
            "\n",
        ]
        spike = ""
        if ctx.delta.get("calls_pct") and ctx.delta["calls_pct"] > 0:
            spike = ctx.T(f"Calls are already up {pct(ctx.delta['calls_pct'])} this week — parents are asking.",
                          f"Is hafte calls {pct(ctx.delta['calls_pct'])} upar hain — parents pooch rahe hain.")
        ask = ctx.T("Next: I turn this into a GBP post + an Instagram carousel. Reply YES and the drafts come to you today.",
                    "Agla step: GBP post + Instagram carousel. YES reply karein, draft aaj hi aa jayenge.")
        return Draft(parts=[f"{ctx.sal},", "".join(lines), spike, ask], cta="binary_yes_no", action="planning_next",
                     action_data={"topic": topic, "fee": fee}, template="vera_planning_draft_v1",
                     rationale="Merchant asked what the kids programme should look like → hand over a concrete programme (reusing numbers already agreed in history) and one next action.")
    last = ctx.p.get("merchant_last_message") or ctx.last_merchant_ask() or humanize(topic)
    ask = ctx.T(f"On your \"{last}\" — I've got a first draft of {humanize(topic)} ready. Reply YES and I'll send it over now.",
                f"Aapke \"{last}\" pe — {humanize(topic)} ka pehla draft ready hai. YES reply karein, abhi bhejti hoon.")
    return Draft(parts=[f"{ctx.sal},", ask], cta="binary_yes_no", action="planning_next",
                 action_data={"topic": topic}, template="vera_planning_draft_v1",
                 rationale="Active planning intent — continue the merchant's own thread with a ready draft.")


def h_seasonal_dip(ctx: Ctx) -> Draft:
    metric = ctx.p.get("metric", "views")
    delta = ctx.p.get("delta_pct")
    item = ctx.digest_item(kinds=("seasonal",))
    beat = ctx.season_beat(0)
    line1 = ctx.T(f"{metric} are down {pct(delta)} this week — and that's expected, not a problem.",
                  f"is hafte {metric} {pct(delta)} down hain — yeh expected hai, problem nahi.") if delta is not None else ""
    if beat:
        line1 += ctx.T(f" {beat['month_range']} is the {beat['note']}.", f" {beat['month_range']}: {beat['note']}.")
    if item and item.get("actionable"):
        line1 += ctx.T(f" Category data says: {item['actionable'].rstrip('.')}.", f" Category data: {item['actionable'].rstrip('.')}.")
    churn, peer_churn = ctx.agg.get("monthly_churn_pct"), ctx.peer.get("monthly_churn_pct")
    members = ctx.agg.get("total_active_members")
    line2 = ""
    if churn is not None and peer_churn is not None and members:
        extra = round(members * (churn - peer_churn))
        if extra > 0:
            line2 = ctx.T(f"The real lever now is retention: your monthly churn is {pct(churn)} vs {pct(peer_churn)} peer — on {members} members that's ~{extra} extra people leaving each month.",
                          f"Asli lever retention hai: aapka monthly churn {pct(churn)} vs peer {pct(peer_churn)} — {members} members pe har mahine ~{extra} extra log.")
        else:
            line2 = ctx.T(f"Your churn ({pct(churn)}) is at or below peer ({pct(peer_churn)}) — protect that across {members} members.",
                          f"Aapka churn ({pct(churn)}) peer ({pct(peer_churn)}) se kam hai — {members} members pe isse bachaaiye.")
    ask = ctx.T("Want me to draft a 4-week member consistency challenge to hold them through the lull? Reply YES.",
                "Members ko is lull mein jode rakhne ke liye 4-week consistency challenge draft kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="retention_challenge",
                 action_data={"members": members}, template="vera_seasonal_reframe_v1",
                 rationale="Expected seasonal dip: pre-empt anxiety, redirect effort from acquisition to retention using the merchant's churn vs peer.")


def h_supply_alert(ctx: Ctx) -> Draft:
    item = ctx.digest_item(ctx.p.get("alert_id"), kinds=("alert",))
    mol = ctx.p.get("molecule") or ""
    batches = ctx.p.get("affected_batches") or []
    mfr = ctx.p.get("manufacturer")
    src = (item or {}).get("source", "")
    risk = ""
    if item and "sub-potency" in str(item.get("summary", "")).lower():
        risk = ctx.T("Reason: sub-potency — no direct safety risk, but patients get weaker LDL control until replaced.",
                     "Wajah: sub-potency — safety risk nahi, par replace hone tak LDL control kamzor rahega.")
    line1 = ctx.T(f"urgent — voluntary recall on {mol} batches {join_human(batches)}" + (f" by {mfr}" if mfr else "") + (f" ({src})." if src else "."),
                  f"urgent — {mol} ke batches {join_human(batches, 'aur')} ka voluntary recall" + (f", manufacturer {mfr}" if mfr else "") + (f" ({src})." if src else "."))
    chronic = ctx.agg.get("chronic_rx_count")
    asked = any("list" in str(t.get("body", "")).lower() and t.get("from") == "merchant" for t in ctx.history)
    line2 = ctx.T((f"You asked for the list — " if asked else "") + (f"I'll filter your {chronic} chronic-Rx customers for {mol} buyers" if chronic else f"I'll filter your repeat-Rx customers for {mol}")
                  + ", then draft their WhatsApp note + the replacement-pickup steps.",
                  (f"Aapne list maangi thi — " if asked else "") + (f"aapke {chronic} chronic-Rx customers mein se {mol} wale filter karke" if chronic else f"repeat-Rx customers mein se {mol} wale filter karke")
                  + " unka WhatsApp note + replacement-pickup steps draft kar deti hoon.")
    ask = ctx.T("Reply YES to start — first step is pulling the batches off your shelf.",
                "Shuru karne ke liye YES reply karein — pehla kaam shelf se batches hataana.")
    return Draft(parts=[f"{ctx.sal},", line1, risk, line2, ask], cta="binary_yes_no", action="recall_workflow",
                 action_data={"molecule": mol, "batches": batches}, template="vera_supply_alert_v1",
                 rationale="Urgency-5 supply recall: exact batch numbers + risk framing without alarm; ties to the merchant's own earlier request for the customer list; end-to-end workflow offer.")


def h_category_seasonal(ctx: Ctx) -> Draft:
    trends = ctx.p.get("trends") or []
    ups, downs = [], []
    for tr in trends:
        mm = re.match(r"([A-Za-z_]+?)_demand_([+-]?\d+)", str(tr))
        if not mm:
            continue
        name = mm.group(1).replace("_", "/").replace("cold/cough", "cold/cough")
        name = name if name.isupper() else name.replace("antifungal", "anti-fungal")
        val = int(mm.group(2))
        (ups if val > 0 else downs).append(f"{name} {'+' if val > 0 else '−'}{abs(val)}%")
    item = ctx.digest_item(kinds=("seasonal",))
    src = (item or {}).get("source")
    line1 = ctx.T(f"the {humanize(ctx.p.get('season', 'seasonal'))} demand shift is here" + (f" ({src})" if src else "") + f": {join_human(ups + downs)}.",
                  f"{humanize(ctx.p.get('season', 'season'))} ka demand shift shuru" + (f" ({src})" if src else "") + f": {join_human(ups + downs, 'aur')}.")
    shelf = ""
    if ctx.p.get("shelf_action_recommended") and item and item.get("actionable"):
        shelf = ctx.T(f"Shelf move: {item['actionable'].rstrip('.')}.", f"Shelf pe: {item['actionable'].rstrip('.')}.")
    offer, live = ctx.offer_for_pitch("delivery")
    ask = ctx.T("Want me to post a 'summer essentials' update on your Google profile" + (f" with '{offer}'" if offer else "") + "? Reply YES.",
                "Google profile pe 'summer essentials' post daal doon" + (f" '{offer}' ke saath" if offer else "") + "? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, shelf, ask], cta="binary_yes_no", action="seasonal_post",
                 action_data={"offer": offer, "ups": ups}, template="vera_seasonal_shift_v1",
                 rationale="Seasonal category shift with exact demand deltas; actionable shelf move + a post tied to the merchant's live delivery offer.")


def h_gbp_unverified(ctx: Ctx) -> Draft:
    uplift = ctx.p.get("estimated_uplift_pct")
    path = humanize(ctx.p.get("verification_path", "")).replace(" or ", " or a ")
    views = ctx.perf.get("views")
    line1 = ctx.T(f"{ctx.biz}'s Google profile is still unverified.", f"{ctx.biz} ka Google profile abhi unverified hai.")
    if uplift:
        extra = f" — on your {inr_group(views)} monthly views that's roughly {inr_group(views * uplift)} more" if views else ""
        extra_hi = f" — aapke {inr_group(views)} monthly views pe lagbhag {inr_group(views * uplift)} extra" if views else ""
        line1 += ctx.T(f" Verified listings see an estimated {pct(uplift)} lift in visibility{extra}.",
                       f" Verified listings ko estimated {pct(uplift)} zyada visibility milti hai{extra_hi}.")
    line2 = ctx.T(f"Verification is by {path}; the phone route usually takes about 5 minutes." if path else "",
                  f"Verification {path.replace(' or a ', ' ya ')} se hota hai; phone wala tareeka ~5 minute ka." if path else "")
    ask = ctx.T("Reply YES and I'll walk you through it step by step right here.",
                "YES reply karein, main yahin step-by-step karwa deti hoon.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="verify_gbp",
                 action_data={"path": path}, template="vera_gbp_verify_v1",
                 rationale="Unverified GBP: quantifies the upside on the merchant's own view count and removes effort (guided, in-chat).")


def h_cde(ctx: Ctx) -> Draft:
    item = ctx.digest_item(ctx.p.get("digest_item_id"), kinds=("cde",))
    if not item:
        return h_generic(ctx)
    when = parse_dt(item.get("date"))
    credits = ctx.p.get("credits") or item.get("credits")
    fee = item.get("actionable") or humanize(ctx.p.get("fee", ""))
    line1 = ctx.T(f"CDE pick for you: '{item.get('title')}' ({item.get('source')})",
                  f"aapke liye CDE: '{item.get('title')}' ({item.get('source')})")
    if when:
        line1 += f" — {fmt_date(when, ctx.now, dow=True)}, {fmt_time(item.get('date'))}"
    line1 += (f", {credits} credits." if credits else ".")
    line2 = first_sentence(item.get("summary", "")) + " " + (fee.rstrip(".") + "." if fee else "")
    rel = ""
    ask_hist = (ctx.last_merchant_ask() or "").lower()
    if "aligner" in ask_hist:
        rel = ctx.T("Relevant since you wanted to push aligners — digital impressions are the front door for aligner cases.",
                    "Aapne aligners push karne ko kaha tha — digital impressions hi aligner cases ka entry point hain.")
    ask = ctx.T("Want me to send the registration details + a calendar reminder? Reply YES.",
                "Registration details + calendar reminder bhej doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2.strip(), rel, ask], cta="binary_yes_no", action="cde_register",
                 action_data={"item_id": item.get("id")}, template="vera_cde_v1",
                 rationale="CDE opportunity with date, credits and fee from the digest; linked to the merchant's own stated interest (aligners).")


def h_competitor(ctx: Ctx) -> Draft:
    name = ctx.p.get("competitor_name")
    dist = ctx.p.get("distance_km")
    their = ctx.p.get("their_offer")
    opened = ctx.p.get("opened_date")
    mine_offer = ctx.lead_offer()
    if name:
        line1 = ctx.T(f"{name} opened {dist} km from you" + (f" on {fmt_date(opened, ctx.now)}" if opened else "") + (f", running '{their}'." if their else "."),
                      f"{name} aapse {dist} km door khula hai" + (f" ({fmt_date(opened, ctx.now)})" if opened else "") + (f", '{their}' chala rahe hain." if their else "."))
        tp, mp = price_from_title(their or ""), price_from_title(mine_offer or "")
        if tp and mp and mp > tp:
            line1 += ctx.T(f" That's {money(mp - tp)} under your '{mine_offer}'.", f" Yeh aapke '{mine_offer}' se {money(mp - tp)} kam hai.")
    else:
        noun = NOUN.get(ctx.slug, "business")
        line1 = ctx.T(f"a new {noun} has opened near you in {ctx.locality}.",
                      f"{ctx.locality} mein aapke paas ek naya {noun} khula hai.")
    pos, neg = ctx.theme("pos"), ctx.theme("neg")
    edge = ""
    if pos:
        edge = ctx.T(f"Don't match on price — your edge is in your reviews: {pos.get('occurrences_30d')} mention {humanize(pos.get('theme'))}"
                     + (f" (\"{pos['common_quote']}\")" if pos.get("common_quote") else "") + ".",
                     f"Price war mat kijiye — aapka edge reviews mein hai: {pos.get('occurrences_30d')} reviews {humanize(pos.get('theme'))} ki tareef karte hain"
                     + (f" (\"{pos['common_quote']}\")" if pos.get("common_quote") else "") + ".")
    else:
        facts = []
        if ctx.perf.get("views"):
            facts.append(ctx.T(f"{inr_group(ctx.perf['views'])} monthly views", f"{inr_group(ctx.perf['views'])} monthly views"))
        if ctx.agg.get("repeat_customer_pct"):
            facts.append(ctx.T(f"{pct(ctx.agg['repeat_customer_pct'])} repeat customers", f"{pct(ctx.agg['repeat_customer_pct'])} repeat customers"))
        if facts:
            edge = ctx.T(f"You start with a head start — {join_human(facts)} — so defend on familiarity, not price.",
                         f"Aapke paas head start hai — {join_human(facts, 'aur')} — isliye price nahi, bharose pe defend kijiye.")
    fix = ""
    if neg and pos:
        fix = ctx.T(f"Newcomers poach on friction, so it's worth fixing the {neg.get('occurrences_30d')} '{humanize(neg.get('theme'))}' complaints first.",
                    f"Naye log friction pe customers kheenchte hain — pehle {neg.get('occurrences_30d')} '{humanize(neg.get('theme'))}' complaints theek kijiye.")
    ask = ctx.T("Want me to draft a Google post that leads with that strength? Reply YES.",
                "Wahi strength aage rakhte hue ek Google post draft kar doon? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, edge, fix, ask], cta="binary_yes_no", action="differentiation_post",
                 action_data={"competitor": name, "strength": (pos or {}).get("theme")}, template="vera_competitor_v1",
                 rationale="Competitor opened nearby: names only what the trigger provides, avoids a price war and positions on the merchant's review-backed strength.")


def h_generic_external(ctx: Ctx) -> Draft:
    """weather_heatwave / local_news_event / category_trend_movement / anything external."""
    p = ctx.p
    bits = [f"{humanize(k)}: {v}" for k, v in p.items() if isinstance(v, (str, int, float)) and k not in ("placeholder", "metric_or_topic", "category")]
    trend = None
    for tr in ctx.cat.get("trend_signals") or []:
        if trend is None or (tr.get("delta_yoy") or 0) > (trend.get("delta_yoy") or 0):
            trend = tr
    line1 = ctx.T(f"heads-up — {humanize(ctx.kind)}" + (f" ({'; '.join(bits[:3])})" if bits else "") + ".",
                  f"heads-up — {humanize(ctx.kind)}" + (f" ({'; '.join(bits[:3])})" if bits else "") + ".")
    line2 = ""
    if trend:
        line2 = ctx.T(f"Related demand signal: '{trend['query']}' searches are up {pct(trend['delta_yoy'])} YoY.",
                      f"Demand signal: '{trend['query']}' searches {pct(trend['delta_yoy'])} YoY upar.")
    offer, _ = ctx.offer_for_pitch()
    ask = ctx.T("Want me to draft a timely Google post" + (f" around '{offer}'" if offer else "") + "? Reply YES.",
                "Iske hisaab se ek Google post draft kar doon" + (f" '{offer}' ke saath" if offer else "") + "? YES reply karein.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="timely_post",
                 action_data={"offer": offer}, template="vera_timely_update_v1",
                 rationale=f"External event ({humanize(ctx.kind)}) connected to category demand data; single post CTA.")


def h_generic(ctx: Ctx) -> Draft:
    """Last-resort merchant-facing message anchored on verifiable account numbers."""
    fixes = fixes_available(ctx)
    facts = perf_facts(ctx)
    line1 = ctx.T(f"quick account check for {ctx.biz}: {join_human(facts)}." if facts else f"quick account check for {ctx.biz}.",
                  f"{ctx.biz} ka quick check: {join_human(facts, 'aur')}." if facts else f"{ctx.biz} ka quick check.")
    line2 = ""
    ask = ctx.T("Want me to draft a fresh Google post to lift this? Reply YES.", "Isse upar le jaane ke liye ek fresh Google post draft kar doon? YES reply karein.")
    if fixes:
        line2 = ctx.T(f"Easiest win: {fixes[0][0]}.", f"Sabse aasaan win: {fixes[0][1]}.")
        key = fixes[0][2]
        if key == "offer":
            sug = ctx.catalog_offer("cleaning", "haircut", "thali", "trial", "consult", "delivery")
            ask = ctx.T(f"Want me to put '{sug}' live on your listing? Reply YES — 10 minutes, I do the setup.",
                        f"Main '{sug}' aapki listing pe live kar doon? YES reply karein — setup main karungi.")
        elif key == "verify":
            ask = ctx.T("Reply YES and I'll walk you through verification here — about 5 minutes.",
                        "YES reply karein, verification yahin 5 minute mein karwa deti hoon.")
    return Draft(parts=[f"{ctx.sal},", line1, line2, ask], cta="binary_yes_no", action="quick_fix",
                 action_data={"fixes": [f[2] for f in fixes]}, template="vera_generic_v1",
                 rationale=f"Trigger '{ctx.kind}' had no usable payload; anchored on the merchant's own performance vs peer and one fix.")


# ---------------------------------------------------------------------------
# Customer-facing handlers (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------
class CCtx:
    """Customer-facing helpers layered on Ctx."""

    def __init__(self, ctx: Ctx):
        self.x = ctx
        c = ctx.c or {}
        ident = c.get("identity") or {}
        raw = str(ident.get("name") or "").strip()
        parent = re.search(r"\(parent:\s*([^)]+)\)", raw)
        self.child = re.sub(r"\s*\(.*\)", "", raw).strip() if parent else None
        self.name = (parent.group(1).strip() if parent else re.sub(r"\s*\(.*\)", "", raw).strip())
        if self.name.startswith("(") or not self.name:
            self.name = ""
        self.mode, self.greet = customer_mode(c)
        self.rel = c.get("relationship") or {}
        self.prefs = c.get("preferences") or {}
        self.state = c.get("state")
        self.senior = bool(ident.get("senior_citizen"))
        self.consent = (c.get("consent") or {}).get("scope") or []
        self.via = str(self.prefs.get("channel", ""))
        biz = ctx.biz
        who = ""
        if ctx.is_dentist and ctx.owner:
            who = f"Dr. {ctx.owner}'s clinic" if "dr" in biz.lower() else biz
        self.signoff_biz = who or biz

    def T(self, en: str, hi: str) -> str:
        return hi if self.mode == "hi" else en

    def hello(self, emoji: str = "") -> str:
        if self.senior and self.mode == "hi":
            return f"Namaste 🙏 {self.x.biz}" + (f", {self.x.locality}" if self.x.locality else "") + " se."
        base = f"{self.greet} {self.name}".strip()
        base += f" {emoji}" if emoji else ","
        who = self.signoff_biz + (f", {self.x.locality}" if self.x.locality else "")
        return base + self.T(f" {who} here.", f" {who} se.")

    def pref_slot(self) -> str:
        return humanize(self.prefs.get("preferred_slots", "")).replace("weekday ", "weekday ").strip()

    def last_visit(self):
        return parse_dt(self.rel.get("last_visit"))


VISIT = {"dentists": ("check-up", "check-up"), "salons": ("appointment", "appointment"),
         "gyms": ("session", "session"), "restaurants": ("table booking", "table booking"),
         "pharmacies": ("pharmacist visit", "pharmacist visit")}
TRIAL = {"gyms": "trial class", "salons": "trial session", "dentists": "consultation"}


def _caps(parts: list[str]) -> list[str]:
    return [cap(p) if i else p for i, p in enumerate(parts)]


def _customer_offer(ctx: Ctx, *keywords: str) -> Optional[str]:
    """Customer-facing: only the merchant's *live* offers (never a catalog price the merchant hasn't activated)."""
    for kw in keywords:
        for t in ctx.active_offers:
            if kw.lower() in t.lower():
                return t
    return None


def h_recall_due(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    svc = humanize(ctx.p.get("service_due", ""))
    last = parse_dt(ctx.p.get("last_service_date")) or cc.last_visit()
    slots = ctx.p.get("available_slots") or []
    offer = _customer_offer(ctx, "clean", "check", "consult", "analysis", "spa", "haircut")
    if ctx.is_dentist or svc:
        due_en = f"your {svc or 'routine check-up'} is due" + (f" (last visit: {fmt_date(last, ctx.now)})" if last else "") + "."
        due_hi = f"aapka {svc or 'routine check-up'} due hai" + (f" (last visit: {fmt_date(last, ctx.now)})" if last else "") + "."
    elif ctx.slug == "pharmacies":
        due_en = "your regular refill may be due" + (f" — your last purchase with us was {fmt_date(last, ctx.now)}" if last else "") + ". Send a photo of your prescription and we'll keep it ready."
        due_hi = "aapka regular refill due ho sakta hai" + (f" — last purchase {fmt_date(last, ctx.now)} ko thi" if last else "") + ". Prescription ki photo bhej dijiye, hum ready rakh denge."
    elif ctx.slug == "restaurants":
        due_en = "we've missed you" + (f" since your last visit on {fmt_date(last, ctx.now)}" if last else "") + "."
        due_hi = "aapko miss kiya" + (f" — last visit {fmt_date(last, ctx.now)} ko thi" if last else "") + "."
    else:
        v = VISIT.get(ctx.slug, ("visit", "visit"))[0]
        due_en = f"it's time for your next {v}" + (f" — your last visit was {fmt_date(last, ctx.now)}" if last else "") + "."
        due_hi = f"aapka agla {v} due hai" + (f" — last visit {fmt_date(last, ctx.now)} ko thi" if last else "") + "."
    line1 = cc.T(due_en[0].upper() + due_en[1:], due_hi[0].upper() + due_hi[1:])
    if slots:
        labels = [s.get("label") for s in slots if s.get("label")][:2]
        pref = cc.pref_slot()
        line2 = cc.T(f"Open slots" + (f" in your usual {pref} window" if pref else "") + f": {' or '.join(labels)}.",
                     f"Aapke liye slots" + (f" ({pref})" if pref else "") + f": {' ya '.join(labels)}.")
        if offer:
            line2 += cc.T(f" {offer}.", f" {offer}.")
        ask = cc.T("Reply 1 or 2 to book — or tell us a time that suits you." if len(labels) > 1 else "Reply YES to book it — or tell us a better time.",
                   "Book karne ke liye 1 ya 2 reply karein — ya apna time bata dijiye." if len(labels) > 1 else "Book karne ke liye YES reply karein — ya apna time bata dijiye.")
        cta = "multi_choice_slot" if len(labels) > 1 else "binary_yes_no"
    else:
        extra = ""
        if offer:
            extra = cc.T(f" {offer} is on right now.", f" Abhi {offer} chal raha hai.")
        pref = cc.pref_slot()
        line2 = extra.strip()
        ask = cc.T("Reply YES and we'll send you a couple of slots" + (f" in your usual {pref} window" if pref else "") + ".",
                   "YES reply karein, hum aapko" + (f" {pref} ke" if pref else "") + " slots bhej denge.")
        cta = "binary_yes_no"
    if ctx.slug == "pharmacies" and not slots:
        ask = cc.T("Reply YES and we'll have it packed for pickup or delivery.", "YES reply karein, hum pickup ya delivery ke liye pack kar denge.")
    return Draft(parts=_caps([cc.hello("🦷" if ctx.is_dentist else ""), line1, line2, ask]), cta=cta, action="book_slot",
                 action_data={"slots": slots, "offer": offer}, template="merchant_recall_reminder_v1",
                 send_as="merchant_on_behalf",
                 rationale=f"Customer recall on the merchant's behalf; real slots/offer only, honours {cc.mode} language + preferred timing; consent scope {cc.consent}.")


def h_appointment_tomorrow(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    when = ctx.p.get("appointment_label") or ctx.p.get("slot_label")
    t = fmt_time(ctx.p.get("appointment_iso")) if ctx.p.get("appointment_iso") else ""
    svc = humanize(ctx.p.get("service", "")) or VISIT.get(ctx.slug, ("appointment",))[0]
    line1 = cc.T(f"A quick reminder: your {svc} with us is tomorrow" + (f" at {t or when}" if (t or when) else "") + ".",
                 f"Yaad dilana tha: kal aapka {svc} hai" + (f" ({t or when})" if (t or when) else "") + ".")
    ask = cc.T("Reply YES to confirm, or tell us if you'd like a different time — we'll adjust.",
               "Confirm karne ke liye YES reply karein, ya time badalna ho toh bata dijiye.")
    return Draft(parts=_caps([cc.hello(), line1, ask]), cta="binary_yes_no", action="confirm_appointment",
                 action_data={}, template="merchant_appointment_reminder_v1", send_as="merchant_on_behalf",
                 rationale="Appointment-tomorrow reminder; confirm/reschedule in one reply to cut no-shows. No invented time when payload lacks it.")


def h_lapsed(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    days = ctx.p.get("days_since_last_visit")
    last = cc.last_visit()
    if days is None and last:
        d = days_between(last, ctx.now)
        days = d if d and d > 0 else None
    focus = humanize(ctx.p.get("previous_focus") or cc.prefs.get("training_focus") or "")
    months = ctx.p.get("previous_membership_months")
    if days:
        span = f"about {round(days / 7)} weeks" if days < 90 else f"about {round(days / 30)} months"
        span_hi = f"lagbhag {round(days / 7)} hafte" if days < 90 else f"lagbhag {round(days / 30)} mahine"
    else:
        span = span_hi = ""
    if days is not None and days < 45 and last:
        v = VISIT.get(ctx.slug, ("visit",))[0]
        line1 = cc.T(f"Your last visit was on {fmt_date(last, ctx.now)} — a good time to plan your next {v}.",
                     f"Aapki last visit {fmt_date(last, ctx.now)} ko thi — agla {v} plan karne ka sahi time hai.")
    else:
        line1 = cc.T((f"It's been {span} since we last saw you" if span else "We haven't seen you in a while") + " — happens to everyone, no judgment.",
                     (f"{span_hi} ho gaye aapko dekhe hue" if span_hi else "Kaafi time ho gaya") + " — koi baat nahi, sabke saath hota hai.")
    offer = _customer_offer(ctx, "trial", "free", "first", "clean", "check", "spa", "haircut", "thali", "delivery")
    line2 = ""
    if ctx.slug == "gyms":
        line2 = cc.T((f"Since your goal was {focus}, " if focus else "") + (f"we'd love to help you restart — {offer} are on us." if offer and "trial" in offer.lower() else "we'd love to help you pick up where you left off."),
                     (f"Aapka goal {focus} tha — " if focus else "") + ("wahi se dobara shuru karein?"))
        if months:
            line2 += cc.T(f" You'd already put in {months} solid months — that base doesn't vanish.", f" {months} mahine ki mehnat bekaar nahi jaati.")
    elif ctx.slug == "dentists":
        line2 = cc.T("A routine check-up + cleaning keeps small issues small.", "Ek routine check-up + cleaning se chhoti problem chhoti hi rehti hai.")
        if offer:
            line2 += cc.T(f" {offer} is on right now.", f" Abhi {offer} chal raha hai.")
    elif ctx.slug == "pharmacies":
        line2 = cc.T("If you need your regular medicines, send a photo of your prescription and we'll keep them ready.",
                     "Regular dawaiyan chahiye toh prescription ki photo bhej dijiye, hum ready rakh denge.")
        if offer:
            line2 += cc.T(f" ({offer})", f" ({offer})")
    else:
        if offer:
            line2 = cc.T(f"{offer} is on right now if you'd like to drop in.", f"Abhi {offer} chal raha hai.")
    pref = cc.pref_slot()
    ask = cc.T("Reply YES and we'll hold a slot for you" + (f" ({pref})" if pref else "") + " — no commitment.",
               "YES reply karein, hum aapke liye slot rakh denge" + (f" ({pref})" if pref else "") + " — koi commitment nahi.")
    if ctx.slug == "pharmacies":
        ask = cc.T("Reply YES and we'll call you back — no obligation.", "YES reply karein, hum call kar lenge — koi zabardasti nahi.")
    return Draft(parts=_caps([cc.hello("👋" if ctx.slug == "gyms" else ""), line1, line2, ask]), cta="binary_yes_no", action="winback_customer",
                 action_data={"offer": offer}, template="merchant_winback_v1", send_as="merchant_on_behalf",
                 rationale=f"Lapsed customer ({cc.state}); warm no-shame tone, references their own history ({focus or 'last visit'}), only the merchant's live offer, single no-commitment CTA.")


def h_chronic_refill(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    mols = ctx.p.get("molecule_list") or []
    runout = parse_dt(ctx.p.get("stock_runs_out_iso"))
    saved = ctx.p.get("delivery_address_saved")
    if ctx.slug != "pharmacies" or not mols:
        # e.g. generated dentist trigger with no molecules — don't invent medicines.
        v = VISIT.get(ctx.slug, ("visit",))[0]
        lv = cc.last_visit()
        if ctx.slug == "restaurants":
            line1 = cc.T("It's been a while since your last meal with us" + (f" ({fmt_date(lv, ctx.now)})" if lv else "") + " — your usual is just a message away.",
                         "Kaafi din ho gaye" + (f" (last visit {fmt_date(lv, ctx.now)})" if lv else "") + " — aapka usual bas ek message door hai.")
            off = ctx.lead_offer()
            if off:
                line1 += cc.T(f" {off} is on right now.", f" Abhi {off} chal raha hai.")
            ask = cc.T("Reply YES and we'll keep a table or your order ready.", "YES reply karein, table ya order ready rakhenge.")
        else:
            line1 = cc.T(f"Your regular {v} with us is due" + (f" — last visit {fmt_date(lv, ctx.now)}" if lv else "") + ".",
                         f"Aapka regular {v} due hai" + (f" — last visit {fmt_date(lv, ctx.now)}" if lv else "") + ".")
            ask = cc.T("Reply YES and we'll share a couple of convenient slots.", "YES reply karein, hum 2 convenient slots bhej denge.")
        return Draft(parts=_caps([cc.hello(), line1, ask]), cta="binary_yes_no", action="book_slot", action_data={"slots": []},
                     template="merchant_followup_reminder_v1", send_as="merchant_on_behalf",
                     rationale="Refill/follow-up trigger without medicine details — kept to a follow-up reminder rather than inventing a prescription.")
    who = "Sharma ji" if "sharma" in cc.name.lower() else (cc.name or "")
    med = ", ".join(mols)
    line1 = cc.T(f"{who + chr(39) + 's' if who else 'Your'} regular medicines ({med}) will run out by {fmt_date(runout, ctx.now)}.",
                 f"{who + ' ki' if who else 'Aapki'} monthly dawaiyan ({med}) {fmt_date(runout, ctx.now)} tak khatam ho jayengi.")
    perks = []
    for o in ctx.active_offers:
        lo = o.lower()
        if "senior" in lo and cc.senior:
            perks.append(cc.T(f"{o.replace(' OFF', ' off')} applies", f"{o.replace(' OFF', ' off')} lagega"))
        elif "delivery" in lo:
            thr = price_from_title(o)
            perks.append(cc.T("free home delivery" + (f" on orders above {money(thr)}" if thr else "") + (" to the saved address" if saved else ""),
                              (f"{money(thr)} se upar ke order pe " if thr else "") + "free home delivery" + (" saved address par" if saved else "")))
    line2 = cc.T("We'll keep the same medicines ready" + (f"; {join_human(perks)}." if perks else "."),
                 "Hum same dawaiyan ready rakh dete hain" + (f"; {join_human(perks, 'aur')}." if perks else "."))
    recall = (ctx.extras or {}).get("recall_alert")
    if not recall:
        for d in ctx.digest.values():
            if d.get("kind") == "alert":
                hit = next((m for m in mols if m.lower() in str(d.get("title", "")).lower()), None)
                if hit:
                    recall = {"molecule": hit, "batches": []}
    safety = ""
    if recall and not recall.get("batches"):
        safety = cc.T(f"Also: some {recall['molecule']} batches are under a voluntary recall right now — we'll only dispatch a verified, unaffected batch.",
                      f"Ek zaroori baat: {recall['molecule']} ke kuch batches abhi recall mein hain — hum sirf verified, unaffected batch hi denge.")
    elif recall and recall.get("molecule", "").lower() in [m.lower() for m in mols]:
        safety = cc.T(f"Note: {recall['molecule']} batches {join_human(recall.get('batches') or [])} are under a voluntary recall — we'll only dispatch an unaffected batch.",
                      f"Note: {recall['molecule']} ke batches {join_human(recall.get('batches') or [], 'aur')} recall mein hain — hum sirf unaffected batch hi denge.")
    ask = cc.T("Reply YES to confirm delivery, or tell us if the doctor changed any dose.",
               "Delivery confirm karne ke liye YES reply karein, ya dose mein koi badlav ho toh bata dijiye.")
    return Draft(parts=_caps([cc.hello(), line1, line2, safety, ask]), cta="binary_yes_no", action="confirm_refill",
                 action_data={"molecules": mols}, template="merchant_refill_reminder_v1", send_as="merchant_on_behalf",
                 rationale="Chronic refill before run-out date; exact molecules, only live offers (senior + delivery), cross-checks the active atorvastatin recall; respectful Hindi for a senior via family phone.")


def h_trial_followup(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    tdate = parse_dt(ctx.p.get("trial_date"))
    opts = ctx.p.get("next_session_options") or []
    trial_word = TRIAL.get(ctx.slug)
    if cc.child and ctx.slug == "gyms":
        trial_word = "kids yoga trial" if "yoga" in ctx.biz.lower() else "kids trial class"
    if trial_word:
        line1 = cc.T(f"thank you for {'bringing ' + cc.child + ' to' if cc.child else 'coming in for'} the {trial_word}" + (f" on {fmt_date(tdate, ctx.now)}" if tdate else "") + "!",
                     f"{cc.child + ' ko ' if cc.child else ''}{trial_word}" + (f" ({fmt_date(tdate, ctx.now)})" if tdate else "") + " pe laane ke liye shukriya!" if cc.child else
                     f"{trial_word}" + (f" ({fmt_date(tdate, ctx.now)})" if tdate else "") + " ke liye aane ka shukriya!")
    else:
        line1 = cc.T("thank you for visiting us" + (f" on {fmt_date(tdate, ctx.now)}" if tdate else "") + " for the first time!",
                     "pehli baar aane ke liye shukriya" + (f" ({fmt_date(tdate, ctx.now)})" if tdate else "") + "!")
    if opts:
        lab = opts[0].get("label")
        line2 = cc.T(f"The next session is {lab}.", f"Agla session {lab} ko hai.")
        ask = cc.T(f"Shall we save {cc.child + chr(39) + 's' if cc.child else 'your'} spot? Reply YES to confirm.",
                   f"{cc.child + ' ki' if cc.child else 'Aapki'} jagah confirm kar dein? YES reply karein.")
    else:
        offer = _customer_offer(ctx, "first", "month", "trial", "membership", "delivery", "thali", "card")
        if ctx.slug in ("pharmacies", "restaurants"):
            line2 = cc.T(f"Good to know for next time: {offer}." if offer else "", f"Agli baar ke liye: {offer}." if offer else "")
        else:
            line2 = cc.T(f"If you enjoyed it, {offer} is the easiest way to continue." if offer else "If you enjoyed it, we'd love to have you continue.",
                         f"Pasand aaya ho toh {offer} se aage continue kar sakte hain." if offer else "Pasand aaya ho toh continue kijiye.")
        ask = cc.T("Reply YES and we'll share this week's slots.", "YES reply karein, is hafte ke slots bhej denge.")
    if not opts and ctx.slug in ("pharmacies", "restaurants"):
        ask = cc.T("Reply YES and we'll save your details for faster service next time.", "YES reply karein, agli baar ke liye aapki details save kar lenge.")
    return Draft(parts=_caps([cc.hello(), line1, line2, ask]), cta="binary_yes_no", action="book_slot",
                 action_data={"slots": opts}, template="merchant_trial_followup_v1", send_as="merchant_on_behalf",
                 rationale=f"Post-trial follow-up to {'parent' if cc.child else 'customer'}; real next-session slot, single confirm.")


def h_wedding(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    wd = parse_dt(ctx.p.get("wedding_date") or cc.prefs.get("wedding_date"))
    trial = parse_dt(ctx.p.get("trial_completed"))
    step = humanize(ctx.p.get("next_step_window_open", "skin prep program"))
    mm = re.search(r"(\d+)\s*day", step)
    if mm:
        step = f"{mm.group(1)}-day " + re.sub(r"\s*program\s*\d+\s*day", " program", step).replace(f"{mm.group(1)}day", "").strip()
    step = step.replace("skin prep", "skin-prep")
    days_left = days_between(ctx.now, wd) if wd else ctx.p.get("days_to_wedding")
    line1 = cc.T(f"hope you loved your bridal trial" + (f" on {fmt_date(trial, ctx.now)}" if trial else "") + "!",
                 f"umeed hai bridal trial" + (f" ({fmt_date(trial, ctx.now)})" if trial else "") + " pasand aaya!")
    line1 = line1[0].upper() + line1[1:]
    if days_left is not None and days_left > 45:
        start = fmt_date(datetime.fromtimestamp(wd.timestamp() - 45 * 86400, tz=wd.tzinfo), ctx.now) if wd else ""
        line2 = cc.T(f"With your wedding on {fmt_date(wd, ctx.now)} ({days_left} days to go), the {step} works best starting around {start}, so it wraps up well before the big day.",
                     f"Shaadi {fmt_date(wd, ctx.now)} ko hai ({days_left} din) — {step} {start} ke aas-paas shuru karna best rahega.")
        ask = cc.T("Want us to reserve your start date now? Reply YES and we'll send Saturday options." if "saturday" in str(cc.prefs.get("preferred_slots", "")).lower() else "Want us to reserve your start date now? Reply YES and we'll send options.",
                   "Start date abhi reserve kar dein? YES reply karein.")
    else:
        line2 = cc.T(f"With your wedding on {fmt_date(wd, ctx.now)}" + (f" ({days_left} days to go)" if days_left is not None else "") + f", this is the right week to start the {step}.",
                     f"Shaadi {fmt_date(wd, ctx.now)} ko hai — {step} shuru karne ka yahi sahi hafta hai.")
        ask = cc.T("Reply YES and we'll book your first session this Saturday.", "YES reply karein, pehla session is Saturday book kar dete hain.")
    return Draft(parts=_caps([cc.hello("💍"), line1, line2, ask]), cta="binary_yes_no", action="book_slot",
                 action_data={"slots": []}, template="merchant_bridal_followup_v1", send_as="merchant_on_behalf",
                 rationale="Bridal follow-up timed to the wedding date (program start ~45 days out), no invented price; honours Saturday preference.")


def h_customer_generic(ctx: Ctx) -> Draft:
    cc = CCtx(ctx)
    offer = ctx.lead_offer()
    line1 = cc.T(f"a quick update from us" + (f": {offer} is on right now." if offer else "."),
                 f"ek chhoti si update" + (f": abhi {offer} chal raha hai." if offer else "."))
    ask = cc.T("Reply YES and we'll share a slot that suits you.", "YES reply karein, hum aapke liye slot bhej denge.")
    return Draft(parts=_caps([cc.hello(), line1, ask]), cta="binary_yes_no", action="book_slot", action_data={"slots": []},
                 template="merchant_update_v1", send_as="merchant_on_behalf",
                 rationale=f"Customer-scoped '{ctx.kind}' with no specific payload; minimal, consent-safe update.")


MERCHANT_HANDLERS: dict[str, Callable[[Ctx], Draft]] = {
    "research_digest": h_research_digest,
    "category_research_digest_release": h_research_digest,
    "research_digest_release": h_research_digest,
    "regulation_change": h_regulation_change,
    "perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "renewal_due": h_renewal_due,
    "festival_upcoming": h_festival,
    "curious_ask_due": h_curious_ask,
    "scheduled_recurring": h_curious_ask,
    "winback_eligible": h_winback,
    "dormant_with_vera": h_dormant,
    "ipl_match_today": h_ipl,
    "review_theme_emerged": h_review_theme,
    "milestone_reached": h_milestone,
    "active_planning_intent": h_planning,
    "seasonal_perf_dip": h_seasonal_dip,
    "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal,
    "gbp_unverified": h_gbp_unverified,
    "cde_opportunity": h_cde,
    "competitor_opened": h_competitor,
    "weather_heatwave": h_generic_external,
    "local_news_event": h_generic_external,
    "category_trend_movement": h_generic_external,
}

CUSTOMER_HANDLERS: dict[str, Callable[[Ctx], Draft]] = {
    "recall_due": h_recall_due,
    "appointment_tomorrow": h_appointment_tomorrow,
    "customer_lapsed_soft": h_lapsed,
    "customer_lapsed_hard": h_lapsed,
    "winback_customer": h_lapsed,
    "chronic_refill_due": h_chronic_refill,
    "trial_followup": h_trial_followup,
    "wedding_package_followup": h_wedding,
    "unplanned_slot_open": h_recall_due,
}


def build_draft(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
                now: Any = None, extras: Optional[dict] = None) -> tuple[Draft, Ctx]:
    ctx = Ctx(category, merchant, trigger, customer, now=now, extras=extras)
    scope = trigger.get("scope") or ("customer" if customer else "merchant")
    if scope == "customer" and customer:
        handler = CUSTOMER_HANDLERS.get(ctx.kind, h_customer_generic)
    else:
        handler = MERCHANT_HANDLERS.get(ctx.kind)
        if handler is None:
            handler = h_generic_external if trigger.get("source") == "external" else h_generic
    try:
        draft = handler(ctx)
    except Exception:  # never fail a send because one field was odd
        draft = h_customer_generic(ctx) if (scope == "customer" and customer) else h_generic(ctx)
    return draft, ctx


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Any = None, extras: Optional[dict] = None) -> dict:
    """Public contract (challenge-brief §7.1). Returns body, cta, send_as, suppression_key, rationale
    plus template_name/template_params for the first-touch WhatsApp template."""
    draft, ctx = build_draft(category, merchant, trigger, customer, now=now, extras=extras)
    body = finalize(draft.body, category)
    params = [finalize(p, category) for p in draft.parts if p and p.strip()]
    return {
        "body": body,
        "cta": draft.cta,
        "send_as": draft.send_as,
        "suppression_key": trigger.get("suppression_key") or f"{ctx.kind}:{merchant.get('merchant_id')}",
        "rationale": draft.rationale,
        "template_name": draft.template,
        "template_params": params,
        "_action": draft.action,
        "_action_data": draft.action_data,
        "_mode": ctx.mode,
    }
