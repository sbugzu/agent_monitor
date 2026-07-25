"""
Adapter for Antigravity AI Agent.
Handles Antigravity agent lifecycle events.
"""

import time
from typing import Dict, Any
from agent_monitor.adapters.base import (
    EventResult,
    NormalizedAgentEvent,
    extract_request_id,
    extract_session_id,
    extract_tool_command,
    normalize_event_name,
)
from agent_monitor.adapters.session import SessionAwareAgentAdapter
from agent_monitor.core.states import AgentState

class AntigravityAdapter(SessionAwareAgentAdapter):
    TERMINAL_EVENT_GRACE_SECONDS = 5.0

    def __init__(self, agent_id: str = "antigravity", display_name: str = "Antigravity"):
        super().__init__(agent_id, display_name)
        self.last_native_event = ""
        self.last_native_event_at = 0.0
        self.last_native_session_id = ""

    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        # Native Antigravity hooks use CamelCase event names and camelCase
        # payload fields.  Keep aliases for the simple agent-hook CLI too.
        event = normalize_event_name(event_name)
        tool_call = payload.get("toolCall") or {}
        command = extract_tool_command(payload)
        msg = (
            command
            or payload.get("message")
            or payload.get("task")
            or payload.get("reason")
            or tool_call.get("name")
            or ""
        )

        state = None
        opens_interaction = False
        if event == "pretooluse":
            # PreToolUse waits for either a hardware decision or Antigravity's
            # native permission prompt, so it represents an approval gate.
            state = AgentState.WAITING_APPROVAL
            msg = msg or "Approve tool use"
            opens_interaction = True
        elif event == "posttooluse" and payload.get("error"):
            state = AgentState.ERROR
            msg = str(payload.get("error"))
        elif event in (
            "start", "planning", "thinking", "executing", "running",
            "toolcall", "posttooluse", "preinvocation", "postinvocation",
        ):
            state = AgentState.THINKING
            msg = msg or "Executing plan..."
        elif event in (
            "wait", "ask", "approval", "inputneeded", "confirm",
            "permissionrequest", "inputrequired",
        ):
            state = AgentState.WAITING_APPROVAL
            msg = msg or "Needs user input"
            opens_interaction = True
        elif event == "stop":
            termination_reason = str(payload.get("terminationReason") or "").lower()
            error = payload.get("error")
            if error or any(word in termination_reason for word in ("error", "fail", "crash")):
                state = AgentState.ERROR
                msg = str(error or termination_reason)
            elif any(word in termination_reason for word in ("approval", "permission", "input", "user")):
                state = AgentState.WAITING_APPROVAL
                msg = msg or "Needs user input"
            elif payload.get("fullyIdle", True):
                state = AgentState.COMPLETED_UNREAD
                msg = msg or "Task completed"
            else:
                state = AgentState.THINKING
                msg = msg or "Background tasks running..."
        elif event in ("finish", "done", "complete", "completed", "success", "finished"):
            state = AgentState.COMPLETED_UNREAD
            msg = msg or "Task completed"
        elif event in ("error", "fail", "failed", "exception", "crash"):
            state = AgentState.ERROR
            msg = msg or "Error encountered"
        elif event in ("idle", "ack", "read"):
            return NormalizedAgentEvent(
                name=event,
                acknowledge=True,
                session_id=extract_session_id(payload),
                request_id=extract_request_id(payload),
                payload=payload,
            )

        return NormalizedAgentEvent(
            name=event,
            state=state,
            message=str(msg),
            session_id=extract_session_id(payload),
            request_id=extract_request_id(payload),
            phase="new_approval" if opens_interaction else "",
            interactive=opens_interaction,
            opens_interaction=opens_interaction,
            correlated_tool_event=event in ("pretooluse", "posttooluse"),
            completes_request=event == "posttooluse",
            payload=payload,
        )

    def apply_event(self, event: NormalizedAgentEvent) -> EventResult:
        result = super().apply_event(event)
        self.last_native_event = event.name
        self.last_native_event_at = time.monotonic()
        self.last_native_session_id = event.session_id
        return result

    def reconcile_external_state(self) -> bool:
        """Finish runs whose manual stop path omitted the native Stop hook."""
        if super().reconcile_external_state():
            return True

        if (
            self.current_state != AgentState.THINKING
            or self.last_native_event not in ("posttooluse", "postinvocation")
            or time.monotonic() - self.last_native_event_at
            < self.TERMINAL_EVENT_GRACE_SECONDS
        ):
            return False

        self.apply_event(
            NormalizedAgentEvent(
                name="reconciled",
                state=AgentState.COMPLETED_UNREAD,
                message="Task finished",
                session_id=self.last_native_session_id,
            )
        )
        return True

    def reconcile_aborted_sessions(self) -> bool:
        """Backward-compatible alias for the previous public method name."""
        return self.reconcile_external_state()
