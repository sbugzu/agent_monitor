"""
Bluetooth Low Energy (BLE UART) Bridge for Lilygo T-Encoder Pro.
Connects over Nordic UART Service (NUS) via Bleak library.
"""

import asyncio
import json
import logging
import threading
from typing import Optional, Callable
from agent_monitor.hardware.protocol import encode_hardware_frame

logger = logging.getLogger("BLEBridge")

# Nordic UART Service UUIDs
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e" # Write to ESP32
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e" # Read from ESP32

class BLEBridge:
    def __init__(
        self,
        device_name: str = "T-Encoder-Pro",
        event_handler: Optional[Callable] = None,
        connection_handler: Optional[Callable[[bool], None]] = None,
    ):
        self.device_name: str = device_name
        self.event_handler: Optional[Callable] = event_handler
        self.connection_handler = connection_handler
        self.connected: bool = False
        self.client = None
        self.loop = None
        self.thread: Optional[threading.Thread] = None
        self.running: bool = False
        self._notification_buffer = bytearray()
        self._pending_frame: Optional[bytes] = None
        self._writer_task = None
        self.chunk_interval = 0.005
        self.write_timeout = 1.0

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._set_connected(False)

    def _set_connected(self, connected: bool):
        connected = bool(connected)
        if self.connected == connected:
            return
        self.connected = connected
        if self.connection_handler:
            self.connection_handler(connected)

    def send_data(self, payload: dict):
        if not self.loop or not self.client or not self.client.is_connected:
            return
        frame = encode_hardware_frame(payload)
        # Hub notifications can originate from several HTTP threads. Marshal
        # them onto the BLE loop so chunks from different JSON frames can
        # never interleave on the UART characteristic.
        self.loop.call_soon_threadsafe(self._enqueue_frame, frame)

    def _enqueue_frame(self, frame: bytes):
        # State is snapshot-based, so while one frame is in flight only the
        # newest pending snapshot matters.
        self._pending_frame = frame
        if not self._writer_task or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._drain_writes())

    async def _drain_writes(self):
        try:
            while self._pending_frame is not None:
                frame = self._pending_frame
                self._pending_frame = None
                await self._async_write(frame)
        finally:
            self._writer_task = None

    async def _async_write(self, data: bytes):
        try:
            if self.client and self.client.is_connected:
                # Twenty-byte chunks work before and after MTU negotiation.
                # Firmware reassembles newline-delimited frames.
                for offset in range(0, len(data), 20):
                    await asyncio.wait_for(
                        self.client.write_gatt_char(
                            NUS_RX_CHAR_UUID,
                            data[offset:offset + 20],
                            response=False,
                        ),
                        timeout=self.write_timeout,
                    )
                    # A single writer prevents frame interleaving; a short
                    # pause supplies enough back-pressure without making every
                    # 20-byte chunk wait for a BLE round trip.
                    if self.chunk_interval > 0:
                        await asyncio.sleep(self.chunk_interval)
        except Exception as e:
            logger.error("BLE write error (%s): %r", type(e).__name__, e)
            # A timed-out CoreBluetooth write can leave the connection alive
            # while its outbound queue is permanently wedged. Force the normal
            # reconnect path; READY replay will resend the latest hub snapshot.
            client = self.client
            if client and client.is_connected:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=2.0)
                except Exception as disconnect_error:
                    logger.error(
                        "BLE disconnect after write failure (%s): %r",
                        type(disconnect_error).__name__,
                        disconnect_error,
                    )

    def _notification_handler(self, sender, data: bytes):
        try:
            self._notification_buffer.extend(data)
            while b"\n" in self._notification_buffer:
                line_bytes, _, remainder = self._notification_buffer.partition(b"\n")
                self._notification_buffer = bytearray(remainder)
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not (line.startswith("{") and line.endswith("}")):
                    continue
                payload = json.loads(line)
                if self.event_handler:
                    self.event_handler(payload)
        except Exception as e:
            self._notification_buffer.clear()
            logger.error(f"BLE notification error: {e}")

    def _worker(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main_ble_loop())

    async def _main_ble_loop(self):
        try:
            from bleak import BleakScanner, BleakClient
        except ImportError:
            logger.warning("bleak package not installed. BLE bridge disabled. (Install with: pip install bleak)")
            return

        while self.running:
            logger.info(f"Scanning for BLE device: {self.device_name}...")
            device = await BleakScanner.find_device_by_name(self.device_name, timeout=5.0)
            if not device:
                await asyncio.sleep(3.0)
                continue

            logger.info(f"Found BLE device: {device.name} ({device.address}). Connecting...")
            try:
                async with BleakClient(device, timeout=10.0) as client:
                    self.client = client
                    self._notification_buffer.clear()
                    logger.info("Connected to Lilygo T-Encoder Pro over BLE!")
                    await client.start_notify(NUS_TX_CHAR_UUID, self._notification_handler)
                    self._set_connected(True)
                    # The firmware's boot-time READY notification is commonly
                    # sent before the daemon connects and subscribes. Request
                    # an immediate replay of the current hub state, matching
                    # the serial bridge's reconnect behavior.
                    if self.event_handler:
                        self.event_handler({"event": "READY", "transport": "ble"})

                    while self.running and client.is_connected:
                        await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"BLE connection error: {e}")
            finally:
                self.client = None
                self._pending_frame = None
                writer_task = self._writer_task
                self._writer_task = None
                if writer_task and not writer_task.done():
                    writer_task.cancel()
                    await asyncio.gather(writer_task, return_exceptions=True)
                self._set_connected(False)
            if self.running:
                await asyncio.sleep(3.0)
