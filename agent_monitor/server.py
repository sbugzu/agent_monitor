"""
HTTP Server and API router for Agent Monitor host daemon.
Provides REST API endpoints for agent hookers and command line wrappers.
"""

import json
import logging
import select
import socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from agent_monitor.core.hub import AgentMonitorHub

logger = logging.getLogger("MonitorServer")

class APIHandler(BaseHTTPRequestHandler):
    hub: AgentMonitorHub = None
    CLAUDE_APPROVAL_TIMEOUT_SECONDS = 300

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_empty(self, status_code: int = 204):
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _client_disconnected(self) -> bool:
        """Detect a Claude hook request cancelled by its native approval UI."""
        connection = getattr(self, "connection", None)
        if connection is None:
            return False
        try:
            readable, _, _ = select.select([connection], [], [], 0)
            if not readable:
                return False
            return (
                connection.recv(
                    1,
                    socket.MSG_PEEK | socket.MSG_DONTWAIT,
                )
                == b""
            )
        except BlockingIOError:
            return False
        except OSError:
            return True

    def do_OPTIONS(self):
        self._send_json(200, {"status": "ok"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/status" or parsed.path == "/":
            payload = self.hub.get_hardware_payload()
            self._send_json(200, {"status": "success", "data": payload})
        elif parsed.path == "/api/v1/action":
            query = parse_qs(parsed.query)
            request_id = (query.get("request_id") or [""])[0]
            consume = (query.get("consume") or ["false"])[0].lower() in (
                "1", "true", "yes",
            )
            if not request_id:
                self._send_json(
                    400,
                    {"status": "error", "message": "request_id is required"},
                )
                return
            result = self.hub.get_action_result(
                request_id,
                consume=consume,
                # Polling hooks use short HTTP timeouts. Keep a delivered
                # decision available so a lost response can be retried.
                retain=consume,
            )
            if result:
                self._send_json(200, {"status": "success", "data": result})
            elif not self.hub.is_hardware_wait_available():
                self._send_json(
                    409,
                    {
                        "status": "hardware_offline",
                        "message": "No approval hardware is connected",
                    },
                )
            else:
                self._send_json(
                    404,
                    {"status": "pending", "message": "No action selected"},
                )
        else:
            self._send_json(404, {"status": "error", "message": "Endpoint not found"})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body = {}

        if self.path == "/api/v1/hooks/claude":
            event_name = body.get("hook_event_name") or "update"
            is_permission_request = event_name.lower() == "permissionrequest"
            if is_permission_request:
                body["actions"] = [
                    {"id": "reject", "label": "Reject"},
                    {"id": "allow_once", "label": "Allow Once"},
                ]
                if body.get("permission_suggestions"):
                    body["actions"].append({
                        "id": "always_allow",
                        "label": "Always Allow",
                        "dangerous": True,
                    })
            logger.info(
                "Received Claude Hook - Event: %s, Session: %s",
                event_name,
                body.get("session_id", "unknown"),
            )
            request_id = self.hub.dispatch_event(
                "claude_code",
                event_name,
                body,
                display_name="Claude Code",
            )
            if not is_permission_request or not request_id:
                self._send_empty()
                return

            result = self.hub.wait_for_action_result(
                request_id,
                self.CLAUDE_APPROVAL_TIMEOUT_SECONDS,
                cancelled=self._client_disconnected,
            )
            if not result:
                # Empty success leaves the native Claude permission dialog in
                # control when hardware is offline or the request times out.
                if not self._client_disconnected():
                    self._send_empty()
                return

            action_id = result["action_id"]
            decision = {
                "behavior": "deny" if action_id == "reject" else "allow",
            }
            if action_id == "reject":
                decision["message"] = "Rejected on Agent Monitor hardware."
            elif action_id == "always_allow":
                decision["updatedPermissions"] = body["permission_suggestions"]
            self._send_json(200, {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                },
            })

        elif self.path == "/api/v1/hooks/codex":
            event_name = body.get("hook_event_name") or "update"
            logger.info(
                "Received Codex Hook - Event: %s, Session: %s",
                event_name,
                body.get("session_id", "unknown"),
            )
            self.hub.dispatch_event(
                "codex",
                event_name,
                body,
                display_name="Codex Agent",
            )
            self._send_empty()

        elif self.path == "/api/v1/action/expire":
            request_id = str(body.get("request_id") or "")
            if not request_id:
                self._send_json(
                    400,
                    {"status": "error", "message": "request_id is required"},
                )
            elif self.hub.expire_interaction(request_id):
                self._send_json(200, {"status": "success"})
            else:
                self._send_json(
                    404,
                    {"status": "stale", "message": "Approval is no longer pending"},
                )

        elif self.path == "/api/v1/action/resolve":
            request_id = str(body.get("request_id") or "")
            approved = body.get("approved", True) is not False
            if not request_id:
                self._send_json(
                    400,
                    {"status": "error", "message": "request_id is required"},
                )
            elif self.hub.resolve_interaction_externally(
                request_id,
                approved=approved,
            ):
                self._send_json(200, {"status": "success"})
            else:
                self._send_json(
                    404,
                    {"status": "stale", "message": "Approval is no longer pending"},
                )

        elif self.path == "/api/v1/event":
            agent_id = body.get("agent") or body.get("agent_id") or "generic"
            event_name = body.get("event") or body.get("status") or "update"
            display_name = body.get("display_name")
            payload = body.get("payload") or body
            
            logger.info(f"Received API Event - Agent: {agent_id}, Event: {event_name}, Payload: {payload}")

            self.hub.dispatch_event(agent_id, event_name, payload, display_name=display_name)
            self._send_json(200, {"status": "success", "message": f"Event '{event_name}' dispatched to agent '{agent_id}'"})

        elif self.path == "/api/v1/ack":
            agent_id = body.get("agent") or body.get("agent_id")
            if agent_id and agent_id in self.hub.adapters:
                self.hub.adapters[agent_id].acknowledge_read()
                self.hub.notify_hardware()
            else:
                self.hub.acknowledge_active_agent()
            self._send_json(200, {"status": "success", "message": "Acknowledged read status"})

        elif self.path == "/api/v1/next":
            self.hub.next_agent()
            self._send_json(200, {"status": "success", "active_agent": self.hub.active_agent_id})

        elif self.path == "/api/v1/prev":
            self.hub.prev_agent()
            self._send_json(200, {"status": "success", "active_agent": self.hub.active_agent_id})

        else:
            self._send_json(404, {"status": "error", "message": "Unknown endpoint"})

    def log_message(self, format, *args):
        # Suppress standard HTTP request logging unless error
        pass

class MonitorServer:
    def __init__(self, hub: AgentMonitorHub, host: str = "127.0.0.1", port: int = 8765):
        self.hub = hub
        self.host = host
        self.port = port
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        APIHandler.hub = self.hub
        self.httpd = ThreadingHTTPServer((self.host, self.port), APIHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        logger.info(f"Agent Monitor Daemon API Server running at http://{self.host}:{self.port}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
