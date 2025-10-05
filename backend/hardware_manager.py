# backend/hardware_manager.py
import logging
import time

import psutil
from smbus2 import SMBus

logger = logging.getLogger("utils")

class HardwareManager:
    def __init__(self, config):
        self.config = config
        self.i2c_address = int(self.config.get("motors", {}).get("i2c_address", "0x34"), 16)
        self.left_channel = int(self.config.get("motors", {}).get("left_channel", 0))
        self.right_channel = int(self.config.get("motors", {}).get("right_channel", 1))
        self.max_speed = int(self.config.get("motors", {}).get("max_speed", 100))
        self.bus = None
        self.left_speed = 0
        self.right_speed = 0
        self._battery_voltage = None
        self._battery_raw = None
        self._battery_bytes = (None, None)
        self._last_battery_read = 0.0

        motors_cfg = self.config.get("motors", {})
        battery_register = motors_cfg.get("battery_register", "0x40")
        self.battery_register = int(battery_register, 16) if battery_register is not None else None
        counts_per_volt = motors_cfg.get("battery_counts_per_volt")
        self.battery_scale = float(motors_cfg.get("battery_scale", 0.01))
        if counts_per_volt is not None:
            try:
                counts_per_volt = float(counts_per_volt)
                if counts_per_volt > 0:
                    self.battery_scale = 1.0 / counts_per_volt
                    logger.info(
                        "Battery scale derived from counts_per_volt=%s -> %.6f",
                        counts_per_volt,
                        self.battery_scale,
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid battery_counts_per_volt value %r; falling back to battery_scale", 
                    counts_per_volt,
                )
        self.battery_offset = float(motors_cfg.get("battery_offset", 0.0))
        self.battery_full_voltage = float(motors_cfg.get("battery_full_voltage", 12.6))
        self.battery_empty_voltage = float(motors_cfg.get("battery_empty_voltage", 9.0))
        self.battery_refresh = float(motors_cfg.get("battery_refresh", 1.5))
        self._initialize_hardware()

    def _initialize_hardware(self):
        # I²C only (no GPIO on this build)
        try:
            self.bus = SMBus(1)  # Pi 4 default I²C bus
            logger.info(f"I2C motor controller ready at 0x{self.i2c_address:02X}")
        except Exception as e:
            logger.exception("Failed to initialize I2C bus")
            raise

    def _clamp(self, value, lo, hi):
        return max(lo, min(hi, value))

    def set_motor_speed(self, left_speed: int, right_speed: int):
        """
        left_speed/right_speed are -100..100 (percent). Map to your controller format here.
        """
        left_speed = self._clamp(int(left_speed), -self.max_speed, self.max_speed)
        right_speed = self._clamp(int(right_speed), -self.max_speed, self.max_speed)
        self.left_speed, self.right_speed = left_speed, right_speed

        # Example mapping: write signed speeds to two registers (adjust to your controller)
        try:
            # Convert -100..100 to 0..200 then shift to signed domain in controller as needed.
            # If your controller expects signed bytes directly: wrap to 0..255 with & 0xFF.
            self.bus.write_byte_data(self.i2c_address, 0x33, left_speed & 0xFF)
            self.bus.write_byte_data(self.i2c_address, 0x34, right_speed & 0xFF)
        except Exception:
            logger.exception("Failed writing motor speeds over I2C")

    def emergency_stop(self):
        try:
            self.set_motor_speed(0, 0)
            logger.info("Emergency stop issued.")
        except Exception:
            logger.exception("Emergency stop failed")

    # ------------------------------------------------------------------
    # Battery monitoring helpers
    # ------------------------------------------------------------------
    def _read_battery_voltage(self):
        """Read battery voltage from the motor controller via I²C."""
        if self.bus is None or self.battery_register is None:
            return None

        now = time.time()
        if self._battery_voltage is not None and (now - self._last_battery_read) < self.battery_refresh:
            return self._battery_voltage

        try:
            raw = self.bus.read_word_data(self.i2c_address, self.battery_register)
            low = raw & 0xFF
            high = (raw >> 8) & 0xFF
            scaled = raw * self.battery_scale

            # Remember the raw response so callers (and the web UI) can inspect it.
            self._battery_raw = raw
            self._battery_bytes = (low, high)

            # SMBus returns the low byte in the lower 8 bits already, so we
            # should not byte swap here. Swapping the bytes inflated the
            # reading (e.g. 12.5 V became ~500 V). Keep the native ordering
            # and apply the configured scale/offset so the UI shows the real
            # battery voltage.
            voltage = scaled + self.battery_offset
            self._battery_voltage = float(voltage)
            self._last_battery_read = now

            logger.info(
                "Motor controller battery read: raw=0x%04X (low=0x%02X high=0x%02X) "
                "scaled=%.5f offset=%.3f -> %.3f V",
                raw,
                low,
                high,
                scaled,
                self.battery_offset,
                self._battery_voltage,
            )
        except Exception:
            logger.exception("Failed reading battery voltage")
            self._battery_voltage = None
            self._battery_raw = None
            self._battery_bytes = (None, None)

        return self._battery_voltage

    def get_battery_status(self):
        voltage = self._read_battery_voltage()
        if voltage is None:
            return {
                "voltage": None,
                "percent": None,
                "raw": None,
                "raw_low_byte": None,
                "raw_high_byte": None,
            }

        span = max(0.1, self.battery_full_voltage - self.battery_empty_voltage)
        percent = (voltage - self.battery_empty_voltage) * 100.0 / span
        percent = self._clamp(percent, 0.0, 100.0)
        return {
            "voltage": round(voltage, 2),
            "percent": round(percent, 1),
            "raw": self._battery_raw,
            "raw_low_byte": self._battery_bytes[0],
            "raw_high_byte": self._battery_bytes[1],
        }

    def get_status(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            battery = self.get_battery_status()
            return {
                "motors": {"left": self.left_speed, "right": self.right_speed},
                "system": {"cpu": cpu, "mem": mem},
                "battery": battery,
            }
        except Exception:
            logger.exception("Status read failed")
            return {"motors": {"left": 0, "right": 0}, "system": {}, "battery": {}}

    def cleanup(self):
        try:
            if self.bus is not None:
                self.bus.close()
                self.bus = None
        except Exception:
            logger.exception("Cleanup failed")

