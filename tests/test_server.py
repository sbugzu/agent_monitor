"""HTTP API behavior used by blocking approval hooks."""

import unittest

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

    def test_pending_action_reports_offline_hardware(self):
        status, body = self.get_action("offline-request")
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "hardware_offline")

    def test_pending_action_remains_pending_when_hardware_is_online(self):
        self.hub.set_hardware_connection("serial", True)

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


if __name__ == "__main__":
    unittest.main()
