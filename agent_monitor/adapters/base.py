"""
Base class for Agent Hooker Adapters.
Every supported AI Agent adapter (Codex, Claude Code, Webhook, CLI) extends BaseAgentAdapter.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time
from typing import Dict, Any, Optional
from agent_monitor.core.states import AgentState, STATE_COLORS

PHASE_COLORS = {
    "new_approval": {"hex": "#FFB454", "rgb": (255, 180, 84)},
    "awaiting_input": {"hex": "#FFB454", "rgb": (255, 180, 84)},
    "approval_selected": {"hex": "#FFB454", "rgb": (255, 180, 84)},
    "approved_running": {"hex": "#FFB454", "rgb": (255, 180, 84)},
    "approval_rejected": {"hex": "#F2A7A7", "rgb": (242, 167, 167)},
}

def extract_tool_command(payload: Dict[str, Any]) -> str:
    """Extract the concrete shell command from native agent hook payloads."""
    for key in (
        "command", "cmd", "shell_command", "script",
        # Antigravity run_command uses toolCall.args.CommandLine.
        "CommandLine", "commandLine",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return " ".join(str(part) for part in value)

    for key in (
        "tool_input", "toolInput", "tool_args", "toolArgs",
        "arguments", "args", "input", "tool_call", "toolCall",
    ):
        nested = payload.get(key)
        if isinstance(nested, dict):
            command = extract_tool_command(nested)
            if command:
                return command
    return ""


def normalize_event_name(event_name: str) -> str:
    """Normalize lifecycle event spellings used by native and custom hooks."""
    return str(event_name or "").lower().replace("_", "").replace("-", "")


def extract_session_id(payload: Dict[str, Any]) -> str:
    """Extract a stable task identity from common agent hook payloads."""
    for key in (
        "session_id", "sessionId", "thread_id", "threadId",
        "conversation_id", "conversationId", "task_id", "taskId",
        "turn_id", "turnId",
    ):
        value = payload.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


def extract_request_id(payload: Dict[str, Any]) -> str:
    """Extract a stable approval/tool identity from common hook payloads."""
    for key in (
        "monitor_request_id", "request_id", "requestId", "tool_use_id",
        "toolUseId", "turn_id", "turnId",
    ):
        value = payload.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


@dataclass(frozen=True)
class NormalizedAgentEvent:
    """Agent-neutral lifecycle event consumed by the shared state engine."""

    name: str
    state: Optional[AgentState] = None
    message: str = ""
    session_id: str = ""
    request_id: str = ""
    phase: str = ""
    interactive: bool = False
    opens_interaction: bool = False
    correlated_tool_event: bool = False
    completes_request: bool = False
    acknowledge: bool = False
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventResult:
    """State-engine result used by the hub without inspecting raw hook fields."""

    event: NormalizedAgentEvent
    previous_state: AgentState
    current_state: AgentState

    @property
    def state_changed(self) -> bool:
        return self.previous_state != self.current_state


class BaseAgentAdapter(ABC):
    COMPLETED_IDLE_TIMEOUT_SECONDS = 60.0

    def __init__(self, agent_id: str, display_name: str):
        self.agent_id: str = agent_id
        self.display_name: str = display_name
        self.current_state: AgentState = AgentState.IDLE
        self.last_message: str = "Standing by"
        self.last_updated: float = time.time()
        self.unread: bool = False
        self.present: bool = False
        self.presence_managed: bool = False
        self.display_phase: str = ""

    @property
    def visible_session_id(self) -> Optional[str]:
        """Session currently represented by this adapter, if it tracks sessions."""
        return None

    def update_state(self, state: AgentState, message: Optional[str] = None):
        """Updates agent state and handles unread flag logic."""
        import logging
        logger = logging.getLogger("BaseAdapter")
        
        old_state = self.current_state
        self.current_state = state
        self.last_updated = time.time()
        if message:
            self.last_message = message
            
        if old_state != state:
            logger.info(f"[{self.agent_id}] State changed: {old_state.name} -> {state.name}, message: {message}")

        if state == AgentState.COMPLETED_UNREAD:
            self.unread = True
        elif state != AgentState.COMPLETED_UNREAD:
            self.unread = False

    def update_presence(self, present: bool) -> bool:
        """Apply client presence without leaking adapter internals into the hub."""
        present = bool(present)
        changed = self.present != present
        self.presence_managed = True
        self.present = present
        if not changed:
            return False

        self.last_updated = time.time()
        if present and self.current_state == AgentState.IDLE:
            self.last_message = "Client connected"
        elif not present:
            self.reset_state("Client disconnected")
        return True

    def reset_state(self, message: str = "Standing by") -> None:
        """Clear transient lifecycle state when a client disconnects."""
        self.current_state = AgentState.IDLE
        self.unread = False
        self.display_phase = ""
        self.last_message = message
        self.last_updated = time.time()

    def acknowledge_read(self):
        """Acknowledges green unread state and transitions agent back to IDLE."""
        if self.current_state == AgentState.COMPLETED_UNREAD or self.unread:
            self.unread = False
            self.current_state = AgentState.IDLE
            self.last_message = "Read & Acknowledged"
            self.last_updated = time.time()

    def mark_approval_phase(
        self,
        session_id: str,
        request_id: str,
        phase: str,
        message: str,
    ) -> bool:
        """Update the common single-session approval presentation."""
        self.display_phase = phase
        if phase in ("approved_running", "approval_rejected"):
            self.update_state(AgentState.THINKING, message=message)
        else:
            self.last_message = message
            self.last_updated = time.time()
        return True

    def apply_event(self, event: NormalizedAgentEvent) -> EventResult:
        """Apply one normalized event to the common single-session state."""
        previous_state = self.current_state
        if event.acknowledge:
            self.acknowledge_read()
        elif event.state is not None:
            self.update_state(event.state, message=event.message)
            self.display_phase = event.phase
        return EventResult(event, previous_state, self.current_state)

    def handle_event(self, event_name: str, payload: Dict[str, Any]) -> EventResult:
        """Translate a native hook event and apply it through the shared engine."""
        return self.apply_event(self.translate_event(event_name, payload))

    def reconcile_external_state(self) -> bool:
        """Auto-clear an unread completion after one quiet minute."""
        if (
            self.current_state != AgentState.COMPLETED_UNREAD
            or time.time() - self.last_updated
            < self.COMPLETED_IDLE_TIMEOUT_SECONDS
        ):
            return False
        self.reset_state("Standing by")
        return True

    def to_dict(self) -> Dict[str, Any]:
        color_info = PHASE_COLORS.get(
            self.display_phase,
            STATE_COLORS.get(self.current_state, STATE_COLORS[AgentState.IDLE]),
        )
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "state": self.current_state.value,
            "color": color_info["hex"],
            "rgb": color_info["rgb"],
            "message": self.last_message,
            "phase": self.display_phase,
            "unread": self.unread,
            "last_updated": self.last_updated
        }

    @abstractmethod
    def translate_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
    ) -> NormalizedAgentEvent:
        """Translate an agent-specific hook payload into the common contract."""
        pass
