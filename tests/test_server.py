"""HTTP API behavior used by blocking approval hooks."""

import io
import json
import unittest
from unittest.mock import patch

from agent_monitor.core.hub import AgentMonitorHub
from agent_monitor.server import APIHandler


class TestServer(unittest.TestCase):
    def setUp(self):
        self.hub = AgentMonitorHub()
        self.responses = []

    def get_action(self, request_id):
        handler = APIHandler.__new__(APIHandler)
        handler.hub = self.hub
        handler.path = (
            f"/api/v1/action?request_id={request_id}&consume=true"
        )
        handler._send_json = lambda status, body: self.responses.append(
            (status, body)
        )
        handler.do_GET()
        return self.responses[-1]

    def expire_action(self, request_id):
        handler = APIHandler.__new__(APIHandler)
        handler.hub = self.hub
        handler.path = "/api/v1/action/expire"
        body = ('{"request_id": "' + request_id + '"}').encode("utf-8")
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._send_json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        handler.do_POST()
        return self.responses[-1]

    def resolve_action(self, request_id, approved=True):
        handler = APIHandler.__new__(APIHandler)
        handler.hub = self.hub
        handler.path = "/api/v1/action/resolve"
        body = json.dumps({
            "request_id": request_id,
            "approved": approved,
        }).encode("utf-8")
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._send_json = lambda status, payload: self.responses.append(
            (status, payload)
        )
        handler.do_POST()
        return self.responses[-1]

    def post_claude_hook(self, payload):
        handler = APIHandler.__new__(APIHandler)
        handler.hub = self.hub
        handler.path = "/api/v1/hooks/claude"
        body = json.dumps(payload).encode("utf-8")
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._send_json = lambda status, response: self.responses.append(
            (status, response)
        )
        handler._send_empty = lambda status=204: self.responses.append(
            (status, None)
        )
        handler.do_POST()
        return self.responses[-1]

    def test_pending_action_reports_offline_hardware(self):
        status, body = self.get_action("offline-request")
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "hardware_offline")

    def test_pending_action_remains_pending_when_hardware_is_online(self):
        self.hub.set_hardware_connection("serial", True)

        status, _ = self.get_action("pending-request")
        self.assertEqual(status, 404)

    def test_pending_action_remains_pending_during_reconnect_grace(self):
        self.hub.set_hardware_connection("ble", True)
        self.hub.set_hardware_connection("ble", False)

        status, _ = self.get_action("pending-request")

        self.assertEqual(status, 404)

    def test_selected_action_can_be_consumed_after_disconnect(self):
        self.hub.set_hardware_connection("serial", True)
        self.hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {
                "request_id": "selected-request",
                "message": "Approve command?",
            },
        )
        self.assertTrue(
            self.hub.perform_active_action(
                "selected-request",
                "allow_once",
            )
        )
        self.hub.set_hardware_connection("serial", False)

        status, body = self.get_action("selected-request")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["action_id"], "allow_once")

        # A lost HTTP response can be retried without losing the hardware
        # decision or applying its lifecycle transition twice.
        status, retry_body = self.get_action("selected-request")
        self.assertEqual(status, 200)
        self.assertEqual(retry_body["data"]["action_id"], "allow_once")

    def test_hook_timeout_expires_pending_hardware_action(self):
        self.hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {
                "session_id": "timeout-task",
                "request_id": "timeout-request",
                "message": "Approve command?",
            },
        )

        status, _ = self.expire_action("timeout-request")
        payload = self.hub.get_hardware_payload()

        self.assertEqual(status, 200)
        self.assertNotIn("interaction", payload)
        self.assertEqual(payload["active"]["phase"], "awaiting_input")
        self.assertFalse(
            self.hub.perform_active_action(
                "timeout-request",
                "allow_once",
            )
        )

    def test_native_app_can_resolve_pending_hardware_action(self):
        self.hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {
                "session_id": "native-task",
                "request_id": "native-request",
                "message": "Approve command?",
            },
        )

        status, _ = self.resolve_action("native-request")
        payload = self.hub.get_hardware_payload()

        self.assertEqual(status, 200)
        self.assertNotIn("interaction", payload)
        self.assertEqual(payload["active"]["state"], "THINKING")
        self.assertEqual(payload["active"]["phase"], "approved_running")

    def test_claude_permission_request_returns_hardware_allow(self):
        with patch.object(
            self.hub,
            "wait_for_action_result",
            return_value={"action_id": "allow_once"},
        ) as wait:
            status, body = self.post_claude_hook({
                "hook_event_name": "PermissionRequest",
                "session_id": "claude-task",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            })

        request_id = self.hub.pending_interactions["claude_code"]["request_id"]
        wait.assert_called_once_with(
            request_id,
            APIHandler.CLAUDE_APPROVAL_TIMEOUT_SECONDS,
            cancelled=unittest.mock.ANY,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["hookSpecificOutput"]["decision"]["behavior"],
            "allow",
        )

    def test_claude_always_allow_echoes_official_permission_suggestion(self):
        suggestion = {
            "type": "addRules",
            "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
            "behavior": "allow",
            "destination": "localSettings",
        }
        with patch.object(
            self.hub,
            "wait_for_action_result",
            return_value={"action_id": "always_allow"},
        ):
            status, body = self.post_claude_hook({
                "hook_event_name": "PermissionRequest",
                "session_id": "claude-task",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
                "permission_suggestions": [suggestion],
            })

        self.assertEqual(status, 200)
        self.assertEqual(
            body["hookSpecificOutput"]["decision"]["updatedPermissions"],
            [suggestion],
        )
        actions = self.hub.pending_interactions["claude_code"]["actions"]
        self.assertIn("always_allow", [action["id"] for action in actions])

    def test_claude_hardware_timeout_falls_back_to_native_dialog(self):
        with patch.object(
            self.hub,
            "wait_for_action_result",
            return_value=None,
        ):
            status, body = self.post_claude_hook({
                "hook_event_name": "PermissionRequest",
                "session_id": "claude-task",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            })

        self.assertEqual(status, 204)
        self.assertIsNone(body)


if __name__ == "__main__":
    unittest.main()
