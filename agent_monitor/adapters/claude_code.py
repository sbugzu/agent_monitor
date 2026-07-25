"""
Adapter for Claude Code CLI AI Agent.
Handles Claude Code lifecycle events (Prompt sent, Thinking/Tool call, Waiting confirmation, Complete, Error).
"""

from typing import Dict, Any
from agent_monitor.adapters.base import (
    NormalizedAgentEvent,
    extract_request_id,
    extract_session_id,
    extract_tool_command,
    normalize_event_name,
)
from agent_monitor.adapters.session import SessionAwareAgentAdapter
from agent_monitor.core.states import AgentState

class ClaudeCodeAdapter(SessionAwareAgentAdapter):
    def __init__(self, agent_id: str = "claude_code", display_name: str = "Claude Code"):
        super().__init__(agent_id, display_name)

    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        # Claude Code native hooks use names such as UserPromptSubmit and
        # PermissionRequest plus snake_case payload fields.
        event = normalize_event_name(event_name)
        command = extract_tool_command(payload)
        msg = (
            command
            or payload.get("message")
            or payload.get("prompt")
            or payload.get("notification_message")
            or payload.get("error")
            or payload.get("tool_name")
            or ""
        )

        state = None
        opens_interaction = False
        if event in (
            "start", "prompt", "thinking", "toolcall", "tooluse",
            "userpromptsubmit", "pretooluse", "posttooluse",
        ):
            state = AgentState.THINKING
            msg = msg or "Claude is thinking..."
        elif event in (
            "ask", "waitapproval", "permissionrequest", "promptuser",
            "inputrequired", "notification",
        ):
            state = AgentState.WAITING_APPROVAL
            msg = msg or "Waiting for approval/input"
            opens_interaction = True
        elif event in ("stop", "finish", "done", "complete", "completed", "success"):
            state = AgentState.COMPLETED_UNREAD
            msg = msg or "Task completed"
        elif event in (
            "stopfailure", "posttoolusefailure", "permissiondenied",
            "error", "fail", "failed", "exception",
        ):
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
            phase=(
                "new_approval"
                if state == AgentState.WAITING_APPROVAL
                else "working" if state == AgentState.THINKING else ""
            ),
            interactive=opens_interaction,
            opens_interaction=opens_interaction,
            correlated_tool_event=event in ("pretooluse", "posttooluse"),
            completes_request=event == "posttooluse",
            payload=payload,
        )
