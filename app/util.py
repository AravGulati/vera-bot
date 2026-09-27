"""Small, dependency-free formatting + date helpers used across the bot."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# Reference "today" of the challenge dataset (triggers, histories and
# days_until values in the seed data are all anchored to 26 Apr 2026).
# Used only when the caller gives us no clock (e.g. offline compose()).
REF_NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse ISO date/datetime strings ('2026-05-12', '...Z', '+05:30')."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def fmt_date(value: Any, now: Optional[datetime] = None, dow: bool = False) -> str:
    """'2026-12-15' -> '15 Dec' (adds year when it differs from `now`)."""
    d = parse_dt(value)
    if d is None:
        return str(value) if value else ""
    out = f"{d.day} {_MONTHS[d.month - 1]}"
    if now is not None and d.year != now.year:
        out += f" {d.year}"
    if dow:
        out = f"{_DOW[d.weekday()]} {out}"
    return out


def fmt_time(value: Any) -> str:
    """'2026-04-26T19:30:00+05:30' -> '7:30pm' (local time as written)."""
    d = parse_dt(value)
    if d is None:
        return ""
    h, m = d.hour, d.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


def days_between(a: Optional[datetime], b: Optional[datetime]) -> Optional[int]:
    if a is None or b is None:
        return None
    return (b.date() - a.date()).days


def inr_group(n: Any) -> str:
    """Indian digit grouping: 124000 -> '1,24,000'."""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    neg = n < 0
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s


def money(n: Any) -> str:
    return "₹" + inr_group(n)


def pct(x: Any, signed: bool = False) -> str:
    """0.18 -> '18%'; signed=True -> '+18%' / '−22%'."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    val = abs(v) * 100
    txt = f"{val:.1f}".rstrip("0").rstrip(".") if val < 10 and val != int(val) else f"{round(val)}"
    if signed:
        return ("+" if v >= 0 else "−") + txt + "%"
    return txt + "%"


def ctr_pct(x: Any) -> str:
    """CTR 0.021 -> '2.1%'."""
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(x)


def stable_index(key: str, n: int) -> int:
    if n <= 0:
        return 0
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % n


def humanize(slug: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning'; 'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    s = str(slug or "")
    s = re.sub(r"(\d)_(month|week|day|year)", r"\1-\2", s)
    return s.replace("_", " ").strip()


def clean_owner_name(owner: Any) -> str:
    """'Dr. Asha' -> 'Asha'; 'Meera' -> 'Meera'."""
    s = str(owner or "").strip()
    s = re.sub(r"^(dr\.?\s+)", "", s, flags=re.I)
    return s.split(" ")[0] if s else ""


def first(items: Iterable, default=None):
    for x in items:
        return x
    return default


def join_human(items: list[str], conj: str = "and") -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {conj} " + items[-1]


def price_from_title(title: str) -> Optional[int]:
    m = re.search(r"₹\s?([\d,]+)", title or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None
