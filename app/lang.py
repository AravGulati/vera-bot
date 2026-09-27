"""Language selection: which register to write in, per merchant / customer / turn."""
from __future__ import annotations

import re

# Strong Hinglish markers (romanised Hindi). Kept to words that are unlikely in English.
_HI_MARKERS = {
    "hai", "hain", "nahi", "nahin", "nhi", "kya", "karo", "kar", "karna", "karenge", "haan", "haa", "ha",
    "aap", "aapka", "aapke", "mujhe", "mera", "meri", "chahiye", "kitna", "kitne", "bhai", "ji", "theek",
    "thik", "accha", "acha", "achha", "bolo", "bataiye", "batao", "abhi", "baad", "mein", "kal", "bhejo",
    "bhejiye", "chalo", "chalega", "dijiye", "karein", "hoga", "toh", "lekin", "matlab", "samjha", "jaldi",
    "judna", "judrna", "shukriya", "dhanyavaad", "namaste", "paisa", "paise", "kaise", "kyun", "kyu",
}


def merchant_mode(merchant: dict, category: dict) -> str:
    """'hi' = natural Hindi-English code-mix; 'en' = English.

    Rule: code-mix only when Hindi is the merchant's first regional language AND the
    category voice allows natural code-mix. A Chennai studio listing [en, ta, hi]
    gets English, not Hinglish.
    """
    langs = [str(l).lower() for l in (merchant.get("identity", {}) or {}).get("languages") or ["en"]]
    regional = [l for l in langs if l not in ("en", "english")]
    code_mix = str(((category or {}).get("voice") or {}).get("code_mix", "hindi_english_natural"))
    if regional and regional[0] in ("hi", "hindi") and "english_primary" not in code_mix:
        return "hi"
    return "en"


def customer_mode(customer: dict) -> tuple[str, str]:
    """Returns (mode, greeting). mode in {'hi', 'en'}; greeting honours regional mix."""
    pref = str(((customer or {}).get("identity") or {}).get("language_pref", "en")).lower()
    if pref in ("hi", "hindi"):
        return "hi", "Namaste"
    if pref.startswith("hi"):
        return "hi", "Hi"
    if pref.startswith("ta"):
        return "en", "Vanakkam"
    if pref.startswith("te"):
        return "en", "Namaskaram"
    if pref.startswith("kn"):
        return "en", "Namaskara"
    if pref.startswith("mr"):
        return "en", "Namaskar"
    return "en", "Hi"


def detect_text_lang(text: str) -> str:
    """Per-turn detection of the merchant's language. Returns 'hi' or 'en'."""
    if not text:
        return "en"
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return "en"
    hits = sum(1 for w in words if w in _HI_MARKERS)
    if hits >= 2 or (hits >= 1 and len(words) <= 4):
        return "hi"
    return "en"


def T(mode: str, en: str, hi: str) -> str:
    return hi if mode == "hi" else en
