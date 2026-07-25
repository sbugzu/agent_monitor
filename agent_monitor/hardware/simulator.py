"""
Software Simulator for Lilygo T-Encoder Pro screen & LED ring.
Outputs rich ANSI colored console UI when hardware is unplugged.
"""

import sys
import logging
from typing import Dict, Any

logger = logging.getLogger("HardwareSimulator")

# ANSI Color Codes
COLOR_MAP = {
    "#FFFDF6": "\033[97m",  # Warm white
    "#A3CCDA": "\033[94m",  # Pastel blue
    "#BDE3C3": "\033[92m",  # Pastel green
    "#F8F7BA": "\033[93m",  # Pastel yellow
    "#F5D2D2": "\033[91m",  # Pastel red
    "#FFB454": "\033[33m",  # Waiting / approval orange
    "#F2A7A7": "\033[91m",  # Rejected coral
}
RESET = "\033[0m"
BOLD = "\033[1m"

class HardwareSimulator:
    def __init__(self, enabled: bool = True):
        self.enabled: bool = enabled
        self.last_state_summary = ""

    def render(self, payload: Dict[str, Any]):
        if not self.enabled:
            return

        active = payload.get("active", {})
        agent_id = active.get("agent_id", "Unknown")
        display_name = active.get("display_name", "No Agent")
        state = active.get("state", "IDLE")
        hex_color = active.get("color", "#FFFDF6")
        msg = active.get("message", "")
        phase = active.get("phase", "")
        unread = active.get("unread", False)

        ansi_color = COLOR_MAP.get(hex_color.upper(), "\033[97m")
        unread_badge = " [UNREAD]" if unread else ""

        summary = f"{agent_id}:{state}:{phase}:{msg}"
        if summary == self.last_state_summary:
            return
        self.last_state_summary = summary

        print("\n" + "="*50)
        print(f" {BOLD}[T-Encoder Pro Hardware Screen Simulator]{RESET}")
        print(f" Status LED Ring Color : {ansi_color}██████████ {state}{unread_badge}{RESET}")
        print(f" Active Agent          : {BOLD}{display_name}{RESET} ({agent_id})")
        print(f" Interaction Phase     : {phase or '-'}")
        print(f" Current Message       : {ansi_color}{msg}{RESET}")
        print(f" Registered Agents     : {payload.get('agents_count', 0)}")
        print("="*50 + "\n")
