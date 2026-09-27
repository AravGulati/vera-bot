"""Optional multi-turn handler (challenge-brief §7.4).

``respond(state, merchant_message)`` where ``state`` is a dict with the contexts and the
conversation so far. Uses the same engine as the live /v1/reply endpoint.
"""
from __future__ import annotations

from app.composer import Ctx
from app.replies import default_action, respond as _respond


def respond(state: dict, merchant_message: str) -> dict:
    """state keys: category, merchant, trigger (optional), customer (optional),
    conversation (dict, mutated in place), merchant_state (dict, mutated), from_role."""
    ctx = Ctx(state.get("category") or {}, state.get("merchant") or {}, state.get("trigger") or {"payload": {}},
              state.get("customer"), now=state.get("now"))
    conv = state.setdefault("conversation", {})
    if "action" not in conv:
        action, data = default_action(ctx)
        conv.update({"action": action, "action_data": data, "stage": 0, "status": "open", "bot_bodies": []})
    return _respond(conv, ctx, merchant_message, state.setdefault("merchant_state", {}),
                    from_role=state.get("from_role", "merchant"))
