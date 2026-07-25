"""Agent-neutral approval interaction and event-conflict coordination."""

import logging
import time
from typing import Any, Callable, Dict, Optional

from agent_monitor.adapters.base import (
    BaseAgentAdapter,
    EventResult,
    NormalizedAgentEvent,
)
from agent_monitor.core.states import AgentState


class InteractionCoordinator:
    """Coordinate correlated tool events without knowing agent protocols."""

    def __init__(
        self,
        pending_interactions: Dict[str, Dict[str, Any]],
        approval_lifecycles: Dict[tuple, Dict[str, Any]],
        logger: Optional[logging.Logger] = None,
    ):
        self.pending_interactions = pending_interactions
        self.approval_lifecycles = approval_lifecycles
        self.logger = logger or logging.getLogger("InteractionCoordinator")

    def approval_lifecycle(
        self,
        agent_id: str,
        session_id: str,
        request_id: str,
    ) -> Optional[Dict[str, Any]]:
        exact = self.approval_lifecycles.get(
            (agent_id, session_id, request_id)
        )
        if exact:
            return exact

        # Some agent surfaces omit session identity on completion while
        # retaining a stable request/tool id.
        matches = [
            item
            for key, item in self.approval_lifecycles.items()
            if key[0] == agent_id and key[2] == request_id
        ]
        return max(matches, key=lambda item: item["sequence"]) if matches else None

    def accepts_event(
        self,
        agent_id: str,
        adapter: BaseAgentAdapter,
        event: NormalizedAgentEvent,
    ) -> bool:
        """Record correlated completion and reject unrelated tool conflicts."""
        if event.completes_request and event.request_id:
            lifecycle = self.approval_lifecycle(
                agent_id,
                event.session_id,
                event.request_id,
            )
            if lifecycle:
                lifecycle["phase"] = "completed"
                lifecycle["completed_at"] = time.time()
                self.logger.info(
                    "Approved operation completed: approval #%s for %s "
                    "session=%s request=%s",
                    lifecycle["sequence"],
                    agent_id,
                    event.session_id or "-",
                    event.request_id,
                )

        existing = self.pending_interactions.get(agent_id)
        if (
            event.correlated_tool_event
            and adapter.current_state == AgentState.WAITING_APPROVAL
            and existing
            and event.request_id
            and event.request_id != existing["request_id"]
        ):
            # Parallel tool activity must not dismiss a different approval.
            return False
        return True

    def sync_result(
        self,
        agent_id: str,
        adapter: BaseAgentAdapter,
        result: EventResult,
        create_interaction: Callable[[NormalizedAgentEvent], None],
    ) -> None:
        """Create or invalidate the pending menu after a state transition."""
        event = result.event
        if adapter.current_state != AgentState.WAITING_APPROVAL:
            self.pending_interactions.pop(agent_id, None)
            return

        existing = self.pending_interactions.get(agent_id)
        if (
            event.opens_interaction
            and (
                result.previous_state != AgentState.WAITING_APPROVAL
                or not existing
                or (
                    event.request_id
                    and existing["request_id"] != event.request_id
                )
            )
        ):
            create_interaction(event)
