"""
AgentMonitorHub - Central event & multi-agent state manager.
Maintains registered adapters, tracks active agent, and dispatches hardware frames.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from agent_monitor.adapters.base import BaseAgentAdapter, NormalizedAgentEvent
from agent_monitor.adapters.claude_code import ClaudeCodeAdapter
from agent_monitor.adapters.codex import CodexAdapter
from agent_monitor.adapters.generic_webhook import GenericWebhookAdapter
from agent_monitor.adapters.cli_wrapper import CLIWrapperAdapter
from agent_monitor.adapters.antigravity import AntigravityAdapter
from agent_monitor.core.interactions import InteractionCoordinator
from agent_monitor.core.states import AgentState

logger = logging.getLogger("AgentMonitorHub")

class AgentMonitorHub:
    HARDWARE_RECONNECT_GRACE_SECONDS = 15.0
    DEFAULT_WAITING_ACTIONS = (
        {"id": "reject", "label": "Reject", "dangerous": False},
        {"id": "allow_once", "label": "Allow Once", "dangerous": False},
        {"id": "always_allow", "label": "Always Allow", "dangerous": True},
    )
    INTERACTION_TTL_SECONDS = 300
    MAX_ACTION_RESULTS = 100

    def __init__(self):
        self.adapters: Dict[str, BaseAgentAdapter] = {}
        self.active_agent_id: Optional[str] = None
        self.hardware_callback = None
        self.hardware_connections: Dict[str, bool] = {}
        self.hardware_reconnect_deadline = 0.0
        self.pending_interactions: Dict[str, Dict[str, Any]] = {}
        self.queued_interactions: List[Dict[str, Any]] = []
        self.action_results: Dict[str, Dict[str, Any]] = {}
        self.approval_lifecycles: Dict[tuple, Dict[str, Any]] = {}
        self.interaction_coordinator = InteractionCoordinator(
            self.pending_interactions,
            self.approval_lifecycles,
            logger,
        )
        self._interaction_sequence = 0
        self._interaction_lock = threading.RLock()
        self._interaction_changed = threading.Condition(self._interaction_lock)

        # Pre-register standard adapters
        self.register_adapter(ClaudeCodeAdapter())
        self.register_adapter(CodexAdapter())
        self.register_adapter(CLIWrapperAdapter())
        self.register_adapter(AntigravityAdapter())

    def set_hardware_connection(self, transport: str, connected: bool):
        """Track physical transports so approval hooks can fail open offline."""
        transport = str(transport or "").strip().lower()
        if not transport:
            return
        with self._interaction_lock:
            connected = bool(connected)
            if self.hardware_connections.get(transport) == connected:
                return
            was_connected = any(self.hardware_connections.values())
            self.hardware_connections[transport] = connected
            is_connected = any(self.hardware_connections.values())
            if is_connected:
                self.hardware_reconnect_deadline = 0.0
            elif was_connected:
                self.hardware_reconnect_deadline = (
                    time.monotonic() + self.HARDWARE_RECONNECT_GRACE_SECONDS
                )
            self._interaction_changed.notify_all()
        logger.info(
            "Hardware transport %s: %s",
            transport,
            "online" if connected else "offline",
        )

    def is_hardware_connected(self) -> bool:
        with self._interaction_lock:
            return any(self.hardware_connections.values())

    def is_hardware_wait_available(self) -> bool:
        """Allow a brief BLE/USB reconnect without discarding an approval."""
        with self._interaction_lock:
            return (
                any(self.hardware_connections.values())
                or time.monotonic() < self.hardware_reconnect_deadline
            )

    def register_adapter(self, adapter: BaseAgentAdapter):
        self.adapters[adapter.agent_id] = adapter
        logger.info(f"Registered agent adapter: {adapter.agent_id} ({adapter.display_name})")

    def _visible_agent_ids(self) -> List[str]:
        """Running clients, plus event-driven agents without presence tracking."""
        return [
            agent_id
            for agent_id, adapter in self.adapters.items()
            if adapter.present or adapter.current_state != AgentState.IDLE
        ]

    def set_agent_presence(self, agent_id: str, present: bool):
        adapter = self.adapters.get(agent_id)
        if not adapter:
            return
        with self._interaction_lock:
            changed = adapter.update_presence(present)
            if not changed:
                return
            if not present:
                removed = self.pending_interactions.pop(agent_id, None)
                self.queued_interactions = [
                    interaction
                    for interaction in self.queued_interactions
                    if interaction["agent_id"] != agent_id
                ]
                if removed:
                    self._promote_next_waiting_locked()
        logger.info("Agent client %s: %s", agent_id, "online" if present else "offline")
        self.notify_hardware()

    def reconcile_external_states(self) -> bool:
        """Apply terminal state recorded outside the lifecycle hook stream."""
        with self._interaction_lock:
            changed = False
            for agent_id, adapter in self.adapters.items():
                if not adapter.reconcile_external_state():
                    continue
                changed = True
                if adapter.current_state != AgentState.WAITING_APPROVAL:
                    removed = self.pending_interactions.pop(agent_id, None)
                    if removed:
                        self._promote_next_waiting_locked()
                    self._interaction_changed.notify_all()
                logger.info(
                    "Reconciled external task state for %s: %s",
                    agent_id,
                    adapter.current_state.value,
                )
        if changed:
            self.notify_hardware()
        return changed

    def _resolve_active_agent(self) -> Optional[BaseAgentAdapter]:
        visible_ids = self._visible_agent_ids()
        if not visible_ids:
            self.active_agent_id = None
            return None

        if self.active_agent_id not in visible_ids:
            self.active_agent_id = max(
                visible_ids,
                key=lambda agent_id: self.adapters[agent_id].last_updated,
            )
        return self.adapters[self.active_agent_id]

    def get_or_create_adapter(self, agent_id: str, display_name: Optional[str] = None) -> BaseAgentAdapter:
        if agent_id not in self.adapters:
            adapter = GenericWebhookAdapter(agent_id, display_name)
            self.register_adapter(adapter)
        return self.adapters[agent_id]

    def set_active_agent(self, agent_id: str) -> bool:
        if (
            agent_id in self.adapters
            and self.adapters[agent_id].current_state != AgentState.IDLE
        ):
            self.active_agent_id = agent_id
            self.notify_hardware()
            return True
        return False

    def next_agent(self):
        """Cycles to the next agent (triggered by rotary knob)."""
        keys = self._visible_agent_ids()
        if not keys:
            self.active_agent_id = None
            self.notify_hardware()
            return
        if self.active_agent_id in keys:
            idx = (keys.index(self.active_agent_id) + 1) % len(keys)
            self.active_agent_id = keys[idx]
        else:
            self.active_agent_id = keys[0]
        logger.info(f"Switched active agent to: {self.active_agent_id}")
        self.notify_hardware()

    def prev_agent(self):
        """Cycles to the previous agent."""
        keys = self._visible_agent_ids()
        if not keys:
            self.active_agent_id = None
            self.notify_hardware()
            return
        if self.active_agent_id in keys:
            idx = (keys.index(self.active_agent_id) - 1) % len(keys)
            self.active_agent_id = keys[idx]
        else:
            self.active_agent_id = keys[0]
        logger.info(f"Switched active agent to: {self.active_agent_id}")
        self.notify_hardware()

    def acknowledge_active_agent(self):
        """Acknowledges unread completed task on active agent (triggered by knob press)."""
        if self.active_agent_id and self.active_agent_id in self.adapters:
            adapter = self.adapters[self.active_agent_id]
            adapter.acknowledge_read()
            self.notify_hardware()

    def _normalize_waiting_actions(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build a bounded action list, always reserving index zero for Return."""
        normalized = [{"id": "return", "label": "Return", "dangerous": False}]
        supplied = payload.get("actions")
        candidates = supplied if isinstance(supplied, list) else self.DEFAULT_WAITING_ACTIONS

        for candidate in candidates:
            if len(normalized) >= 6:
                break
            if isinstance(candidate, str):
                action_id = candidate.strip().lower().replace(" ", "_")
                label = candidate.strip()
                dangerous = False
            elif isinstance(candidate, dict):
                action_id = str(candidate.get("id") or "").strip()
                label = str(candidate.get("label") or action_id).strip()
                dangerous = bool(candidate.get("dangerous", False))
            else:
                continue
            if (
                not action_id
                or not label
                or action_id == "return"
                or any(action["id"] == action_id for action in normalized)
            ):
                continue
            normalized.append({
                "id": action_id[:32],
                "label": label[:48],
                "dangerous": dangerous,
            })
        return normalized

    def _create_waiting_interaction(
        self,
        agent_id: str,
        event: NormalizedAgentEvent,
    ) -> Dict[str, Any]:
        payload = event.payload
        self._interaction_sequence += 1
        request_id = str(
            event.request_id
            or f"{agent_id}-{self._interaction_sequence}"
        )[:64]
        interaction = {
            "request_id": request_id,
            "agent_id": agent_id,
            "session_id": event.session_id,
            "actions": self._normalize_waiting_actions(payload),
            "revision": 0,
            "created_at": time.time(),
            "expires_at": time.time() + self.INTERACTION_TTL_SECONDS,
            "sequence": self._interaction_sequence,
            "message": str(
                event.message
                or self.adapters[agent_id].last_message
                or payload.get("tool_name")
                or "approval"
            ),
            "tool_name": str(
                payload.get("tool_name")
                or payload.get("toolName")
                or "Operation"
            ),
        }
        self.pending_interactions[agent_id] = interaction
        self._record_waiting_lifecycle(interaction)
        return interaction

    def _record_waiting_lifecycle(
        self,
        interaction: Dict[str, Any],
    ) -> None:
        key = (
            interaction["agent_id"],
            interaction["session_id"],
            interaction["request_id"],
        )
        running = [
            item
            for lifecycle_key, item in self.approval_lifecycles.items()
            if lifecycle_key[:2] == key[:2]
            and lifecycle_key != key
            and item.get("phase") == "approved_running"
        ]
        lifecycle_interaction = {
            field: value
            for field, value in interaction.items()
            if not field.startswith("_")
        }
        self.approval_lifecycles[key] = {
            **lifecycle_interaction,
            "phase": "new_approval",
        }
        while len(self.approval_lifecycles) > self.MAX_ACTION_RESULTS:
            self.approval_lifecycles.pop(next(iter(self.approval_lifecycles)))
        if running:
            logger.info(
                "New approval #%s for %s session=%s request=%s while request=%s is executing",
                interaction["sequence"],
                interaction["agent_id"],
                interaction["session_id"] or "-",
                interaction["request_id"],
                running[-1]["request_id"],
            )
        else:
            logger.info(
                "New approval #%s for %s session=%s request=%s",
                interaction["sequence"],
                interaction["agent_id"],
                interaction["session_id"] or "-",
                interaction["request_id"],
            )

    def _current_waiting_interaction_locked(self) -> Optional[Dict[str, Any]]:
        """Return the one waiting request allowed to own the display."""
        if self.active_agent_id:
            current = self.pending_interactions.get(self.active_agent_id)
            if current:
                return current
        if not self.pending_interactions:
            return None
        return min(
            self.pending_interactions.values(),
            key=lambda interaction: interaction["sequence"],
        )

    def _queue_waiting_interaction(
        self,
        agent_id: str,
        event: NormalizedAgentEvent,
    ) -> Dict[str, Any]:
        """Queue a waiting event without changing its adapter's visible state."""
        request_id = str(event.request_id or "")
        duplicate = next(
            (
                interaction
                for interaction in (
                    list(self.pending_interactions.values())
                    + self.queued_interactions
                )
                if interaction["agent_id"] == agent_id
                and interaction.get("session_id") == event.session_id
                and request_id
                and interaction["request_id"] == request_id
            ),
            None,
        )
        if duplicate:
            return duplicate

        self._interaction_sequence += 1
        now = time.time()
        interaction = {
            "request_id": str(
                event.request_id
                or f"{agent_id}-{self._interaction_sequence}"
            )[:64],
            "agent_id": agent_id,
            "session_id": event.session_id,
            "actions": self._normalize_waiting_actions(event.payload),
            "revision": 0,
            "created_at": now,
            # The actionable timeout starts when this item reaches the display.
            "expires_at": 0.0,
            "sequence": self._interaction_sequence,
            "message": str(
                event.message
                or event.payload.get("tool_name")
                or "approval"
            ),
            "tool_name": str(
                event.payload.get("tool_name")
                or event.payload.get("toolName")
                or "Operation"
            ),
            "_event": event,
        }
        self.queued_interactions.append(interaction)
        self._record_waiting_lifecycle(interaction)
        lifecycle = self._approval_lifecycle(
            agent_id,
            interaction["session_id"],
            interaction["request_id"],
        )
        if lifecycle:
            lifecycle["phase"] = "queued"
        logger.info(
            "Queued approval #%s for %s behind the active waiting request",
            interaction["sequence"],
            agent_id,
        )
        return interaction

    def _promote_next_waiting_locked(self) -> Optional[Dict[str, Any]]:
        """Show the next queued waiting request after the current one closes."""
        if self.pending_interactions or not self.queued_interactions:
            return None
        interaction = self.queued_interactions.pop(0)
        event = interaction.pop("_event")
        interaction["expires_at"] = time.time() + self.INTERACTION_TTL_SECONDS
        adapter = self.adapters[interaction["agent_id"]]
        adapter.apply_event(event)
        self.pending_interactions[interaction["agent_id"]] = interaction
        self.active_agent_id = interaction["agent_id"]
        lifecycle = self._approval_lifecycle(
            interaction["agent_id"],
            interaction["session_id"],
            interaction["request_id"],
        )
        if lifecycle:
            lifecycle["phase"] = "new_approval"
            lifecycle["promoted_at"] = time.time()
            lifecycle["expires_at"] = interaction["expires_at"]
        logger.info(
            "Promoted queued approval #%s for %s session=%s request=%s",
            interaction["sequence"],
            interaction["agent_id"],
            interaction["session_id"] or "-",
            interaction["request_id"],
        )
        return interaction

    def _approval_lifecycle(
        self,
        agent_id: str,
        session_id: str,
        request_id: str,
    ) -> Optional[Dict[str, Any]]:
        return self.interaction_coordinator.approval_lifecycle(
            agent_id,
            session_id,
            request_id,
        )

    def _expire_interaction_locked(
        self,
        interaction: Dict[str, Any],
    ) -> None:
        agent_id = interaction["agent_id"]
        request_id = interaction["request_id"]
        session_id = interaction.get("session_id", "")
        if self.pending_interactions.get(agent_id) is interaction:
            self.pending_interactions.pop(agent_id, None)
        lifecycle = self._approval_lifecycle(
            agent_id,
            session_id,
            request_id,
        )
        if lifecycle:
            lifecycle["phase"] = "timed_out"
            lifecycle["timed_out_at"] = time.time()
        adapter = self.adapters.get(agent_id)
        if adapter:
            adapter.mark_approval_phase(
                session_id,
                request_id,
                "awaiting_input",
                "Approval timed out · awaiting input",
            )
        self._interaction_changed.notify_all()
        logger.info(
            "Approval #%s timed out for %s session=%s request=%s",
            interaction["sequence"],
            agent_id,
            session_id or "-",
            request_id,
        )
        self._promote_next_waiting_locked()

    def expire_interaction(self, request_id: str) -> bool:
        """Close an approval request whose blocking agent hook has timed out."""
        with self._interaction_lock:
            interaction = next(
                (
                    candidate
                    for candidate in self.pending_interactions.values()
                    if candidate["request_id"] == request_id
                ),
                None,
            )
            if not interaction:
                interaction = next(
                    (
                        candidate
                        for candidate in self.queued_interactions
                        if candidate["request_id"] == request_id
                    ),
                    None,
                )
            if not interaction:
                return False
            if interaction in self.queued_interactions:
                self.queued_interactions.remove(interaction)
                lifecycle = self._approval_lifecycle(
                    interaction["agent_id"],
                    interaction.get("session_id", ""),
                    request_id,
                )
                if lifecycle:
                    lifecycle["phase"] = "timed_out"
                    lifecycle["timed_out_at"] = time.time()
                self._interaction_changed.notify_all()
            else:
                self._expire_interaction_locked(interaction)
        self.notify_hardware()
        return True

    def _active_interaction(self) -> Optional[Dict[str, Any]]:
        if not self.active_agent_id:
            return None
        interaction = self.pending_interactions.get(self.active_agent_id)
        if not interaction:
            return None
        adapter = self.adapters.get(self.active_agent_id)
        visible_session_id = adapter.visible_session_id if adapter else None
        interaction_session_id = interaction.get("session_id")
        if not adapter or adapter.current_state != AgentState.WAITING_APPROVAL:
            self.pending_interactions.pop(self.active_agent_id, None)
            return None
        if interaction["expires_at"] <= time.time():
            self._expire_interaction_locked(interaction)
            return None
        if (
            visible_session_id
            and interaction_session_id
            and visible_session_id != interaction_session_id
        ):
            return None
        return interaction

    def perform_active_action(self, request_id: str, action_id: str) -> bool:
        """Record one validated hardware choice for the active waiting request."""
        with self._interaction_lock:
            interaction = self._active_interaction()
            if not interaction or interaction["request_id"] != request_id:
                logger.warning("Rejected stale hardware action for request %s", request_id)
                return False
            action = next(
                (
                    candidate
                    for candidate in interaction["actions"]
                    if candidate["id"] == action_id
                ),
                None,
            )
            if not action or request_id in self.action_results:
                logger.warning(
                    "Rejected invalid or duplicate hardware action %s for %s",
                    action_id,
                    request_id,
                )
                return False

            if action["id"] == "return":
                # Return only closes the hardware menu. Keep the request
                # pending so a subsequent press can reopen it and submit a
                # real approval decision. Bump the wire revision so transports
                # replay the interaction even when using older firmware that
                # submitted Return before disabling its local menu.
                interaction["revision"] += 1
                logger.info(
                    "Hardware returned from menu for %s (%s)",
                    interaction["agent_id"],
                    request_id,
                )
                self.notify_hardware()
                return True

            result = {
                "request_id": request_id,
                "agent_id": interaction["agent_id"],
                "action_id": action["id"],
                "label": action["label"],
                "selected_at": time.time(),
                "session_id": interaction["session_id"],
                "sequence": interaction["sequence"],
                "message": interaction["message"],
            }
            self.action_results[request_id] = result
            self._interaction_changed.notify_all()
            while len(self.action_results) > self.MAX_ACTION_RESULTS:
                self.action_results.pop(next(iter(self.action_results)))
            self.pending_interactions.pop(interaction["agent_id"], None)

            adapter = self.adapters[interaction["agent_id"]]
            lifecycle = self._approval_lifecycle(
                interaction["agent_id"],
                interaction["session_id"],
                request_id,
            )
            if lifecycle:
                lifecycle["phase"] = "approval_selected"
                lifecycle["selected_at"] = result["selected_at"]
            selected_message = (
                f"Selected {action['label']} · sending to "
                f"{adapter.display_name}"
            )
            adapter.mark_approval_phase(
                interaction["session_id"],
                request_id,
                "approval_selected",
                selected_message,
            )
            self._promote_next_waiting_locked()

        logger.info(
            "Approval #%s selected on hardware: %s for %s session=%s request=%s",
            interaction["sequence"],
            action_id,
            interaction["agent_id"],
            interaction["session_id"] or "-",
            request_id,
        )
        self.notify_hardware()
        return True

    def resolve_interaction_externally(
        self,
        request_id: str,
        approved: bool = True,
    ) -> bool:
        """Close a request answered in the agent's own approval UI."""
        with self._interaction_lock:
            interaction = next(
                (
                    candidate
                    for candidate in self.pending_interactions.values()
                    if candidate["request_id"] == request_id
                ),
                None,
            )
            queued = False
            if not interaction:
                interaction = next(
                    (
                        candidate
                        for candidate in self.queued_interactions
                        if candidate["request_id"] == request_id
                    ),
                    None,
                )
                queued = interaction is not None
            if not interaction:
                return False

            if queued:
                self.queued_interactions.remove(interaction)
            else:
                self.pending_interactions.pop(interaction["agent_id"], None)

            phase = "approved_running" if approved else "approval_rejected"
            lifecycle = self._approval_lifecycle(
                interaction["agent_id"],
                interaction.get("session_id", ""),
                request_id,
            )
            if lifecycle:
                lifecycle["phase"] = phase
                lifecycle["resolved_externally_at"] = time.time()

            if not queued:
                adapter = self.adapters.get(interaction["agent_id"])
                if adapter:
                    adapter.mark_approval_phase(
                        interaction.get("session_id", ""),
                        request_id,
                        phase,
                        (
                            f"Approved in {adapter.display_name} · running "
                            f"{interaction.get('message') or 'operation'}"
                            if approved
                            else f"Rejected in {adapter.display_name}"
                        ),
                    )
                self._promote_next_waiting_locked()
            self._interaction_changed.notify_all()

        logger.info(
            "Approval #%s resolved in agent UI: %s for %s request=%s",
            interaction["sequence"],
            "approved" if approved else "rejected",
            interaction["agent_id"],
            request_id,
        )
        self.notify_hardware()
        return True

    def get_action_result(
        self,
        request_id: str,
        consume: bool = False,
        retain: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return a hardware decision, optionally recording its delivery."""
        consumed = None
        with self._interaction_lock:
            result = self.action_results.get(request_id)
            if result and consume:
                if not retain:
                    result = self.action_results.pop(request_id)
                # HTTP delivery must be idempotent: a client may time out after
                # the server has processed the GET but before it receives the
                # response. Retained results let its next poll recover the
                # exact same decision.
                if not result.get("delivered_at"):
                    result["delivered_at"] = time.time()
                    consumed = result
                    lifecycle = self._approval_lifecycle(
                        result["agent_id"],
                        result.get("session_id", ""),
                        request_id,
                    )
                    phase = (
                        "approval_rejected"
                        if result["action_id"] == "reject"
                        else "approved_running"
                    )
                    if lifecycle:
                        lifecycle["phase"] = phase
                        lifecycle["consumed_at"] = result["delivered_at"]
                    adapter = self.adapters.get(result["agent_id"])
                    if adapter:
                        adapter.mark_approval_phase(
                            result.get("session_id", ""),
                            request_id,
                            phase,
                            (
                                "Approval rejected"
                                if phase == "approval_rejected"
                                else f"Approved · running {result.get('message') or 'operation'}"
                            ),
                        )
                    logger.info(
                        "Approval #%s consumed by agent: %s for %s session=%s request=%s; phase=%s",
                        result.get("sequence", "-"),
                        result["action_id"],
                        result["agent_id"],
                        result.get("session_id") or "-",
                        request_id,
                        phase,
                    )
            copied = dict(result) if result else None
        if consumed:
            self.notify_hardware()
        return copied

    def wait_for_action_result(
        self,
        request_id: str,
        timeout: float,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Wait for and consume one hardware decision from a blocking hook."""
        deadline = None
        externally_resolved = False
        with self._interaction_changed:
            while request_id not in self.action_results:
                if cancelled and cancelled():
                    externally_resolved = True
                    break
                is_active = any(
                    interaction["request_id"] == request_id
                    for interaction in self.pending_interactions.values()
                )
                is_queued = any(
                    interaction["request_id"] == request_id
                    for interaction in self.queued_interactions
                )
                if not is_active and not is_queued:
                    break
                if not self.is_hardware_wait_available():
                    break
                # Queued requests receive their full decision window only
                # after the preceding waiting item has left the display.
                if is_queued:
                    self._interaction_changed.wait(
                        0.25 if cancelled else None
                    )
                    continue
                if deadline is None:
                    deadline = time.monotonic() + max(0.0, timeout)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_for = min(remaining, 0.25) if cancelled else remaining
                if not any(self.hardware_connections.values()):
                    reconnect_remaining = (
                        self.hardware_reconnect_deadline - time.monotonic()
                    )
                    if reconnect_remaining <= 0:
                        break
                    wait_for = min(wait_for, reconnect_remaining)
                self._interaction_changed.wait(wait_for)

        result = self.get_action_result(request_id, consume=True)
        if result:
            return result
        if externally_resolved:
            self.resolve_interaction_externally(request_id, approved=True)
            return None
        self.expire_interaction(request_id)
        return None

    def dispatch_event(self, agent_id: str, event_name: str, payload: Dict[str, Any], display_name: Optional[str] = None):
        if agent_id not in self.adapters and (
            event_name.lower() in ("start", "exit", "waiting_input")
            and any(key in payload for key in ("cmd", "command", "exit_code"))
        ):
            adapter = CLIWrapperAdapter(agent_id, display_name or agent_id.capitalize())
            self.register_adapter(adapter)
        else:
            adapter = self.get_or_create_adapter(agent_id, display_name)
            if display_name:
                adapter.display_name = display_name
        with self._interaction_lock:
            event = adapter.translate_event(event_name, payload)
            current = self._current_waiting_interaction_locked()

            if event.opens_interaction and event.state == AgentState.WAITING_APPROVAL:
                if current:
                    interaction = self._queue_waiting_interaction(
                        agent_id,
                        event,
                    )
                    self._interaction_changed.notify_all()
                    request_id = interaction["request_id"]
                    # A queued wait must not change either the current agent or
                    # the state currently rendered on the hardware.
                    self.notify_hardware()
                    return request_id

            if (
                current
                and current["agent_id"] == agent_id
                and (
                    not event.session_id
                    or not current.get("session_id")
                    or event.session_id == current.get("session_id")
                )
                and event.state is not None
                and event.state != AgentState.WAITING_APPROVAL
                and not (
                    event.completes_request
                    and event.request_id == current["request_id"]
                )
                and not (
                    event.name in ("permissiondenied", "posttoolusefailure")
                    and event.request_id == current["request_id"]
                )
            ):
                # Until the current wait is answered or expires, lifecycle
                # noise for the same task cannot take over its display.
                return current["request_id"]

            if not self.interaction_coordinator.accepts_event(
                agent_id,
                adapter,
                event,
            ):
                return None

            result = adapter.apply_event(event)
            self.interaction_coordinator.sync_result(
                agent_id,
                adapter,
                result,
                lambda normalized: self._create_waiting_interaction(
                    agent_id,
                    normalized,
                ),
            )
            if (
                current
                and current not in self.pending_interactions.values()
            ):
                self._promote_next_waiting_locked()

            # Any meaningful lifecycle state makes the reporting agent visible and
            # active. IDLE adapters remain registered internally but stay hidden.
            if (
                adapter.current_state != AgentState.IDLE
                and not self._current_waiting_interaction_locked()
            ):
                self.active_agent_id = agent_id
            elif event.opens_interaction:
                self.active_agent_id = agent_id
            self._interaction_changed.notify_all()
            interaction = self.pending_interactions.get(agent_id)
            request_id = (
                interaction["request_id"]
                if interaction
                and interaction.get("session_id") == event.session_id
                else None
            )

        self.notify_hardware()
        return request_id

    def get_hardware_payload(self) -> Dict[str, Any]:
        """Generates hardware display frame dictionary."""
        active_adapter = self._resolve_active_agent()

        if active_adapter:
            active_info = active_adapter.to_dict()
        else:
            active_info = {
                "agent_id": "none",
                "display_name": "No Active",
                "state": AgentState.IDLE.value,
                "color": "#FFFDF6",
                "rgb": [255, 255, 255],
                "message": "Standing by",
                "unread": False,
                "last_updated": 0
            }

        payload = {
            "cmd": "SET_STATE",
            "active": active_info,
            "agents_count": len(self._visible_agent_ids()),
            "hardware_connected": self.is_hardware_connected(),
        }
        with self._interaction_lock:
            interaction = self._active_interaction()
            if interaction:
                active_info["phase"] = "new_approval"
                payload["interaction"] = {
                    "request_id": interaction["request_id"],
                    "actions": interaction["actions"],
                    "revision": interaction["revision"],
                    "detail": interaction["message"],
                    "tool_name": interaction["tool_name"],
                }
        return payload

    def register_hardware_callback(self, callback):
        self.hardware_callback = callback

    def notify_hardware(self):
        if self.hardware_callback:
            payload = self.get_hardware_payload()
            try:
                self.hardware_callback(payload)
            except Exception as e:
                logger.error(f"Failed to send update to hardware: {e}")
