"""
Unit tests for protocol payload formatting and state transitions.
"""

import unittest
import json
import asyncio
from unittest.mock import Mock, patch
from agent_monitor.core.states import AgentState, STATE_COLORS
from agent_monitor.core.hub import AgentMonitorHub
from agent_monitor.hardware.bridge import SerialBridge
from agent_monitor.hardware.ble_bridge import BLEBridge
from agent_monitor.hardware.protocol import encode_hardware_frame

class TestProtocol(unittest.TestCase):
    def test_state_colors(self):
        self.assertEqual(STATE_COLORS[AgentState.IDLE]["hex"], "#FFFDF6")
        self.assertEqual(STATE_COLORS[AgentState.THINKING]["hex"], "#A3CCDA")
        self.assertEqual(STATE_COLORS[AgentState.COMPLETED_UNREAD]["hex"], "#BDE3C3")
        self.assertEqual(STATE_COLORS[AgentState.WAITING_APPROVAL]["hex"], "#F8F7BA")
        self.assertEqual(STATE_COLORS[AgentState.ERROR]["hex"], "#F5D2D2")

    def test_hardware_payload_json_serializable(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "thinking", {"message": "Codex generating code..."})
        payload = hub.get_hardware_payload()

        # Check JSON serialization
        json_bytes = json.dumps(payload).encode("utf-8")
        parsed = json.loads(json_bytes.decode("utf-8"))

        self.assertEqual(parsed["cmd"], "SET_STATE")
        self.assertEqual(parsed["active"]["agent_id"], "codex")
        self.assertEqual(parsed["active"]["state"], "THINKING")
        self.assertEqual(parsed["active"]["color"], "#A3CCDA")

    def test_wire_frame_is_compact_and_utf8_safe(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "antigravity",
            "thinking",
            {"message": "正在分析一个很长的任务状态" * 20},
        )
        frame = encode_hardware_frame(hub.get_hardware_payload())
        parsed = json.loads(frame)

        self.assertLessEqual(len(frame), 256)
        self.assertEqual(parsed["cmd"], "SET_STATE")
        self.assertEqual(parsed["active"]["state"], "THINKING")
        self.assertNotIn("last_updated", parsed["active"])
        self.assertNotIn("rgb", parsed["active"])

    def test_waiting_interaction_is_included_in_wire_frame(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {"request_id": "wire-1", "message": "Approve command?"},
        )
        frame = encode_hardware_frame(hub.get_hardware_payload())
        parsed = json.loads(frame)

        self.assertEqual(parsed["interaction"]["request_id"], "wire-1")
        self.assertEqual(parsed["active"]["phase"], "new_approval")
        self.assertEqual(
            parsed["interaction"]["detail"],
            "Approve command?",
        )
        self.assertEqual(
            parsed["interaction"]["tool_name"],
            "Operation",
        )
        self.assertEqual(parsed["interaction"]["revision"], 0)
        self.assertEqual(parsed["interaction"]["actions"][0]["id"], "return")
        self.assertEqual(
            parsed["interaction"]["actions"][-1]["id"],
            "always_allow",
        )

    def test_approval_detail_is_longer_than_normal_status_message(self):
        hub = AgentMonitorHub()
        command = "printf '详细命令内容'; " * 40
        hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {
                "request_id": "wire-detail",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        )
        frame = encode_hardware_frame(hub.get_hardware_payload())
        parsed = json.loads(frame)

        self.assertEqual(parsed["interaction"]["tool_name"], "Bash")
        self.assertGreater(
            len(parsed["interaction"]["detail"].encode("utf-8")),
            len(parsed["active"]["message"].encode("utf-8")),
        )
        self.assertLessEqual(
            len(parsed["interaction"]["detail"].encode("utf-8")),
            320,
        )
        self.assertLess(len(frame), 2048)

    def test_return_changes_wire_frame_without_consuming_interaction(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {"request_id": "wire-return", "message": "Approve command?"},
        )
        before = encode_hardware_frame(hub.get_hardware_payload())

        self.assertTrue(
            hub.perform_active_action("wire-return", "return")
        )
        after = encode_hardware_frame(hub.get_hardware_payload())

        self.assertNotEqual(before, after)
        self.assertEqual(
            json.loads(after)["interaction"]["request_id"],
            "wire-return",
        )

    def test_serial_bridge_deduplicates_identical_frames(self):
        connection = Mock()
        connection.is_open = True
        bridge = SerialBridge()
        bridge.serial_conn = connection
        bridge.min_send_interval = 0
        payload = {
            "cmd": "SET_STATE",
            "active": {
                "display_name": "Claude Code",
                "state": "THINKING",
                "color": "#A3CCDA",
                "message": "Working",
                "unread": False,
            },
            "agents_count": 1,
        }

        bridge.send_data(payload)
        bridge.send_data(payload)

        connection.write.assert_called_once_with(
            encode_hardware_frame(payload)
        )

    @patch("agent_monitor.hardware.bridge.time.monotonic", return_value=10.0)
    def test_serial_bridge_sends_heartbeat_when_state_is_unchanged(self, _monotonic):
        connection = Mock()
        connection.is_open = True
        bridge = SerialBridge()
        bridge.serial_conn = connection
        bridge.last_frame = b'{"cmd":"SET_STATE"}\n'
        bridge.last_send_at = 0.0
        bridge.heartbeat_interval = 2.0

        bridge._send_heartbeat_if_due()

        connection.write.assert_called_once_with(b'{"cmd":"PING"}\n')
        connection.flush.assert_called_once()

    @patch("agent_monitor.hardware.bridge.time.sleep", side_effect=SystemExit)
    @patch("agent_monitor.hardware.bridge.serial.Serial")
    def test_serial_connect_replays_current_state(self, serial_cls, _sleep):
        connection = Mock()
        connection.is_open = True
        connection.readline.side_effect = OSError("disconnect")
        serial_cls.return_value = connection
        handler = Mock()
        connection_handler = Mock()
        bridge = SerialBridge(
            port="/dev/test",
            event_handler=handler,
            connection_handler=connection_handler,
        )
        bridge.running = True

        with self.assertRaises(SystemExit):
            bridge._run_loop()

        handler.assert_any_call({"event": "READY", "transport": "serial"})
        self.assertEqual(
            connection_handler.call_args_list,
            [unittest.mock.call(True), unittest.mock.call(False)],
        )

    @patch("agent_monitor.hardware.bridge.time.sleep", side_effect=SystemExit)
    @patch("agent_monitor.hardware.bridge.serial.Serial")
    def test_serial_bridge_parses_hardware_events(self, serial_cls, _sleep):
        connection = Mock()
        connection.is_open = True
        connection.readline.side_effect = [
            b'{"event":"KNOB_ROTATE","dir":1}\n',
            OSError("disconnect"),
        ]
        serial_cls.return_value = connection
        handler = Mock()
        bridge = SerialBridge(port="/dev/test", event_handler=handler)
        bridge.running = True

        with self.assertRaises(SystemExit):
            bridge._run_loop()

        handler.assert_any_call({"event": "KNOB_ROTATE", "dir": 1})

    def test_ble_bridge_reassembles_fragmented_notification(self):
        handler = Mock()
        bridge = BLEBridge(event_handler=handler)

        bridge._notification_handler(None, b'{"event":"KNOB_')
        bridge._notification_handler(None, b'PRESS"}\n')

        handler.assert_called_once_with({"event": "KNOB_PRESS"})

    def test_ble_bridge_splits_large_write(self):
        bridge = BLEBridge()
        bridge.client = Mock()
        bridge.client.is_connected = True
        bridge.client.write_gatt_char = Mock(
            side_effect=lambda *args, **kwargs: asyncio.sleep(0)
        )

        asyncio.run(bridge._async_write(b"x" * 45))

        chunks = [
            call.args[1]
            for call in bridge.client.write_gatt_char.call_args_list
        ]
        self.assertEqual([len(chunk) for chunk in chunks], [20, 20, 5])

    def test_ble_connection_notifications_are_deduplicated(self):
        connection_handler = Mock()
        bridge = BLEBridge(connection_handler=connection_handler)

        bridge._set_connected(True)
        bridge._set_connected(True)
        bridge._set_connected(False)

        self.assertEqual(
            connection_handler.call_args_list,
            [unittest.mock.call(True), unittest.mock.call(False)],
        )

if __name__ == "__main__":
    unittest.main()
