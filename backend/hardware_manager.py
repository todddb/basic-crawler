# backend/hardware_manager.py
import logging

import psutil
from smbus2 import SMBus

MOTOR_SPEED_REGISTER_BASE = 0x33

logger = logging.getLogger("utils")


class HardwareManager:
    def __init__(self, config):
        self.config = config
        self.i2c_address = int(self.config.get("motors", {}).get("i2c_address", "0x34"), 16)
        self.left_channel = int(self.config.get("motors", {}).get("left_channel", 0))
        self.right_channel = int(self.config.get("motors", {}).get("right_channel", 1))
        self.max_speed = int(self.config.get("motors", {}).get("max_speed", 100))
        self.speed_register_base = MOTOR_SPEED_REGISTER_BASE
        self.left_speed_register = self._compute_motor_register(self.left_channel, "left")
        self.right_speed_register = self._compute_motor_register(self.right_channel, "right")
        logger.info(
            "Motor controller channels: left=%s -> 0x%02X, right=%s -> 0x%02X",
            self.left_channel,
            self.left_speed_register,
            self.right_channel,
            self.right_speed_register,
        )
        self.bus = None
        self.left_speed = 0
        self.right_speed = 0

        self._initialize_hardware()

    def _initialize_hardware(self):
        # I²C only (no GPIO on this build)
        try:
            self.bus = SMBus(1)  # Pi 4 default I²C bus
            logger.info(f"I2C motor controller ready at 0x{self.i2c_address:02X}")
        except Exception:
            logger.exception("Failed to initialize I2C bus")
            raise

    def _clamp(self, value, lo, hi):
        return max(lo, min(hi, value))

    def _compute_motor_register(self, channel, label):
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            logger.warning("Invalid %s motor channel %r; defaulting to 0", label, channel)
            channel = 0

        register = self.speed_register_base + channel
        if register < 0 or register > 0xFF:
            logger.warning(
                "%s motor channel %s maps to out-of-range register 0x%02X; using base register 0x%02X",
                label.capitalize(),
                channel,
                register & 0xFFFF,
                self.speed_register_base,
            )
            return self.speed_register_base

        return register & 0xFF

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
            self.bus.write_byte_data(self.i2c_address, self.left_speed_register, left_speed & 0xFF)
            self.bus.write_byte_data(self.i2c_address, self.right_speed_register, right_speed & 0xFF)
        except Exception:
            logger.exception("Failed writing motor speeds over I2C")

    def emergency_stop(self):
        try:
            self.set_motor_speed(0, 0)
            logger.info("Emergency stop issued.")
        except Exception:
            logger.exception("Emergency stop failed")

    def get_status(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            return {
                "motors": {"left": self.left_speed, "right": self.right_speed},
                "system": {"cpu": cpu, "mem": mem},
            }
        except Exception:
            logger.exception("Status read failed")
            return {"motors": {"left": 0, "right": 0}, "system": {}}

    def cleanup(self):
        try:
            if self.bus is not None:
                self.bus.close()
                self.bus = None
        except Exception:
            logger.exception("Cleanup failed")

