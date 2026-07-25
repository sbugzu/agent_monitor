"""Reusable multi-session state aggregation for native agent adapters."""

import time
from typing import Any, Dict, Optional

from agent_monitor.adapters.base import (
    BaseAgentAdapter,
    EventResult,
    NormalizedAgentEvent,
)
from agent_monitor.core.states import AgentState


class SessionAwareAgentAdapter(BaseAgentAdapter):
    """Base adapter for agents that can report concurrent task sessions."""

    STATE_PRIORITY = {
        AgentState.IDLE: 0,
        AgentState.COMPLETED_UNREAD: 1,
        AgentState.ERROR: 2,
        AgentState.THINKING: 3,
        AgentState.WAITING_APPROVAL: 4,
    }

    def __init__(self, agent_id: str, display_name: str):
        super().__init__(agent_id, display_name)
        self.session_states: Dict[str, Dict[str, Any]] = {}
        self._visible_session_id: Optional[str] = None

    @property
    def visible_session_id(self) -> Optional[str]:
        return self._visible_session_id

    def _refresh_visible_state(self, empty_message: str = "Standing by") -> None:
        if not self.session_states:
            self._visible_session_id = None
            self.display_phase = ""
            self.update_state(AgentState.IDLE, message=empty_message)
            return

        self._visible_session_id, visible = max(
            self.session_states.items(),
            key=lambda pair: (
                self.STATE_PRIORITY[pair[1]["state"]],
                bool(pair[1].get("interactive", False)),
                pair[1]["updated_at"],
            ),
        )
        self.display_phase = str(visible.get("phase") or "")
        self.update_state(visible["state"], message=visible["message"])

    def apply_event(self, event: NormalizedAgentEvent) -> EventResult:
        previous_state = self.current_state
        if event.acknowledge:
            self.acknowledge_read()
        elif event.state is not None and event.session_id:
            previous = self.session_states.get(event.session_id, {})
            self.session_states[event.session_id] = {
                **previous,
                "state": event.state,
                "message": event.message,
                "updated_at": time.time(),
                "interactive": event.interactive,
                "phase": event.phase,
                "request_id": event.request_id or previous.get("request_id", ""),
                "turn_id": str(
                    event.payload.get("turn_id")
                    or event.payload.get("turnId")
                    or previous.get("turn_id")
                    or ""
                ),
                "transcript_path": str(
                    event.payload.get("transcript_path")
                    or event.payload.get("transcriptPath")
                    or previous.get("transcript_path")
                    or ""
                ),
            }
            self._refresh_visible_state()
        elif event.state is not None:
            # Manual/generic events may not carry a native task identity.
            self.update_state(event.state, message=event.message)
            self.display_phase = event.phase
        return EventResult(event, previous_state, self.current_state)

    def mark_approval_phase(
        self,
        session_id: str,
        request_id: str,
        phase: str,
        message: str,
    ) -> bool:
        item = self.session_states.get(session_id) if session_id else None
        if item is None and self.visible_session_id:
            item = self.session_states.get(self.visible_session_id)
            session_id = self.visible_session_id
        if item is None:
            return super().mark_approval_phase(
                session_id, request_id, phase, message
            )
        if item.get("request_id") and request_id and item["request_id"] != request_id:
            return False

        item["phase"] = phase
        item["message"] = message
        item["updated_at"] = time.time()
        item["interactive"] = phase == "new_approval"
        if phase in ("approved_running", "approval_rejected"):
            item["state"] = AgentState.THINKING
        self._refresh_visible_state()
        return True

    def acknowledge_read(self):
        # Remove completed sessions so another session update cannot resurrect
        # an already acknowledged completion.
        self.session_states = {
            session_id: item
            for session_id, item in self.session_states.items()
            if item["state"] != AgentState.COMPLETED_UNREAD
        }
        super().acknowledge_read()
        if self.session_states:
            self._refresh_visible_state()

    def reset_state(self, message: str = "Standing by") -> None:
        self.session_states.clear()
        self._visible_session_id = None
        super().reset_state(message)
