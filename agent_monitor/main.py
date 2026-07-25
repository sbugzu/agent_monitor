"""
Main Daemon entry point for AI Agent Hardware Monitor.
Launches state hub, HTTP server, hardware bridges (Serial / BLE), and simulator.
"""

import sys
import time
import argparse
import logging
from agent_monitor.core.hub import AgentMonitorHub
from agent_monitor.server import MonitorServer
from agent_monitor.hardware.bridge import SerialBridge
from agent_monitor.hardware.ble_bridge import BLEBridge
from agent_monitor.hardware.simulator import HardwareSimulator
from agent_monitor.core.process_monitor import AgentProcessMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("AgentMonitorDaemon")

def main():
    parser = argparse.ArgumentParser(description="AI Agent Hardware Monitor Daemon for Lilygo T-Encoder Pro")
    parser.add_argument("--port", type=str, default=None, help="Serial port path (auto-detected if omitted)")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baudrate (default: 115200)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="HTTP API host (default: 127.0.0.1)")
    parser.add_argument("--api-port", type=int, default=8765, help="HTTP API port (default: 8765)")
    parser.add_argument("--ble", action="store_true", help="Enable Bluetooth BLE connection mode")
    parser.add_argument("--no-sim", action="store_true", help="Disable terminal simulator output")
    args = parser.parse_args()

    hub = AgentMonitorHub()
    simulator = HardwareSimulator(enabled=not args.no_sim)

    # Hardware Event Receiver (Knob / Touch events sent back from T-Encoder Pro)
    def on_hardware_event(data: dict):
        event = data.get("event")
        logger.info(f"Hardware event received: {data}")
        if event == "KNOB_ROTATE":
            direction = data.get("dir", 1)
            if direction > 0:
                hub.next_agent()
            else:
                hub.prev_agent()
        elif event == "KNOB_PRESS" or event == "TOUCH_ACK":
            hub.acknowledge_active_agent()
        elif event == "KNOB_ACTION":
            hub.perform_active_action(
                str(data.get("request_id") or ""),
                str(data.get("action_id") or ""),
            )
        elif event == "READY":
            logger.info("Hardware ready. Sending current state...")
            hub.notify_hardware()

    # Hardware Bridges
    serial_bridge = SerialBridge(
        port=args.port,
        baudrate=args.baud,
        event_handler=on_hardware_event,
        connection_handler=lambda connected: hub.set_hardware_connection(
            "serial", connected
        ),
    )
    serial_bridge.start()

    ble_bridge = None
    if args.ble:
        ble_bridge = BLEBridge(
            event_handler=on_hardware_event,
            connection_handler=lambda connected: hub.set_hardware_connection(
                "ble", connected
            ),
        )
        ble_bridge.start()

    # Hub Hardware Notification Callback
    def notify_hardware_all(payload: dict):
        simulator.render(payload)
        serial_bridge.send_data(payload)
        if ble_bridge:
            ble_bridge.send_data(payload)

    hub.register_hardware_callback(notify_hardware_all)

    # Presence detection makes an open-but-idle client visible without
    # requiring it to emit a lifecycle event first.
    process_monitor = AgentProcessMonitor(hub)
    process_monitor.start()

    # Start HTTP API Server
    server = MonitorServer(hub, host=args.host, port=args.api_port)
    server.start()

    # Send initial state frame
    hub.notify_hardware()

    logger.info("Agent Monitor Host Daemon started successfully. Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("Shutting down Agent Monitor Daemon...")
        process_monitor.stop()
        serial_bridge.stop()
        if ble_bridge:
            ble_bridge.stop()
        server.stop()

if __name__ == "__main__":
    main()
