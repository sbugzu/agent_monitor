"""
Unit tests for AI Agent Adapters and Monitor Hub logic.
"""

import unittest
import io
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
from agent_monitor.core.states import AgentState
from agent_monitor.adapters.claude_code import ClaudeCodeAdapter
from agent_monitor.adapters.codex import CodexAdapter
from agent_monitor.adapters.generic_webhook import GenericWebhookAdapter
from agent_monitor.adapters.cli_wrapper import CLIWrapperAdapter
from agent_monitor.adapters.antigravity import AntigravityAdapter
from agent_monitor.adapters.session import SessionAwareAgentAdapter
from agent_monitor.core.hub import AgentMonitorHub
from agent_monitor.core.process_monitor import AgentProcessMonitor

CODEX_HOOK = SourceFileLoader(
    "agent_monitor_codex_hook",
    str(Path(__file__).resolve().parents[1] / "bin" / "codex-hook"),
).load_module()
ANTIGRAVITY_HOOK = SourceFileLoader(
    "agent_monitor_antigravity_hook",
    str(Path(__file__).resolve().parents[1] / "bin" / "antigravity-hook"),
).load_module()

class TestAdapters(unittest.TestCase):
    def test_claude_code_adapter(self):
        adapter = ClaudeCodeAdapter()
        self.assertEqual(adapter.current_state, AgentState.IDLE)

        # 1. Thinking / Tool call
        adapter.handle_event("thinking", {"message": "Analyzing repository"})
        self.assertEqual(adapter.current_state, AgentState.THINKING)
        self.assertEqual(adapter.last_message, "Analyzing repository")

        # 2. Waiting approval
        adapter.handle_event("ask", {"message": "Permission to edit file?"})
        self.assertEqual(adapter.current_state, AgentState.WAITING_APPROVAL)

        # 3. Finished (Unread)
        adapter.handle_event("done", {"message": "Changes complete"})
        self.assertEqual(adapter.current_state, AgentState.COMPLETED_UNREAD)
        self.assertTrue(adapter.unread)

        # 4. Acknowledge read
        adapter.acknowledge_read()
        self.assertEqual(adapter.current_state, AgentState.IDLE)
        self.assertFalse(adapter.unread)

    def test_claude_code_native_http_hook_events(self):
        adapter = ClaudeCodeAdapter()
        adapter.handle_event("UserPromptSubmit", {"prompt": "Fix the tests"})
        self.assertEqual(adapter.current_state, AgentState.THINKING)

        adapter.handle_event("PermissionRequest", {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q tests"},
        })
        self.assertEqual(adapter.current_state, AgentState.WAITING_APPROVAL)
        self.assertEqual(adapter.last_message, "pytest -q tests")

        adapter.handle_event("Stop", {"stop_hook_active": False})
        self.assertEqual(adapter.current_state, AgentState.COMPLETED_UNREAD)

        adapter.handle_event("PostToolUseFailure", {
            "tool_name": "Bash",
            "error": "exit status 1",
        })
        self.assertEqual(adapter.current_state, AgentState.ERROR)
        self.assertFalse(adapter.unread)

    def test_claude_user_interrupt_reconciles_without_stop_hook(self):
        adapter = ClaudeCodeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "claude.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Start work",
                    },
                }) + "\n",
                encoding="utf-8",
            )
            adapter.handle_event("UserPromptSubmit", {
                "session_id": "claude-task",
                "transcript_path": str(transcript),
                "prompt": "Start work",
            })
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "..."}],
                    },
                }) + "\n")
                stream.write(json.dumps({
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "[Request interrupted by user]",
                        }],
                    },
                }) + "\n")

            self.assertTrue(adapter.reconcile_external_state())

        self.assertEqual(adapter.current_state, AgentState.IDLE)
        self.assertEqual(adapter.last_message, "Claude task interrupted")
        self.assertEqual(adapter.session_states, {})

    def test_claude_old_interrupt_does_not_cancel_new_prompt(self):
        adapter = ClaudeCodeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "claude.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "[Request interrupted by user]",
                        }],
                    },
                }) + "\n",
                encoding="utf-8",
            )
            adapter.handle_event("UserPromptSubmit", {
                "session_id": "claude-task",
                "transcript_path": str(transcript),
                "prompt": "Try again",
            })

            self.assertFalse(adapter.reconcile_external_state())

        self.assertEqual(adapter.current_state, AgentState.THINKING)

    def test_claude_interrupt_removes_only_matching_parallel_session(self):
        adapter = ClaudeCodeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for session_id in ("interrupted", "running"):
                transcript = Path(directory) / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                paths[session_id] = transcript
                adapter.handle_event("UserPromptSubmit", {
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "prompt": session_id,
                })

            with paths["interrupted"].open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "[Request interrupted by user for tool use]",
                        }],
                    },
                }) + "\n")

            self.assertTrue(adapter.reconcile_external_state())

        self.assertNotIn("interrupted", adapter.session_states)
        self.assertIn("running", adapter.session_states)
        self.assertEqual(adapter.current_state, AgentState.THINKING)

    def test_claude_interrupt_releases_blocking_permission_hook(self):
        hub = AgentMonitorHub()
        hub.set_hardware_connection("serial", True)
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "claude.jsonl"
            transcript.write_text("", encoding="utf-8")
            request_id = hub.dispatch_event(
                "claude_code",
                "PermissionRequest",
                {
                    "session_id": "claude-task",
                    "transcript_path": str(transcript),
                    "tool_name": "Bash",
                    "tool_input": {"command": "npm test"},
                },
            )
            result = {}
            waiter = threading.Thread(
                target=lambda: result.setdefault(
                    "action",
                    hub.wait_for_action_result(request_id, timeout=5),
                )
            )
            waiter.start()
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "[Request interrupted by user]",
                        }],
                    },
                }) + "\n")

            self.assertTrue(hub.reconcile_external_states())
            waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertIsNone(result["action"])
        self.assertNotIn("claude_code", hub.pending_interactions)
        self.assertEqual(
            hub.adapters["claude_code"].current_state,
            AgentState.IDLE,
        )

    def test_claude_app_approval_posttool_closes_hardware_wait(self):
        hub = AgentMonitorHub()
        tool_payload = {
            "session_id": "claude-task",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        hub.dispatch_event("claude_code", "PreToolUse", tool_payload)
        request_id = hub.dispatch_event(
            "claude_code",
            "PermissionRequest",
            {
                key: value
                for key, value in tool_payload.items()
                if key != "tool_use_id"
            },
        )

        self.assertEqual(request_id, "tool-1")
        self.assertEqual(
            hub.get_hardware_payload()["active"]["state"],
            "WAITING_APPROVAL",
        )

        hub.dispatch_event("claude_code", "PostToolUse", tool_payload)

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "THINKING")
        self.assertEqual(payload["active"]["phase"], "working")
        self.assertNotIn("interaction", payload)
        self.assertEqual(
            hub.approval_lifecycles[
                ("claude_code", "claude-task", "tool-1")
            ]["phase"],
            "completed",
        )

    def test_claude_cancelled_hook_marks_app_approval_as_running(self):
        hub = AgentMonitorHub()
        hub.set_hardware_connection("serial", True)
        request_id = hub.dispatch_event(
            "claude_code",
            "PermissionRequest",
            {
                "session_id": "claude-task",
                "request_id": "tool-1",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            },
        )

        result = hub.wait_for_action_result(
            request_id,
            timeout=300,
            cancelled=lambda: True,
        )

        self.assertIsNone(result)
        payload = hub.get_hardware_payload()
        self.assertNotIn("interaction", payload)
        self.assertEqual(payload["active"]["state"], "THINKING")
        self.assertEqual(payload["active"]["phase"], "approved_running")
        self.assertEqual(
            hub.approval_lifecycles[
                ("claude_code", "claude-task", "tool-1")
            ]["phase"],
            "approved_running",
        )

    def test_claude_permission_denied_closes_matching_wait(self):
        hub = AgentMonitorHub()
        tool_payload = {
            "session_id": "claude-task",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        hub.dispatch_event("claude_code", "PreToolUse", tool_payload)
        hub.dispatch_event(
            "claude_code",
            "PermissionRequest",
            {
                key: value
                for key, value in tool_payload.items()
                if key != "tool_use_id"
            },
        )

        hub.dispatch_event(
            "claude_code",
            "PermissionDenied",
            tool_payload,
        )

        payload = hub.get_hardware_payload()
        self.assertNotIn("interaction", payload)
        self.assertEqual(payload["active"]["state"], "ERROR")

    def test_claude_permission_denial_correlates_without_tool_use_id(self):
        adapter = ClaudeCodeAdapter()
        tool_payload = {
            "session_id": "claude-task",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        adapter.translate_event("PreToolUse", tool_payload)

        denied = adapter.translate_event(
            "PermissionDenied",
            {
                key: value
                for key, value in tool_payload.items()
                if key != "tool_use_id"
            },
        )

        self.assertEqual(denied.request_id, "tool-1")
        self.assertEqual(denied.state, AgentState.ERROR)

    def test_codex_adapter(self):
        adapter = CodexAdapter()
        adapter.handle_event("running", {"task": "Writing test suite"})
        self.assertEqual(adapter.current_state, AgentState.THINKING)

        adapter.handle_event("error", {"task": "Syntax error in file"})
        self.assertEqual(adapter.current_state, AgentState.ERROR)

    def test_codex_native_hook_events(self):
        adapter = CodexAdapter()
        adapter.handle_event("UserPromptSubmit", {"prompt": "Review changes"})
        self.assertEqual(adapter.current_state, AgentState.THINKING)

        adapter.handle_event("PermissionRequest", {
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
        })
        self.assertEqual(adapter.current_state, AgentState.WAITING_APPROVAL)
        self.assertEqual(adapter.last_message, "git status --short")

        adapter.handle_event("Stop", {})
        self.assertEqual(adapter.current_state, AgentState.COMPLETED_UNREAD)

        adapter.handle_event("PostToolUse", {
            "tool_name": "Bash",
            "error": "exit status 1",
        })
        self.assertEqual(adapter.current_state, AgentState.ERROR)

    def test_codex_permission_request_exposes_operation_to_hardware(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "PermissionRequest", {
            "turn_id": "turn-1",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {
                "command": "npm run test",
                "description": "Run the test suite",
            },
        })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(payload["active"]["message"], "npm run test")
        self.assertEqual(payload["active"]["phase"], "new_approval")
        self.assertEqual(payload["active"]["color"], "#FFB454")
        self.assertEqual(payload["interaction"]["request_id"], "tool-1")

    def test_codex_permission_request_uses_human_description(self):
        adapter = CodexAdapter()
        adapter.handle_event("PermissionRequest", {
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"description": "Create GitHub issue"},
        })

        self.assertEqual(adapter.current_state, AgentState.WAITING_APPROVAL)
        self.assertEqual(adapter.last_message, "Create GitHub issue")

    def test_codex_hook_maps_hardware_actions_to_permission_decisions(self):
        allow = CODEX_HOOK.permission_decision("allow_once")
        deny = CODEX_HOOK.permission_decision("reject")

        self.assertEqual(
            allow["hookSpecificOutput"]["decision"]["behavior"],
            "allow",
        )
        self.assertEqual(
            deny["hookSpecificOutput"]["decision"]["behavior"],
            "deny",
        )
        self.assertIsNone(CODEX_HOOK.permission_decision("return"))
        self.assertIsNone(CODEX_HOOK.permission_decision(None))

    def test_codex_cancelled_hook_resolves_native_app_approval(self):
        payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-task",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        with (
            patch.object(CODEX_HOOK, "post_event", return_value=True),
            patch.object(CODEX_HOOK, "install_cancellation_handler"),
            patch.object(
                CODEX_HOOK,
                "wait_for_hardware_action",
                side_effect=CODEX_HOOK.ApprovalHookCancelled,
            ),
            patch.object(CODEX_HOOK, "resolve_hardware_request") as resolve,
            patch.object(CODEX_HOOK.json, "load", return_value=payload),
        ):
            CODEX_HOOK.main()

        resolve.assert_called_once_with("tool-1")

    def test_codex_hook_uses_five_minute_approval_window(self):
        self.assertEqual(CODEX_HOOK.APPROVAL_TIMEOUT, 300)

    def test_codex_hook_checks_once_more_at_timeout_boundary(self):
        with (
            patch.object(CODEX_HOOK, "APPROVAL_TIMEOUT", 0),
            patch.object(
                CODEX_HOOK,
                "fetch_hardware_action",
                return_value="allow_once",
            ) as fetch,
        ):
            action = CODEX_HOOK.wait_for_hardware_action("boundary-request")

        self.assertEqual(action, "allow_once")
        fetch.assert_called_once()

    def test_codex_hook_retries_transient_poll_failure(self):
        with (
            patch.object(CODEX_HOOK, "APPROVAL_TIMEOUT", 10),
            patch.object(
                CODEX_HOOK,
                "fetch_hardware_action",
                side_effect=[OSError("transient"), "allow_once"],
            ) as fetch,
            patch.object(CODEX_HOOK.time, "sleep"),
        ):
            action = CODEX_HOOK.wait_for_hardware_action("retry-request")

        self.assertEqual(action, "allow_once")
        self.assertEqual(fetch.call_count, 2)

    def test_codex_hook_expires_request_after_timeout(self):
        with (
            patch.object(CODEX_HOOK, "APPROVAL_TIMEOUT", 0),
            patch.object(
                CODEX_HOOK,
                "fetch_hardware_action",
                return_value=None,
            ),
            patch.object(CODEX_HOOK, "expire_hardware_request") as expire,
        ):
            action = CODEX_HOOK.wait_for_hardware_action("timeout-request")

        self.assertIsNone(action)
        expire.assert_called_once_with("timeout-request")

    def test_codex_hook_returns_immediately_when_hardware_is_offline(self):
        with (
            patch.object(
                CODEX_HOOK,
                "fetch_hardware_action",
                return_value=CODEX_HOOK.HARDWARE_OFFLINE,
            ) as fetch,
            patch.object(CODEX_HOOK.time, "sleep") as sleep,
        ):
            action = CODEX_HOOK.wait_for_hardware_action("offline-request")

        self.assertIsNone(action)
        fetch.assert_called_once()
        sleep.assert_not_called()

    def test_hardware_approve_after_timeout_is_rejected_and_status_converges(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "PermissionRequest", {
            "session_id": "timed-out-task",
            "tool_use_id": "timed-out-approval",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        })
        interaction = hub.pending_interactions["codex"]

        with patch(
            "agent_monitor.core.hub.time.time",
            return_value=interaction["expires_at"],
        ):
            self.assertFalse(
                hub.perform_active_action(
                    "timed-out-approval",
                    "allow_once",
                )
            )
            payload = hub.get_hardware_payload()

        self.assertNotIn("interaction", payload)
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(payload["active"]["phase"], "awaiting_input")
        self.assertEqual(
            payload["active"]["message"],
            "Approval timed out · awaiting input",
        )
        self.assertNotIn("timed-out-approval", hub.action_results)
        self.assertEqual(
            hub.approval_lifecycles[
                ("codex", "timed-out-task", "timed-out-approval")
            ]["phase"],
            "timed_out",
        )

    def test_hardware_connection_status_uses_any_online_transport(self):
        hub = AgentMonitorHub()
        self.assertFalse(hub.is_hardware_connected())

        hub.set_hardware_connection("serial", True)
        hub.set_hardware_connection("ble", False)
        self.assertTrue(hub.is_hardware_connected())
        self.assertTrue(hub.get_hardware_payload()["hardware_connected"])

        hub.set_hardware_connection("serial", False)
        self.assertFalse(hub.is_hardware_connected())

    @patch("agent_monitor.core.hub.time.monotonic")
    def test_transient_hardware_disconnect_gets_reconnect_grace(self, monotonic):
        monotonic.return_value = 100.0
        hub = AgentMonitorHub()
        hub.set_hardware_connection("ble", True)
        hub.set_hardware_connection("ble", False)

        self.assertFalse(hub.is_hardware_connected())
        self.assertTrue(hub.is_hardware_wait_available())

        monotonic.return_value = (
            100.0 + hub.HARDWARE_RECONNECT_GRACE_SECONDS + 0.01
        )
        self.assertFalse(hub.is_hardware_wait_available())

    def test_blocking_hook_consumes_preselected_hardware_action(self):
        hub = AgentMonitorHub()
        request_id = hub.dispatch_event(
            "claude_code",
            "PermissionRequest",
            {
                "session_id": "claude-task",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            },
        )
        self.assertTrue(
            hub.perform_active_action(request_id, "allow_once")
        )

        result = hub.wait_for_action_result(request_id, timeout=0)

        self.assertEqual(result["action_id"], "allow_once")
        self.assertNotIn(request_id, hub.action_results)
        self.assertEqual(
            hub.get_hardware_payload()["active"]["phase"],
            "approved_running",
        )

    def test_codex_new_tool_use_queues_behind_waiting_interaction(self):
        hub = AgentMonitorHub()
        for tool_use_id, command in (
            ("tool-1", "npm test"),
            ("tool-2", "npm run lint"),
        ):
            hub.dispatch_event("codex", "PermissionRequest", {
                "turn_id": "turn-1",
                "tool_use_id": tool_use_id,
                "tool_name": "Bash",
                "tool_input": {"command": command},
            })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["interaction"]["request_id"], "tool-1")
        self.assertEqual(payload["active"]["message"], "npm test")

        self.assertTrue(hub.perform_active_action("tool-1", "allow_once"))
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["interaction"]["request_id"], "tool-2")
        self.assertEqual(payload["active"]["message"], "npm run lint")

    def test_codex_completed_session_does_not_hide_waiting_session(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "PermissionRequest", {
            "session_id": "waiting-task",
            "tool_use_id": "approval-tool",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        })
        hub.dispatch_event("codex", "Stop", {
            "session_id": "completed-task",
            "message": "Other task finished",
        })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(payload["active"]["message"], "npm test")
        self.assertEqual(
            payload["interaction"]["request_id"],
            "approval-tool",
        )

    def test_codex_aborted_session_is_removed_from_hardware(self):
        hub = AgentMonitorHub()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            hub.dispatch_event("codex", "UserPromptSubmit", {
                "session_id": "aborted-task",
                "turn_id": "aborted-turn",
                "transcript_path": str(transcript),
                "prompt": "Work until interrupted",
            })
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_aborted",
                        "turn_id": "aborted-turn",
                        "reason": "interrupted",
                    },
                }) + "\n")

            self.assertTrue(hub.reconcile_external_states())

        self.assertEqual(
            hub.adapters["codex"].current_state,
            AgentState.IDLE,
        )
        self.assertEqual(hub.get_hardware_payload()["agents_count"], 0)

    def test_codex_task_complete_without_stop_becomes_awaiting(self):
        hub = AgentMonitorHub()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            hub.dispatch_event("codex", "UserPromptSubmit", {
                "session_id": "awaiting-task",
                "turn_id": "finished-turn",
                "transcript_path": str(transcript),
                "prompt": "Finish this task",
            })
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "finished-turn",
                        "last_agent_message": "Work is ready for review.",
                    },
                }) + "\n")

            self.assertTrue(hub.reconcile_external_states())
            self.assertFalse(hub.reconcile_external_states())

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(payload["active"]["message"], "Codex awaiting input")
        self.assertEqual(payload["active"]["phase"], "awaiting_input")
        self.assertEqual(payload["active"]["color"], "#FFB454")
        self.assertNotIn("interaction", payload)

    def test_codex_approval_tracks_selected_consumed_and_new_request(self):
        hub = AgentMonitorHub()
        first = {
            "session_id": "task-1",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        hub.dispatch_event("codex", "PermissionRequest", first)
        self.assertTrue(hub.perform_active_action("tool-1", "allow_once"))

        selected = hub.get_hardware_payload()
        self.assertEqual(selected["active"]["phase"], "approval_selected")
        self.assertNotIn("interaction", selected)

        result = hub.get_action_result("tool-1", consume=True)
        self.assertEqual(result["action_id"], "allow_once")
        running = hub.get_hardware_payload()
        self.assertEqual(running["active"]["state"], "THINKING")
        self.assertEqual(running["active"]["phase"], "approved_running")
        self.assertEqual(running["active"]["color"], "#FFB454")

        hub.dispatch_event("codex", "PermissionRequest", {
            **first,
            "tool_use_id": "tool-2",
            "tool_input": {"command": "npm run lint"},
        })
        new_request = hub.get_hardware_payload()
        self.assertEqual(new_request["active"]["phase"], "new_approval")
        self.assertEqual(
            new_request["interaction"]["request_id"],
            "tool-2",
        )
        self.assertEqual(
            hub.approval_lifecycles[
                ("codex", "task-1", "tool-1")
            ]["phase"],
            "approved_running",
        )

    def test_codex_post_tool_use_completes_matching_approval_only(self):
        hub = AgentMonitorHub()
        payload = {
            "session_id": "task-1",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        hub.dispatch_event("codex", "PermissionRequest", payload)
        hub.perform_active_action("tool-1", "allow_once")
        hub.get_action_result("tool-1", consume=True)
        hub.dispatch_event("codex", "PostToolUse", payload)

        self.assertEqual(
            hub.approval_lifecycles[
                ("codex", "task-1", "tool-1")
            ]["phase"],
            "completed",
        )
        self.assertEqual(
            hub.get_hardware_payload()["active"]["phase"],
            "working",
        )

    def test_codex_actionable_approval_outranks_display_only_awaiting(self):
        hub = AgentMonitorHub()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            hub.dispatch_event("codex", "UserPromptSubmit", {
                "session_id": "display-wait",
                "turn_id": "completed-turn",
                "transcript_path": str(transcript),
                "prompt": "Finish",
            })
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "completed-turn",
                    },
                }) + "\n")
            self.assertTrue(hub.reconcile_external_states())

            hub.dispatch_event("codex", "PermissionRequest", {
                "session_id": "approval-wait",
                "turn_id": "approval-turn",
                "tool_use_id": "approval-tool",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["message"], "npm test")
        self.assertEqual(
            payload["interaction"]["request_id"],
            "approval-tool",
        )

    def test_codex_non_waiting_event_cannot_create_menu_from_aggregate_wait(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "PermissionRequest", {
            "session_id": "approval-wait",
            "tool_use_id": "approval-tool",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        })
        self.assertTrue(
            hub.perform_active_action("approval-tool", "allow_once")
        )

        hub.dispatch_event("codex", "UserPromptSubmit", {
            "session_id": "other-task",
            "turn_id": "other-turn",
            "prompt": "Start another task",
        })

        self.assertNotIn("interaction", hub.get_hardware_payload())

    def test_codex_abort_removes_only_matching_parallel_session(self):
        hub = AgentMonitorHub()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            for session_id, turn_id, prompt in (
                ("kept-task", "kept-turn", "Still running"),
                ("aborted-task", "aborted-turn", "Will stop"),
            ):
                hub.dispatch_event("codex", "UserPromptSubmit", {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": str(transcript),
                    "prompt": prompt,
                })
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_aborted",
                        "turn_id": "aborted-turn",
                    },
                }) + "\n")

            self.assertTrue(hub.reconcile_external_states())

        adapter = hub.adapters["codex"]
        self.assertNotIn("aborted-task", adapter.session_states)
        self.assertIn("kept-task", adapter.session_states)
        self.assertEqual(adapter.current_state, AgentState.THINKING)
        self.assertEqual(adapter.last_message, "Still running")

    def test_codex_abort_clears_matching_approval_interaction(self):
        hub = AgentMonitorHub()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            hub.dispatch_event("codex", "PermissionRequest", {
                "session_id": "aborted-task",
                "turn_id": "aborted-turn",
                "tool_use_id": "approval-tool",
                "transcript_path": str(transcript),
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            })
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_aborted",
                        "turn_id": "aborted-turn",
                    },
                }) + "\n")

            self.assertTrue(hub.reconcile_external_states())

        self.assertNotIn("interaction", hub.get_hardware_payload())

    def test_unrelated_tool_activity_keeps_waiting_interaction(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "PermissionRequest", {
            "tool_use_id": "approval-tool",
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        })
        for event_name in ("PreToolUse", "PostToolUse"):
            hub.dispatch_event("codex", event_name, {
                "tool_use_id": "unrelated-tool",
                "tool_name": "Bash",
                "tool_input": {"command": "echo unrelated"},
            })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(
            payload["interaction"]["request_id"],
            "approval-tool",
        )

    def test_antigravity_native_hooks(self):
        adapter = AntigravityAdapter()
        adapter.handle_event("PreInvocation", {"invocationNum": 1})
        self.assertEqual(adapter.current_state, AgentState.THINKING)

        adapter.handle_event("PreToolUse", {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "npm test",
                    "Cwd": "/workspace/project",
                },
            }
        })
        self.assertEqual(adapter.current_state, AgentState.WAITING_APPROVAL)
        self.assertEqual(adapter.last_message, "npm test")

        adapter.handle_event("PostToolUse", {"stepIdx": 1, "error": ""})
        self.assertEqual(adapter.current_state, AgentState.THINKING)

        adapter.handle_event("PostToolUse", {
            "stepIdx": 2,
            "error": "exit status 1",
        })
        self.assertEqual(adapter.current_state, AgentState.ERROR)
        self.assertEqual(adapter.last_message, "exit status 1")

        adapter.handle_event("Stop", {
            "terminationReason": "model_stop",
            "fullyIdle": True,
        })
        self.assertEqual(adapter.current_state, AgentState.COMPLETED_UNREAD)

        adapter.handle_event("Stop", {
            "terminationReason": "error",
            "error": "model failed",
            "fullyIdle": True,
        })
        self.assertEqual(adapter.current_state, AgentState.ERROR)

    def test_antigravity_quiet_post_tool_use_reconciles_to_completed(self):
        adapter = AntigravityAdapter()
        adapter.handle_event("PostToolUse", {
            "conversationId": "conversation-1",
            "stepIdx": 264,
            "error": "",
            "toolCall": None,
        })
        self.assertEqual(adapter.current_state, AgentState.THINKING)
        self.assertFalse(adapter.reconcile_aborted_sessions())

        adapter.last_native_event_at -= (
            adapter.TERMINAL_EVENT_GRACE_SECONDS
        )
        self.assertTrue(adapter.reconcile_aborted_sessions())
        self.assertEqual(
            adapter.current_state,
            AgentState.COMPLETED_UNREAD,
        )
        self.assertEqual(adapter.last_message, "Task finished")
        self.assertFalse(adapter.reconcile_aborted_sessions())

    def test_antigravity_active_invocation_does_not_reconcile(self):
        adapter = AntigravityAdapter()
        adapter.handle_event("PreInvocation", {
            "conversationId": "conversation-1",
            "invocationNum": 4,
        })
        adapter.last_native_event_at -= (
            adapter.TERMINAL_EVENT_GRACE_SECONDS
        )

        self.assertFalse(adapter.reconcile_aborted_sessions())
        self.assertEqual(adapter.current_state, AgentState.THINKING)

    def test_hub_reconciles_quiet_antigravity_post_tool_use(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("antigravity", "PostToolUse", {
            "conversationId": "conversation-1",
            "stepIdx": 264,
            "error": "",
            "toolCall": None,
        })
        adapter = hub.adapters["antigravity"]
        adapter.last_native_event_at -= (
            adapter.TERMINAL_EVENT_GRACE_SECONDS
        )

        self.assertTrue(hub.reconcile_external_states())
        active = hub.get_hardware_payload()["active"]
        self.assertEqual(active["state"], "COMPLETED_UNREAD")
        self.assertEqual(active["message"], "Task finished")

    def test_antigravity_command_is_exposed_as_waiting_interaction(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("antigravity", "PreToolUse", {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "git status --short"},
            },
            "conversationId": "conversation-1",
            "requestId": "antigravity-request-1",
            "stepIdx": 3,
            "actions": [
                {"id": "reject", "label": "Reject"},
                {"id": "allow_once", "label": "Allow Once"},
            ],
        })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(
            payload["active"]["message"],
            "git status --short",
        )
        self.assertIn("interaction", payload)
        self.assertEqual(
            [action["id"] for action in payload["interaction"]["actions"]],
            ["return", "reject", "allow_once"],
        )

        self.assertTrue(
            hub.perform_active_action(
                "antigravity-request-1",
                "allow_once",
            )
        )
        self.assertEqual(
            hub.get_hardware_payload()["active"]["message"],
            "Selected Allow Once · sending to Antigravity",
        )
        self.assertEqual(
            hub.get_action_result(
                "antigravity-request-1",
                consume=True,
            )["action_id"],
            "allow_once",
        )
        consumed = hub.get_hardware_payload()["active"]
        self.assertEqual(consumed["phase"], "approved_running")
        self.assertEqual(consumed["state"], "THINKING")
        self.assertEqual(consumed["color"], "#FFB454")
        self.assertEqual(
            consumed["message"],
            "Approved · running git status --short",
        )

        hub.dispatch_event("antigravity", "PostToolUse", {
            "conversationId": "conversation-1",
            "stepIdx": 3,
            "error": "",
        })
        self.assertEqual(
            hub.get_hardware_payload()["active"]["phase"],
            "",
        )

    def test_antigravity_hook_maps_hardware_actions_to_decisions(self):
        self.assertEqual(
            ANTIGRAVITY_HOOK.permission_decision("allow_once")["decision"],
            "allow",
        )
        self.assertEqual(
            ANTIGRAVITY_HOOK.permission_decision("reject")["decision"],
            "deny",
        )
        self.assertEqual(
            ANTIGRAVITY_HOOK.permission_decision("return")["decision"],
            "ask",
        )
        self.assertEqual(
            ANTIGRAVITY_HOOK.permission_decision(None)["decision"],
            "ask",
        )

    def test_antigravity_cancelled_hook_resolves_native_app_approval(self):
        payload = {
            "conversationId": "antigravity-task",
            "requestId": "tool-1",
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "npm test"},
            },
        }
        with (
            patch.object(ANTIGRAVITY_HOOK, "post_event", return_value=True),
            patch.object(ANTIGRAVITY_HOOK, "install_cancellation_handler"),
            patch.object(
                ANTIGRAVITY_HOOK,
                "wait_for_hardware_action",
                side_effect=ANTIGRAVITY_HOOK.ApprovalHookCancelled,
            ),
            patch.object(
                ANTIGRAVITY_HOOK,
                "resolve_hardware_request",
            ) as resolve,
            patch.object(
                ANTIGRAVITY_HOOK.json,
                "load",
                return_value=payload,
            ),
            patch.object(
                ANTIGRAVITY_HOOK.sys,
                "argv",
                ["antigravity-hook", "PreToolUse"],
            ),
        ):
            ANTIGRAVITY_HOOK.main()

        resolve.assert_called_once_with("tool-1")

    def test_antigravity_expire_uses_daemon_url(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        with patch.object(
            ANTIGRAVITY_HOOK.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            ANTIGRAVITY_HOOK.expire_hardware_request("tool-1")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            f"{ANTIGRAVITY_HOOK.DAEMON_URL}/api/v1/action/expire",
        )

    def test_antigravity_hardware_allow_overrides_exact_sandbox_escape(self):
        payload = {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "BypassSandbox": True,
                    "CommandLine": "git push -u origin main",
                },
            },
        }

        decision = ANTIGRAVITY_HOOK.permission_decision(
            "allow_once",
            payload,
        )

        self.assertEqual(decision["decision"], "allow")
        self.assertEqual(
            decision["permissionOverrides"],
            ["unsandboxed(git push -u origin main)"],
        )

    def test_antigravity_sandbox_override_is_never_broader_than_request(self):
        regular = ANTIGRAVITY_HOOK.permission_decision(
            "allow_once",
            {
                "toolCall": {
                    "args": {
                        "BypassSandbox": False,
                        "CommandLine": "git status",
                    },
                },
            },
        )
        rejected = ANTIGRAVITY_HOOK.permission_decision(
            "reject",
            {
                "toolCall": {
                    "args": {
                        "BypassSandbox": True,
                        "CommandLine": "git push",
                    },
                },
            },
        )
        missing_command = ANTIGRAVITY_HOOK.permission_decision(
            "allow_once",
            {"toolCall": {"args": {"BypassSandbox": True}}},
        )

        self.assertNotIn("permissionOverrides", regular)
        self.assertNotIn("permissionOverrides", rejected)
        self.assertNotIn("permissionOverrides", missing_command)

    def test_antigravity_hook_waits_for_and_returns_hardware_decision(self):
        posted = {}

        def capture_event(event, payload):
            posted["event"] = event
            posted["payload"] = dict(payload)
            return True

        stdout = io.StringIO()
        with (
            patch.object(
                ANTIGRAVITY_HOOK.sys,
                "argv",
                ["antigravity-hook", "PreToolUse"],
            ),
            patch.object(
                ANTIGRAVITY_HOOK.sys,
                "stdin",
                io.StringIO(json.dumps({
                    "conversationId": "conversation-1",
                    "stepIdx": 3,
                    "toolCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "npm test"},
                    },
                })),
            ),
            patch.object(ANTIGRAVITY_HOOK.sys, "stdout", stdout),
            patch.object(
                ANTIGRAVITY_HOOK,
                "post_event",
                side_effect=capture_event,
            ),
            patch.object(
                ANTIGRAVITY_HOOK,
                "wait_for_hardware_action",
                return_value="allow_once",
            ) as wait,
        ):
            ANTIGRAVITY_HOOK.main()

        self.assertEqual(posted["event"], "PreToolUse")
        request_id = posted["payload"]["requestId"]
        self.assertTrue(request_id)
        self.assertEqual(
            [action["id"] for action in posted["payload"]["actions"]],
            ["reject", "allow_once"],
        )
        wait.assert_called_once_with(request_id)
        self.assertEqual(json.loads(stdout.getvalue())["decision"], "allow")

    def test_antigravity_hook_emits_sandbox_override_after_hardware_allow(self):
        stdout = io.StringIO()
        with (
            patch.object(
                ANTIGRAVITY_HOOK.sys,
                "argv",
                ["antigravity-hook", "PreToolUse"],
            ),
            patch.object(
                ANTIGRAVITY_HOOK.sys,
                "stdin",
                io.StringIO(json.dumps({
                    "conversationId": "conversation-1",
                    "toolCall": {
                        "name": "run_command",
                        "args": {
                            "BypassSandbox": True,
                            "CommandLine": ["git", "push"],
                        },
                    },
                })),
            ),
            patch.object(ANTIGRAVITY_HOOK.sys, "stdout", stdout),
            patch.object(
                ANTIGRAVITY_HOOK,
                "post_event",
                return_value=True,
            ),
            patch.object(
                ANTIGRAVITY_HOOK,
                "wait_for_hardware_action",
                return_value="allow_once",
            ),
        ):
            ANTIGRAVITY_HOOK.main()

        self.assertEqual(
            json.loads(stdout.getvalue())["permissionOverrides"],
            ["unsandboxed(git push)"],
        )

    def test_antigravity_hook_returns_immediately_when_hardware_is_offline(self):
        with (
            patch.object(
                ANTIGRAVITY_HOOK,
                "fetch_hardware_action",
                return_value=ANTIGRAVITY_HOOK.HARDWARE_OFFLINE,
            ) as fetch,
            patch.object(ANTIGRAVITY_HOOK.time, "sleep") as sleep,
        ):
            action = ANTIGRAVITY_HOOK.wait_for_hardware_action(
                "offline-request"
            )

        self.assertIsNone(action)
        fetch.assert_called_once()
        sleep.assert_not_called()

    def test_antigravity_hook_retries_transient_poll_failure(self):
        with (
            patch.object(ANTIGRAVITY_HOOK, "APPROVAL_TIMEOUT", 10),
            patch.object(
                ANTIGRAVITY_HOOK,
                "fetch_hardware_action",
                side_effect=[OSError("transient"), "allow_once"],
            ) as fetch,
            patch.object(ANTIGRAVITY_HOOK.time, "sleep"),
        ):
            action = ANTIGRAVITY_HOOK.wait_for_hardware_action(
                "retry-request"
            )

        self.assertEqual(action, "allow_once")
        self.assertEqual(fetch.call_count, 2)

    def test_generic_webhook_adapter(self):
        adapter = GenericWebhookAdapter("custom_bot", "Custom Bot")
        adapter.handle_event("custom_event", {"status": "THINKING", "message": "Processing..."})
        self.assertEqual(adapter.current_state, AgentState.THINKING)

        adapter.handle_event("custom_event", {"status": "WAITING_APPROVAL", "message": "Approve action"})
        self.assertEqual(adapter.current_state, AgentState.WAITING_APPROVAL)

    def test_hub_multi_agent(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("claude_code", "thinking", {"message": "Claude is working"})
        self.assertEqual(hub.active_agent_id, "claude_code")

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["cmd"], "SET_STATE")
        self.assertEqual(payload["active"]["state"], "THINKING")
        self.assertEqual(payload["active"]["color"], "#A3CCDA")

        # Switch agent
        hub.next_agent()
        self.assertNotEqual(hub.active_agent_id, None)

    def test_waiting_interaction_starts_with_return_and_accepts_one_action(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {"request_id": "req-1", "message": "Run tests?"},
        )

        payload = hub.get_hardware_payload()
        interaction = payload["interaction"]
        self.assertEqual(interaction["request_id"], "req-1")
        self.assertEqual(interaction["actions"][0]["id"], "return")
        self.assertTrue(
            hub.perform_active_action("req-1", "allow_once")
        )
        self.assertFalse(
            hub.perform_active_action("req-1", "allow_once")
        )
        self.assertNotIn("interaction", hub.get_hardware_payload())
        self.assertEqual(
            hub.get_action_result("req-1")["action_id"],
            "allow_once",
        )

    def test_return_keeps_waiting_interaction_reopenable(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "claude_code",
            "PermissionRequest",
            {"request_id": "req-current", "message": "Edit file?"},
        )

        self.assertTrue(
            hub.perform_active_action("req-current", "return")
        )
        self.assertIsNone(hub.get_action_result("req-current"))
        payload = hub.get_hardware_payload()
        self.assertEqual(
            payload["interaction"]["request_id"],
            "req-current",
        )
        self.assertEqual(payload["interaction"]["revision"], 1)
        self.assertTrue(
            hub.perform_active_action("req-current", "allow_once")
        )
        self.assertEqual(
            hub.get_action_result("req-current")["action_id"],
            "allow_once",
        )
        self.assertNotIn("interaction", hub.get_hardware_payload())
        self.assertFalse(
            hub.perform_active_action("req-stale", "reject")
        )

    def test_custom_waiting_actions_cannot_replace_return(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "custom",
            "WAITING_APPROVAL",
            {
                "request_id": "custom-1",
                "actions": [
                    {"id": "return", "label": "Unsafe replacement"},
                    {"id": "retry", "label": "Retry"},
                ],
            },
        )

        actions = hub.get_hardware_payload()["interaction"]["actions"]
        self.assertEqual(actions[0], {
            "id": "return",
            "label": "Return",
            "dangerous": False,
        })
        self.assertEqual(actions[1]["id"], "retry")

    def test_non_waiting_state_cannot_replace_current_waiting_state(self):
        hub = AgentMonitorHub()
        hub.dispatch_event(
            "codex",
            "PermissionRequest",
            {"request_id": "req-2"},
        )
        hub.dispatch_event("codex", "thinking", {"message": "Continuing"})

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(payload["interaction"]["request_id"], "req-2")
        self.assertTrue(
            hub.perform_active_action("req-2", "allow_once")
        )

    def test_other_agent_status_cannot_take_over_current_waiting_display(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("codex", "PermissionRequest", {
            "session_id": "codex-task",
            "request_id": "codex-wait",
            "message": "Approve Codex?",
        })

        hub.dispatch_event("claude_code", "thinking", {
            "session_id": "claude-task",
            "message": "Claude is working",
        })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["agent_id"], "codex")
        self.assertEqual(payload["active"]["state"], "WAITING_APPROVAL")
        self.assertEqual(
            payload["interaction"]["request_id"],
            "codex-wait",
        )
        self.assertEqual(
            hub.adapters["claude_code"].current_state,
            AgentState.THINKING,
        )

    def test_new_waiting_requests_are_displayed_in_fifo_order(self):
        hub = AgentMonitorHub()
        waits = (
            ("codex", "codex-task", "wait-1"),
            ("claude_code", "claude-task", "wait-2"),
            ("antigravity", "antigravity-task", "wait-3"),
        )
        for agent_id, session_id, request_id in waits:
            hub.dispatch_event(agent_id, "PermissionRequest", {
                "session_id": session_id,
                "request_id": request_id,
                "message": request_id,
            })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["agent_id"], "codex")
        self.assertEqual(payload["interaction"]["request_id"], "wait-1")
        self.assertEqual(
            [item["request_id"] for item in hub.queued_interactions],
            ["wait-2", "wait-3"],
        )

        self.assertTrue(hub.perform_active_action("wait-1", "allow_once"))
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["agent_id"], "claude_code")
        self.assertEqual(payload["interaction"]["request_id"], "wait-2")

        self.assertTrue(hub.perform_active_action("wait-2", "reject"))
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["active"]["agent_id"], "antigravity")
        self.assertEqual(payload["interaction"]["request_id"], "wait-3")

    def test_same_agent_waiting_requests_do_not_overwrite_each_other(self):
        hub = AgentMonitorHub()
        for request_id in ("wait-1", "wait-2"):
            hub.dispatch_event("codex", "PermissionRequest", {
                "session_id": "same-task",
                "request_id": request_id,
                "message": request_id,
            })

        self.assertEqual(
            hub.get_hardware_payload()["interaction"]["request_id"],
            "wait-1",
        )
        self.assertTrue(hub.perform_active_action("wait-1", "allow_once"))
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["interaction"]["request_id"], "wait-2")
        self.assertEqual(payload["active"]["message"], "wait-2")

    def test_idle_adapters_are_hidden_from_hardware(self):
        hub = AgentMonitorHub()
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["agents_count"], 0)
        self.assertEqual(payload["active"]["agent_id"], "none")

        hub.dispatch_event("claude_code", "UserPromptSubmit", {"prompt": "Work"})
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["agents_count"], 1)
        self.assertEqual(payload["active"]["agent_id"], "claude_code")

        hub.dispatch_event("codex", "thinking", {"task": "Review"})
        self.assertEqual(hub.get_hardware_payload()["agents_count"], 2)

        hub.adapters["claude_code"].acknowledge_read()
        hub.adapters["claude_code"].update_state(AgentState.IDLE)
        payload = hub.get_hardware_payload()
        self.assertEqual(payload["agents_count"], 1)
        self.assertEqual(payload["active"]["agent_id"], "codex")

    def test_running_idle_clients_are_visible(self):
        hub = AgentMonitorHub()
        hub.set_agent_presence("claude_code", True)
        hub.set_agent_presence("codex", True)
        hub.set_agent_presence("antigravity", False)

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["agents_count"], 2)
        self.assertIn(payload["active"]["agent_id"], ("claude_code", "codex"))
        self.assertEqual(payload["active"]["state"], "IDLE")

        hub.set_agent_presence("claude_code", False)
        self.assertEqual(hub.get_hardware_payload()["agents_count"], 1)

    def test_hook_state_is_visible_when_process_detection_misses_client(self):
        hub = AgentMonitorHub()
        hub.set_agent_presence("antigravity", False)
        hub.dispatch_event("antigravity", "PreInvocation", {
            "conversationId": "conversation-1",
            "invocationNum": 0,
        })

        payload = hub.get_hardware_payload()
        self.assertEqual(payload["agents_count"], 1)
        self.assertEqual(payload["active"]["agent_id"], "antigravity")
        self.assertEqual(payload["active"]["state"], "THINKING")

    def test_process_monitor_detection(self):
        hub = AgentMonitorHub()
        monitor = AgentProcessMonitor(hub)
        monitor._snapshot = lambda: """
claude claude
/Applications/ChatGPT.app/Contents/MacOS/ChatGPT
/Applications/Antigravity.app/Contents/MacOS/Antigravity
""".lower()
        detected = monitor.detect()
        self.assertTrue(detected["claude_code"])
        self.assertTrue(detected["codex"])
        self.assertTrue(detected["antigravity"])

    def test_custom_cli_wrapper_keeps_exit_semantics(self):
        hub = AgentMonitorHub()
        hub.dispatch_event("pytest_job", "start", {"cmd": "pytest"})
        self.assertIsInstance(hub.adapters["pytest_job"], CLIWrapperAdapter)
        self.assertEqual(hub.adapters["pytest_job"].current_state, AgentState.THINKING)

        hub.dispatch_event("pytest_job", "exit", {"cmd": "pytest", "exit_code": 0})
        self.assertEqual(
            hub.adapters["pytest_job"].current_state,
            AgentState.COMPLETED_UNREAD,
        )

    def test_native_adapters_share_session_aggregation_contract(self):
        cases = (
            (
                ClaudeCodeAdapter(),
                "PermissionRequest",
                {
                    "session_id": "waiting",
                    "tool_use_id": "approval-1",
                    "tool_input": {"command": "pytest"},
                },
                "Stop",
                {"session_id": "completed"},
            ),
            (
                AntigravityAdapter(),
                "PreToolUse",
                {
                    "conversationId": "waiting",
                    "tool_use_id": "approval-1",
                    "toolCall": {
                        "args": {"CommandLine": ["pytest"]},
                    },
                },
                "Stop",
                {
                    "conversationId": "completed",
                    "fullyIdle": True,
                },
            ),
        )

        for adapter, waiting_event, waiting, completed_event, completed in cases:
            with self.subTest(adapter=adapter.agent_id):
                self.assertIsInstance(adapter, SessionAwareAgentAdapter)
                adapter.handle_event(waiting_event, waiting)
                adapter.handle_event(completed_event, completed)
                self.assertEqual(
                    adapter.current_state,
                    AgentState.WAITING_APPROVAL,
                )
                self.assertEqual(adapter.visible_session_id, "waiting")
                self.assertIn("completed", adapter.session_states)

    def test_native_translators_expose_interaction_correlation(self):
        cases = (
            (
                ClaudeCodeAdapter(),
                "PermissionRequest",
                {"session_id": "claude-1", "tool_use_id": "tool-1"},
            ),
            (
                CodexAdapter(),
                "PermissionRequest",
                {"threadId": "codex-1", "tool_use_id": "tool-1"},
            ),
            (
                AntigravityAdapter(),
                "PreToolUse",
                {"conversationId": "ag-1", "tool_use_id": "tool-1"},
            ),
        )

        for adapter, event_name, payload in cases:
            with self.subTest(adapter=adapter.agent_id):
                event = adapter.translate_event(event_name, payload)
                self.assertTrue(event.opens_interaction)
                self.assertTrue(event.interactive)
                self.assertEqual(event.request_id, "tool-1")
                self.assertTrue(event.session_id)

    def test_normalized_tool_conflict_policy_applies_to_all_native_agents(self):
        cases = (
            (
                "claude_code",
                "PermissionRequest",
                {"session_id": "task", "tool_use_id": "waiting"},
            ),
            (
                "codex",
                "PermissionRequest",
                {"session_id": "task", "tool_use_id": "waiting"},
            ),
            (
                "antigravity",
                "PreToolUse",
                {"conversationId": "task", "tool_use_id": "waiting"},
            ),
        )

        for agent_id, waiting_event, payload in cases:
            with self.subTest(agent=agent_id):
                hub = AgentMonitorHub()
                hub.dispatch_event(agent_id, waiting_event, payload)
                hub.dispatch_event(agent_id, "PostToolUse", {
                    **payload,
                    "tool_use_id": "unrelated",
                })
                self.assertEqual(
                    hub.get_hardware_payload()["interaction"]["request_id"],
                    "waiting",
                )

    def test_disconnect_clears_shared_session_state(self):
        hub = AgentMonitorHub()
        hub.set_agent_presence("claude_code", True)
        hub.dispatch_event("claude_code", "UserPromptSubmit", {
            "session_id": "task-1",
            "prompt": "Work",
        })
        self.assertTrue(hub.adapters["claude_code"].session_states)

        hub.set_agent_presence("claude_code", False)

        adapter = hub.adapters["claude_code"]
        self.assertEqual(adapter.current_state, AgentState.IDLE)
        self.assertEqual(adapter.session_states, {})

    def test_completed_state_auto_clears_after_one_quiet_minute(self):
        cases = (
            (ClaudeCodeAdapter(), "done", {"session_id": "claude-task"}),
            (CodexAdapter(), "Stop", {"session_id": "codex-task"}),
            (
                AntigravityAdapter(),
                "finished",
                {"conversationId": "antigravity-task"},
            ),
            (GenericWebhookAdapter("generic"), "completed", {}),
            (
                CLIWrapperAdapter(),
                "exit",
                {"cmd": "pytest", "exit_code": 0},
            ),
        )

        for adapter, event_name, payload in cases:
            with self.subTest(adapter=adapter.agent_id):
                adapter.handle_event(event_name, payload)
                self.assertEqual(
                    adapter.current_state,
                    AgentState.COMPLETED_UNREAD,
                )

                adapter.last_updated -= 59
                self.assertFalse(adapter.reconcile_external_state())
                self.assertEqual(
                    adapter.current_state,
                    AgentState.COMPLETED_UNREAD,
                )

                adapter.last_updated -= 1.1
                self.assertTrue(adapter.reconcile_external_state())
                self.assertEqual(adapter.current_state, AgentState.IDLE)
                self.assertFalse(adapter.unread)
                if isinstance(adapter, SessionAwareAgentAdapter):
                    self.assertEqual(adapter.session_states, {})

    def test_new_completed_action_restarts_auto_idle_timer(self):
        adapter = CodexAdapter()
        adapter.handle_event("Stop", {
            "session_id": "task-1",
            "message": "First completion",
        })
        adapter.last_updated -= 59

        adapter.handle_event("Stop", {
            "session_id": "task-2",
            "message": "Second completion",
        })

        self.assertFalse(adapter.reconcile_external_state())
        self.assertEqual(
            adapter.current_state,
            AgentState.COMPLETED_UNREAD,
        )

if __name__ == "__main__":
    unittest.main()
