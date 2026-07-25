"""
AgentMonitorHub - Central event & multi-agent state manager.
Maintains registered adapters, tracks active agent, and dispatches hardware frames.
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Any
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
        self.pending_interactions: Dict[str, Dict[str, Any]] = {}
        self.action_results: Dict[str, Dict[str, Any]] = {}
        self.approval_lifecycles: Dict[tuple, Dict[str, Any]] = {}
        self.interaction_coordinator = InteractionCoordinator(
            self.pending_interactions,
            self.approval_lifecycles,
            logger,
        )
        self._interaction_sequence = 0
        self._interaction_lock = threading.RLock()

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
            self.hardware_connections[transport] = connected
        logger.info(
            "Hardware transport %s: %s",
            transport,
            "online" if connected else "offline",
        )

    def is_hardware_connected(self) -> bool:
        with self._interaction_lock:
            return any(self.hardware_connections.values())

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
                self.pending_interactions.pop(agent_id, None)
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
                    self.pending_interactions.pop(agent_id, None)
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
        key = (
            agent_id,
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
        self.approval_lifecycles[key] = {
            **interaction,
            "phase": "new_approval",
        }
        while len(self.approval_lifecycles) > self.MAX_ACTION_RESULTS:
            self.approval_lifecycles.pop(next(iter(self.approval_lifecycles)))
        if running:
            logger.info(
                "New approval #%s for %s session=%s request=%s while request=%s is executing",
                interaction["sequence"],
                agent_id,
                interaction["session_id"] or "-",
                interaction["request_id"],
                running[-1]["request_id"],
            )
        else:
            logger.info(
                "New approval #%s for %s session=%s request=%s",
                interaction["sequence"],
                agent_id,
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

    def _active_interaction(self) -> Optional[Dict[str, Any]]:
        if not self.active_agent_id:
            return None
        interaction = self.pending_interactions.get(self.active_agent_id)
        if not interaction:
            return None
        adapter = self.adapters.get(self.active_agent_id)
        visible_session_id = adapter.visible_session_id if adapter else None
        interaction_session_id = interaction.get("session_id")
        if (
            not adapter
            or adapter.current_state != AgentState.WAITING_APPROVAL
            or interaction["expires_at"] <= time.time()
        ):
            self.pending_interactions.pop(self.active_agent_id, None)
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

    def get_action_result(
        self,
        request_id: str,
        consume: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return a hardware decision for custom agents, optionally consuming it."""
        consumed = None
        with self._interaction_lock:
            result = self.action_results.get(request_id)
            if result and consume:
                result = self.action_results.pop(request_id)
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
                    lifecycle["consumed_at"] = time.time()
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
            if not self.interaction_coordinator.accepts_event(
                agent_id,
                adapter,
                event,
            ):
                return

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

            # Any meaningful lifecycle state makes the reporting agent visible and
            # active. IDLE adapters remain registered internally but stay hidden.
            if adapter.current_state != AgentState.IDLE:
                self.active_agent_id = agent_id

        self.notify_hardware()

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
