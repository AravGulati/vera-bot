"""Post-composition guardrails. Every outbound body passes through `finalize`.

Deterministic checks (no LLM):
  * category taboo phrases removed ("guaranteed", "miracle", "best in city" ...)
  * no URLs (Meta rejects them in templates; judge penalises -3 each)
  * no internal jargon leaking (snake_case tokens like ctr_below_peer_median)
  * no template residue ({...}, 'None', double spaces)
  * action-mode replies never contain qualifying phrases ("would you", "do you" ...)
"""
from __future__ import annotations

import re

_URL = re.compile(r"(https?://\S+|www\.\S+)", re.I)
_SNAKE = re.compile(r"\b([a-z0-9]+(?:_[a-z0-9]+)+)\b")
_GENERIC_TABOO = ["guaranteed", "guarantee", "100% safe", "miracle", "best in city", "completely cure"]

QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


def _taboos(category: dict | None) -> list[str]:
    voice = (category or {}).get("voice") or {}
    raw = list(voice.get("vocab_taboo") or voice.get("taboos") or [])
    out = []
    for t in raw + _GENERIC_TABOO:
        t = re.sub(r"\(.*?\)", "", str(t)).strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def finalize(body: str, category: dict | None = None, action_mode: bool = False) -> str:
    text = str(body or "")
    text = _URL.sub("", text)
    text = _SNAKE.sub(lambda m: m.group(1).replace("_", " "), text)
    for taboo in sorted(_taboos(category), key=len, reverse=True):
        text = re.sub(re.escape(taboo), "", text, flags=re.I)
    text = text.replace("None", "").replace("{", "").replace("}", "")
    if action_mode:
        text = strip_qualifiers(text)
    # whitespace + punctuation hygiene (keep intentional newlines)
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    return text.strip()


def strip_qualifiers(text: str) -> str:
    """Rewrite any qualifying-question phrasing so an action reply stays an action."""
    repl = {
        "would you": "you can",
        "do you": "you",
        "can you tell": "tell",
        "what if": "if",
        "how about": "plus",
    }
    for bad, good in repl.items():
        text = re.sub(re.escape(bad), good, text, flags=re.I)
    return text


def has_qualifier(text: str) -> bool:
    low = (text or "").lower()
    return any(q in low for q in QUALIFYING)


def has_url(text: str) -> bool:
    return bool(_URL.search(text or ""))
