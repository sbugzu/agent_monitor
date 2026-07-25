"""
Adapter for Codex AI Agent.
Handles Codex agent lifecycle events (Execution start, thinking, approval needed, finished, error).
"""

import json
import os
import time
from typing import Dict, Any, Optional
from agent_monitor.adapters.base import (
    NormalizedAgentEvent,
    extract_request_id,
    extract_session_id,
    extract_tool_command,
    normalize_event_name,
)
from agent_monitor.adapters.session import SessionAwareAgentAdapter
from agent_monitor.core.states import AgentState

class CodexAdapter(SessionAwareAgentAdapter):
    TRANSCRIPT_TAIL_BYTES = 1024 * 1024

    def __init__(self, agent_id: str = "codex", display_name: str = "Codex Agent"):
        super().__init__(agent_id, display_name)

    @classmethod
    def _transcript_terminal_event(
        cls,
        transcript_path: str,
        turn_id: str,
    ) -> Optional[str]:
        """Return the persisted terminal event for one Codex turn, if present."""
        if not transcript_path or not turn_id or not os.path.isfile(transcript_path):
            return None
        terminal_event = None
        try:
            size = os.path.getsize(transcript_path)
            with open(transcript_path, "rb") as stream:
                stream.seek(max(0, size - cls.TRANSCRIPT_TAIL_BYTES))
                if size > cls.TRANSCRIPT_TAIL_BYTES:
                    stream.readline()
                for raw_line in stream:
                    if (
                        b"turn_aborted" not in raw_line
                        and b"task_complete" not in raw_line
                    ):
                        continue
                    record = json.loads(raw_line)
                    payload = record.get("payload") or {}
                    if (
                        record.get("type") == "event_msg"
                        and payload.get("type") in (
                            "turn_aborted",
                            "task_complete",
                        )
                        and str(payload.get("turn_id") or "") == turn_id
                    ):
                        terminal_event = str(payload["type"])
        except (OSError, UnicodeError, json.JSONDecodeError):
            # Transcript is a best-effort Codex extension. A malformed or
            # concurrently-written line must not disrupt normal hook handling.
            return None
        return terminal_event

    @classmethod
    def _transcript_has_aborted_turn(
        cls,
        transcript_path: str,
        turn_id: str,
    ) -> bool:
        """Compatibility helper retained for focused abort checks and tests."""
        return (
            cls._transcript_terminal_event(transcript_path, turn_id)
            == "turn_aborted"
        )

    def reconcile_external_state(self) -> bool:
        """Reconcile active turns when Codex App omits a terminal lifecycle hook."""
        if super().reconcile_external_state():
            return True

        terminal_events = {}
        for session_id, item in self.session_states.items():
            if item["state"] not in (
                AgentState.THINKING,
                AgentState.WAITING_APPROVAL,
            ):
                continue
            event = self._transcript_terminal_event(
                item.get("transcript_path", ""),
                item.get("turn_id", ""),
            )
            if event:
                terminal_events[session_id] = event

        changed = False
        for session_id, event in terminal_events.items():
            item = self.session_states[session_id]
            if event == "turn_aborted":
                del self.session_states[session_id]
                changed = True
            elif (
                event == "task_complete"
                and item.get("terminal_event") != "task_complete"
            ):
                # Codex App labels a completed turn as awaiting the next user
                # response, but currently does not always emit a Stop hook.
                # This is a display-only wait and must not create an approval
                # action menu.
                item["state"] = AgentState.WAITING_APPROVAL
                item["message"] = "Codex awaiting input"
                item["updated_at"] = time.time()
                item["interactive"] = False
                item["phase"] = "awaiting_input"
                item["terminal_event"] = "task_complete"
                changed = True

        if not changed:
            return False
        self._refresh_visible_state(empty_message="Codex task interrupted")
        return True

    def reconcile_aborted_sessions(self) -> bool:
        """Backward-compatible alias for the previous public method name."""
        return self.reconcile_external_state()

    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        event = normalize_event_name(event_name)
        command = extract_tool_command(payload)
        tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
        description = (
            tool_input.get("description")
            if isinstance(tool_input, dict)
            else ""
        )
        msg = (
            command
            or description
            or payload.get("message")
            or payload.get("task")
            or payload.get("prompt")
            or payload.get("error")
            or payload.get("tool_name")
            or ""
        )

        state = None
        opens_interaction = False
        correlated_tool_event = event in ("pretooluse", "posttooluse")
        completes_request = event == "posttooluse"
        if event == "posttooluse" and (
            payload.get("error")
            or payload.get("tool_error")
            or payload.get("is_error")
        ):
            state = AgentState.ERROR
            msg = str(msg or "Tool failed")
        elif event in (
            "run", "running", "start", "executing", "thinking", "generating",
            "userpromptsubmit", "pretooluse", "posttooluse",
        ):
            state = AgentState.THINKING
            msg = msg or "Codex generating code..."
        elif event in (
            "approval", "confirm", "waituser", "input",
            "permissionrequest",
        ):
            state = AgentState.WAITING_APPROVAL
            msg = msg or "Codex awaiting approval"
            opens_interaction = True
        elif event in ("stop", "complete", "done", "success", "finished"):
            state = AgentState.COMPLETED_UNREAD
            msg = msg or "Codex task finished"
        elif event in ("error", "failed", "crash"):
            state = AgentState.ERROR
            msg = msg or "Codex error"
        elif event in ("idle", "ack"):
            return NormalizedAgentEvent(
                name=event,
                acknowledge=True,
                session_id=extract_session_id(payload),
                request_id=extract_request_id(payload),
                payload=payload,
            )

        if state == AgentState.WAITING_APPROVAL:
            phase = "new_approval"
        elif state == AgentState.THINKING:
            phase = "working"
        else:
            phase = ""
        return NormalizedAgentEvent(
            name=event,
            state=state,
            message=str(msg),
            session_id=extract_session_id(payload),
            request_id=extract_request_id(payload),
            phase=phase,
            interactive=opens_interaction,
            opens_interaction=opens_interaction,
            correlated_tool_event=correlated_tool_event,
            completes_request=completes_request,
            payload=payload,
        )
