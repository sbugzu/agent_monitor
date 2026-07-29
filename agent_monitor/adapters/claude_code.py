"""
Adapter for Claude Code CLI AI Agent.
Handles Claude Code lifecycle events (Prompt sent, Thinking/Tool call, Waiting confirmation, Complete, Error).
"""

import json
import os
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
    TRANSCRIPT_TAIL_BYTES = 1024 * 1024
    INTERRUPT_MARKERS = frozenset((
        "[Request interrupted by user]",
        "Request interrupted by user",
        "[Request interrupted by user for tool use]",
    ))

    def __init__(self, agent_id: str = "claude_code", display_name: str = "Claude Code"):
        super().__init__(agent_id, display_name)
        self._tool_request_ids = {}

    @staticmethod
    def _tool_signature(payload: Dict[str, Any]) -> str:
        """Build the correlation key shared by Claude tool hook payloads."""
        tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
        try:
            encoded_input = json.dumps(
                tool_input,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            encoded_input = repr(tool_input)
        return (
            f"{extract_session_id(payload)}\0"
            f"{payload.get('tool_name') or payload.get('toolName') or ''}\0"
            f"{encoded_input}"
        )

    @classmethod
    def _transcript_has_user_interrupt(
        cls,
        transcript_path: str,
        start_offset: int,
    ) -> bool:
        """Detect Claude's persisted marker for an interrupted active turn."""
        if not transcript_path or not os.path.isfile(transcript_path):
            return False
        try:
            size = os.path.getsize(transcript_path)
            if start_offset < 0 or start_offset > size:
                return False
            scan_from = max(start_offset, size - cls.TRANSCRIPT_TAIL_BYTES)
            latest_user_text_is_interrupt = None
            with open(transcript_path, "rb") as stream:
                stream.seek(scan_from)
                if scan_from > start_offset:
                    stream.readline()
                for raw_line in stream:
                    record = json.loads(raw_line)
                    message = record.get("message") or {}
                    if (
                        record.get("type") != "user"
                        or not isinstance(message, dict)
                        or message.get("role") != "user"
                    ):
                        continue
                    content = message.get("content")
                    if isinstance(content, str):
                        texts = [content]
                    elif isinstance(content, list):
                        texts = [
                            item.get("text", "")
                            for item in content
                            if isinstance(item, dict)
                            and item.get("type") == "text"
                            and isinstance(item.get("text"), str)
                        ]
                    else:
                        texts = []
                    for text in texts:
                        latest_user_text_is_interrupt = (
                            text.strip() in cls.INTERRUPT_MARKERS
                        )
            return latest_user_text_is_interrupt is True
        except (OSError, UnicodeError, json.JSONDecodeError):
            # Transcripts are written concurrently. An incomplete trailing
            # record should simply be checked again on the next monitor poll.
            return False

    def apply_event(self, event: NormalizedAgentEvent):
        result = super().apply_event(event)
        if (
            event.session_id
            and event.state in (
                AgentState.THINKING,
                AgentState.WAITING_APPROVAL,
            )
        ):
            item = self.session_states.get(event.session_id)
            transcript_path = item.get("transcript_path", "") if item else ""
            if (
                item is not None
                and transcript_path
                and (
                    event.name == "userpromptsubmit"
                    or "transcript_reconcile_offset" not in item
                )
            ):
                try:
                    item["transcript_reconcile_offset"] = os.path.getsize(
                        transcript_path
                    )
                except OSError:
                    pass
        return result

    def reconcile_external_state(self) -> bool:
        """Clear turns interrupted by the user, for which Stop never fires."""
        if super().reconcile_external_state():
            return True

        interrupted_sessions = [
            session_id
            for session_id, item in self.session_states.items()
            if item["state"] in (
                AgentState.THINKING,
                AgentState.WAITING_APPROVAL,
            )
            and self._transcript_has_user_interrupt(
                item.get("transcript_path", ""),
                int(item.get("transcript_reconcile_offset", 0)),
            )
        ]
        if not interrupted_sessions:
            return False

        for session_id in interrupted_sessions:
            del self.session_states[session_id]
        self._refresh_visible_state(empty_message="Claude task interrupted")
        return True

    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        # Claude Code native hooks use names such as UserPromptSubmit and
        # PermissionRequest plus snake_case payload fields.
        event = normalize_event_name(event_name)
        request_id = extract_request_id(payload)
        if event == "pretooluse" and request_id:
            self._tool_request_ids[self._tool_signature(payload)] = request_id
            while len(self._tool_request_ids) > 100:
                self._tool_request_ids.pop(next(iter(self._tool_request_ids)))
        elif event in ("permissionrequest", "permissiondenied") and not request_id:
            # Claude PermissionRequest deliberately omits tool_use_id. Recover
            # it from the preceding PreToolUse event so App-side decisions and
            # PostToolUse can close the same hardware interaction.
            request_id = self._tool_request_ids.get(
                self._tool_signature(payload),
                "",
            )
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
                request_id=request_id,
                payload=payload,
            )

        return NormalizedAgentEvent(
            name=event,
            state=state,
            message=str(msg),
            session_id=extract_session_id(payload),
            request_id=request_id,
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
