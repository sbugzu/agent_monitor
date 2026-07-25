"""
CLI Wrapper Adapter to wrap command execution (e.g., `agent-hook exec -- agent_cmd`).
Automatically sets THINKING when started, COMPLETED_UNREAD when exit code 0, ERROR when non-zero.
"""

from typing import Dict, Any
from agent_monitor.adapters.base import (
    BaseAgentAdapter,
    NormalizedAgentEvent,
    extract_request_id,
    extract_session_id,
    normalize_event_name,
)
from agent_monitor.core.states import AgentState

class CLIWrapperAdapter(BaseAgentAdapter):
    def __init__(self, agent_id: str = "cli_agent", display_name: str = "CLI Agent"):
        super().__init__(agent_id, display_name)

    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        event = normalize_event_name(event_name)
        cmd = payload.get("cmd") or payload.get("command") or ""
        code = payload.get("exit_code")

        state = None
        message = ""
        opens_interaction = False
        acknowledge = False
        if event == "start":
            state = AgentState.THINKING
            message = f"Running: {cmd}" if cmd else "Executing CLI agent..."
        elif event in ("waitinginput", "wait", "ask", "inputrequired"):
            state = AgentState.WAITING_APPROVAL
            message = f"Prompting: {cmd}"
            opens_interaction = True
        elif event == "exit":
            if code == 0:
                state = AgentState.COMPLETED_UNREAD
                message = f"Finished: {cmd}"
            else:
                state = AgentState.ERROR
                message = f"Failed (exit {code}): {cmd}"
        elif event in ("ack", "read"):
            acknowledge = True

        return NormalizedAgentEvent(
            name=event,
            state=state,
            message=message,
            session_id=extract_session_id(payload),
            request_id=extract_request_id(payload),
            phase=(
                "new_approval"
                if opens_interaction
                else "working" if state == AgentState.THINKING else ""
            ),
            interactive=opens_interaction,
            opens_interaction=opens_interaction,
            acknowledge=acknowledge,
            payload=payload,
        )
