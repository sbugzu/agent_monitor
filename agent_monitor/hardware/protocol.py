"""Compact, bounded wire protocol helpers for hardware transports."""

import json
from typing import Any, Dict


MAX_MESSAGE_BYTES = 100
MAX_NAME_BYTES = 32
MAX_ACTION_ID_BYTES = 32
MAX_ACTION_LABEL_BYTES = 32
MAX_PHASE_BYTES = 24
MAX_INTERACTION_DETAIL_BYTES = 320
MAX_TOOL_NAME_BYTES = 40


def _truncate_utf8(value: Any, max_bytes: int) -> str:
    raw = str(value or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return raw.decode("utf-8")
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def compact_hardware_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return only fields consumed by firmware, bounded for its RX buffer."""
    if payload.get("cmd") != "SET_STATE":
        return payload

    active = payload.get("active") or {}
    compact = {
        "cmd": "SET_STATE",
        "active": {
            "display_name": _truncate_utf8(
                active.get("display_name", "Agent"), MAX_NAME_BYTES
            ),
            "state": str(active.get("state", "IDLE")),
            "color": str(active.get("color", "#FFFDF6")),
            "message": _truncate_utf8(
                active.get("message", ""), MAX_MESSAGE_BYTES
            ),
            "phase": _truncate_utf8(
                active.get("phase", ""), MAX_PHASE_BYTES
            ),
            "unread": bool(active.get("unread", False)),
        },
        "agents_count": int(payload.get("agents_count", 0)),
    }
    interaction = payload.get("interaction")
    if isinstance(interaction, dict):
        actions = []
        for action in interaction.get("actions", [])[:6]:
            if not isinstance(action, dict):
                continue
            actions.append({
                "id": _truncate_utf8(action.get("id", ""), MAX_ACTION_ID_BYTES),
                "label": _truncate_utf8(
                    action.get("label", ""), MAX_ACTION_LABEL_BYTES
                ),
                "dangerous": bool(action.get("dangerous", False)),
            })
        if actions:
            compact["interaction"] = {
                "request_id": _truncate_utf8(
                    interaction.get("request_id", ""), 64
                ),
                "detail": _truncate_utf8(
                    interaction.get("detail", ""),
                    MAX_INTERACTION_DETAIL_BYTES,
                ),
                "tool_name": _truncate_utf8(
                    interaction.get("tool_name", ""),
                    MAX_TOOL_NAME_BYTES,
                ),
                "actions": actions,
                # Firmware safely ignores this field. It exists to make a
                # Return replay distinct from the preceding state frame.
                "revision": int(interaction.get("revision", 0)),
            }
    return compact


def encode_hardware_frame(payload: Dict[str, Any]) -> bytes:
    """Serialize one newline-delimited compact JSON frame."""
    compact = compact_hardware_payload(payload)
    return (
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
