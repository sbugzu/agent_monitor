"""
Agent states and color mapping definition for Lilygo T-Encoder Pro monitor.
"""

from enum import Enum
from typing import Dict

class AgentState(str, Enum):
    IDLE = "IDLE"                         # White: Task idle
    THINKING = "THINKING"                 # Blue: In progress / thinking
    COMPLETED_UNREAD = "COMPLETED_UNREAD" # Green: Task finished but unread
    WAITING_APPROVAL = "WAITING_APPROVAL" # Amber/Yellow: Waiting for user approval or reply
    ERROR = "ERROR"                       # Red: Error occurred

# Color mappings (HEX and RGB tuple)
STATE_COLORS: Dict[AgentState, Dict[str, any]] = {
    AgentState.IDLE: {
        "hex": "#FFFDF6",
        "rgb": (255, 253, 246),
        "label": "Idle",
        "description": "Task idle / standing by"
    },
    AgentState.THINKING: {
        "hex": "#A3CCDA",
        "rgb": (163, 204, 218),
        "label": "Thinking",
        "description": "Task running / in progress"
    },
    AgentState.COMPLETED_UNREAD: {
        "hex": "#BDE3C3",
        "rgb": (189, 227, 195),
        "label": "Completed (Unread)",
        "description": "Task finished but unread"
    },
    AgentState.WAITING_APPROVAL: {
        "hex": "#F8F7BA",
        "rgb": (248, 247, 186),
        "label": "Waiting Approval",
        "description": "Waiting for user input or approval"
    },
    AgentState.ERROR: {
        "hex": "#F5D2D2",
        "rgb": (245, 210, 210),
        "label": "Error",
        "description": "Error or failure occurred"
    }
}
