"""
USB Serial Bridge for Lilygo T-Encoder Pro.
Handles serial port auto-detection, reconnection loop, JSON protocol framing, and receiving knob events.
"""

import json
import logging
import threading
import time
from typing import Optional, Callable
import serial
import serial.tools.list_ports
from agent_monitor.hardware.protocol import encode_hardware_frame

logger = logging.getLogger("SerialBridge")

class SerialBridge:
    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        event_handler: Optional[Callable] = None,
        connection_handler: Optional[Callable[[bool], None]] = None,
    ):
        self.port: Optional[str] = port
        self.baudrate: int = baudrate
        self.event_handler: Optional[Callable] = event_handler
        self.connection_handler = connection_handler
        self.connected: bool = False
        self.serial_conn: Optional[serial.Serial] = None
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.last_frame: Optional[bytes] = None
        self.last_send_at: float = 0.0
        # AMOLED redraws can briefly delay firmware serial reads. Keep
        # back-to-back state frames below the device RX buffer pressure.
        self.min_send_interval: float = 0.15
        # State frames are deduplicated, so keep the firmware's USB connection
        # indicator alive with a lightweight frame that does not redraw the UI.
        self.heartbeat_interval: float = 2.0

    def find_lilygo_port(self) -> Optional[str]:
        """Auto-detect Lilygo T-Encoder Pro / ESP32-S3 USB serial port on Mac/Linux/Windows."""
        ports = serial.tools.list_ports.comports()
        for p in ports:
            # Common USB CDC VID/PID for ESP32-S3 / CH340 / CP210x / Lilygo
            vid_pid = f"{p.vid:04x}:{p.pid:04x}".upper() if p.vid and p.pid else ""
            desc = p.description.lower() if p.description else ""
            hwid = p.hwid.lower() if p.hwid else ""

            if "usbmodem" in p.device.lower() or "ttyusb" in p.device.lower() or "ch340" in desc or "cp210" in desc or "esp32" in desc or "303a" in vid_pid:
                logger.info(f"Auto-detected hardware port: {p.device} ({p.description})")
                return p.device
        return None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        self._set_connected(False)

    def _set_connected(self, connected: bool):
        connected = bool(connected)
        if self.connected == connected:
            return
        self.connected = connected
        if self.connection_handler:
            self.connection_handler(connected)

    def send_data(self, payload: dict):
        if not self.serial_conn or not self.serial_conn.is_open:
            return
        try:
            frame = encode_hardware_frame(payload)
            with self.lock:
                if frame == self.last_frame:
                    return
                wait_for = self.min_send_interval - (time.monotonic() - self.last_send_at)
                if wait_for > 0:
                    time.sleep(wait_for)
                self.serial_conn.write(frame)
                self.serial_conn.flush()
                self.last_frame = frame
                self.last_send_at = time.monotonic()
        except Exception as e:
            logger.error(f"Serial send error: {e}")
            if self.serial_conn:
                try:
                    self.serial_conn.close()
                except Exception:
                    pass
            self.serial_conn = None
            self.last_frame = None
            self.last_send_at = 0.0
            self._set_connected(False)

    def _send_heartbeat_if_due(self):
        if (
            not self.serial_conn
            or not self.serial_conn.is_open
            or self.last_frame is None
            or time.monotonic() - self.last_send_at < self.heartbeat_interval
        ):
            return

        with self.lock:
            # Re-check after acquiring the lock because send_data() may have
            # written a state frame while this thread was waiting.
            if time.monotonic() - self.last_send_at < self.heartbeat_interval:
                return
            self.serial_conn.write(b'{"cmd":"PING"}\n')
            self.serial_conn.flush()
            self.last_send_at = time.monotonic()

    def _run_loop(self):
        while self.running:
            if not self.serial_conn or not self.serial_conn.is_open:
                target_port = self.port or self.find_lilygo_port()
                if target_port:
                    try:
                        self.serial_conn = serial.Serial(target_port, self.baudrate, timeout=1.0)
                        self.last_frame = None
                        self.last_send_at = 0.0
                        logger.info(f"Connected to Lilygo hardware on {target_port}")
                        self._set_connected(True)
                        # The device's boot-time READY frame may have been sent
                        # before the daemon opened the port. Request an
                        # immediate replay of the current hub state.
                        if self.event_handler:
                            self.event_handler({"event": "READY", "transport": "serial"})
                    except Exception as e:
                        logger.debug(f"Could not connect to port {target_port}: {e}")
                        time.sleep(2.0)
                        continue
                else:
                    time.sleep(2.0)
                    continue

            try:
                line = self.serial_conn.readline().decode("utf-8", errors="ignore").strip()
                if line and line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if self.event_handler:
                            self.event_handler(data)
                    except json.JSONDecodeError:
                        pass
                self._send_heartbeat_if_due()
            except Exception as e:
                logger.error(f"Serial read loop error: {e}")
                if self.serial_conn:
                    try:
                        self.serial_conn.close()
                    except Exception:
                        pass
                self.serial_conn = None
                self.last_frame = None
                self.last_send_at = 0.0
                self._set_connected(False)
                time.sleep(1.0)
