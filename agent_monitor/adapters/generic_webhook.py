"""
Generic Webhook Adapter for any custom AI Agent or script.
Receives arbitrary JSON payload specifying explicit AgentState or status string.
"""

from typing import Dict, Any, Optional
from agent_monitor.adapters.base import (
    BaseAgentAdapter,
    NormalizedAgentEvent,
    extract_request_id,
    extract_session_id,
    normalize_event_name,
)
from agent_monitor.core.states import AgentState

class GenericWebhookAdapter(BaseAgentAdapter):
    def __init__(self, agent_id: str, display_name: Optional[str] = None):
        name = display_name or agent_id.capitalize()
        super().__init__(agent_id, name)

    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        state_str = payload.get("state") or payload.get("status") or event_name
        message = payload.get("message") or payload.get("msg") or ""

        # Normalize state
        state_upper = str(state_str).upper()
        if hasattr(AgentState, state_upper):
            target_state = AgentState[state_upper]
        else:
            # Smart mapping fallback
            if "THINK" in state_upper or "RUN" in state_upper or "BUSY" in state_upper:
                target_state = AgentState.THINKING
            elif "WAIT" in state_upper or "ASK" in state_upper or "APPROV" in state_upper or "INPUT" in state_upper:
                target_state = AgentState.WAITING_APPROVAL
            elif "DONE" in state_upper or "FINISH" in state_upper or "SUCCESS" in state_upper or "COMPLETE" in state_upper:
                target_state = AgentState.COMPLETED_UNREAD
            elif "ERR" in state_upper or "FAIL" in state_upper:
                target_state = AgentState.ERROR
            else:
                target_state = AgentState.IDLE

        acknowledge = target_state == AgentState.IDLE and (
            payload.get("ack", False) or state_upper in ("ACK", "READ")
        )
        waiting = target_state == AgentState.WAITING_APPROVAL
        return NormalizedAgentEvent(
            name=normalize_event_name(event_name),
            state=None if acknowledge else target_state,
            message=str(message),
            session_id=extract_session_id(payload),
            request_id=extract_request_id(payload),
            phase="new_approval" if waiting else "",
            interactive=waiting,
            opens_interaction=waiting,
            acknowledge=acknowledge,
            payload=payload,
        )
