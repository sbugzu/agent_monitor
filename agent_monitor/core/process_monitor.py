"""Detect locally running agent clients and publish their presence to the hub."""

import logging
import subprocess
import threading
import time
from typing import Dict

from agent_monitor.core.hub import AgentMonitorHub

logger = logging.getLogger("AgentProcessMonitor")


class AgentProcessMonitor:
    def __init__(self, hub: AgentMonitorHub, interval: float = 2.0):
        self.hub = hub
        self.interval = interval
        self.running = False
        self.thread = None

    def _snapshot(self) -> str:
        try:
            result = subprocess.run(
                ["ps", "-axo", "comm=,args="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return result.stdout.lower()
        except Exception as exc:
            logger.debug("Could not inspect agent processes: %s", exc)
            return ""

    def detect(self) -> Dict[str, bool]:
        processes = self._snapshot()
        return {
            "claude_code": any(
                marker in processes
                for marker in ("\nclaude ", "\nclaude\t", "/bin/claude ")
            ),
            "codex": any(
                marker in processes
                for marker in (
                    "/applications/chatgpt.app/contents/macos/chatgpt",
                    "/resources/codex ",
                    "\ncodex ",
                )
            ),
            "antigravity": (
                "/applications/antigravity.app/contents/macos/antigravity"
                in processes
            ),
        }

    def poll_once(self):
        for agent_id, present in self.detect().items():
            self.hub.set_agent_presence(agent_id, present)
        self.hub.reconcile_external_states()

    def _run(self):
        while self.running:
            self.poll_once()
            time.sleep(self.interval)

    def start(self):
        self.running = True
        self.poll_once()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.interval + 1)
