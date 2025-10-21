# backend/hardware_manager.py
import logging
import math
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
        self.battery_word_order = str(motors_cfg.get("battery_word_order", "auto")).lower()

        try:
            self.battery_shift = int(motors_cfg.get("battery_shift", 0))
        except (TypeError, ValueError):
            logger.warning("Invalid battery_shift value %r; defaulting to 0", motors_cfg.get("battery_shift"))
            self.battery_shift = 0

        try:
            self.battery_bits = int(motors_cfg.get("battery_bits", 16))
        except (TypeError, ValueError):
            logger.warning("Invalid battery_bits value %r; defaulting to 16", motors_cfg.get("battery_bits"))
            self.battery_bits = 16

        signed_cfg = motors_cfg.get("battery_signed", False)
        if isinstance(signed_cfg, str):
            self.battery_signed = signed_cfg.strip().lower() in {"1", "true", "yes", "on"}
        else:
            self.battery_signed = bool(signed_cfg)

        mask_cfg = motors_cfg.get("battery_mask")
        self.battery_mask = None
        if mask_cfg not in (None, ""):
            try:
                self.battery_mask = int(mask_cfg, 0) if isinstance(mask_cfg, str) else int(mask_cfg)
            except (TypeError, ValueError):
                logger.warning("Invalid battery_mask value %r; ignoring", mask_cfg)
                self.battery_mask = None

        self._battery_debug = {}
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

    def _evaluate_battery_candidates(self, raw_native: int, raw_swapped: int):
        bits = max(1, min(32, int(self.battery_bits or 16)))
        clamp_mask = (1 << bits) - 1

        raw_options = [
            {"order": "native", "raw": raw_native},
            {"order": "swapped", "raw": raw_swapped},
        ]

        candidates = []
        for option in raw_options:
            processed = option["raw"]
            if self.battery_mask is not None:
                processed &= self.battery_mask
            processed &= clamp_mask

            if self.battery_shift > 0:
                processed >>= self.battery_shift
            elif self.battery_shift < 0:
                processed <<= -self.battery_shift

            processed &= clamp_mask

            if self.battery_signed:
                sign_bit = 1 << (bits - 1)
                if processed & sign_bit:
                    processed -= (1 << bits)

            scaled = processed * self.battery_scale
            voltage = scaled + self.battery_offset

            candidates.append(
                {
                    "order": option["order"],
                    "raw": option["raw"],
                    "processed": processed,
                    "scaled": scaled,
                    "voltage": voltage,
                }
            )

        return candidates

    def _select_battery_candidate(self, candidates):
        if not candidates:
            return {
                "order": "native",
                "raw": 0,
                "processed": 0,
                "scaled": 0.0,
                "voltage": 0.0,
            }

        order_cfg = (self.battery_word_order or "native").lower()
        if order_cfg in {"native", "little", "lsb", "lsb_msb"}:
            desired = "native"
            for candidate in candidates:
                if candidate["order"] == desired:
                    return candidate
            return candidates[0]

        if order_cfg in {"swapped", "swap", "big", "msb", "msb_lsb"}:
            desired = "swapped"
            for candidate in candidates:
                if candidate["order"] == desired:
                    return candidate
            return candidates[-1]

        # Auto-detect: choose the candidate with the most reasonable voltage
        expected_min = self.battery_empty_voltage - 2.0
        expected_max = self.battery_full_voltage + 5.0
        expected_mid = (self.battery_full_voltage + self.battery_empty_voltage) / 2.0

        def score(candidate):
            voltage = candidate.get("voltage")
            if voltage is None or (isinstance(voltage, float) and voltage != voltage):
                return float("inf")
            if expected_min <= voltage <= expected_max:
                return abs(voltage - expected_mid)
            if voltage < expected_min:
                return (expected_min - voltage) + 10.0
            return (voltage - expected_max) + 10.0

        return min(candidates, key=score)

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
            raw_word = self.bus.read_word_data(self.i2c_address, self.battery_register)
            low = raw_word & 0xFF
            high = (raw_word >> 8) & 0xFF
            word_native = raw_word & 0xFFFF
            word_swapped = (low << 8) | high

            candidates = self._evaluate_battery_candidates(word_native, word_swapped)
            selected_candidate = self._select_battery_candidate(candidates) or {}

            def _safe_int(value):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            def _safe_float(value):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return None
                if math.isnan(value) or math.isinf(value):
                    return None
                return value

            bits = max(1, min(32, int(self.battery_bits or 16)))
            digits = max(4, math.ceil(bits / 4))

            selected_raw = _safe_int(selected_candidate.get("raw"))
            if selected_raw is None:
                selected_raw = word_native

            processed = _safe_int(selected_candidate.get("processed"))
            if processed is None:
                processed = selected_raw

            scaled = _safe_float(selected_candidate.get("scaled"))
            if scaled is None and processed is not None:
                scaled = processed * self.battery_scale
            if scaled is None:
                scaled = word_native * self.battery_scale

            voltage = _safe_float(selected_candidate.get("voltage"))
            if voltage is None:
                voltage = scaled + self.battery_offset

            applied_order = selected_candidate.get("order", "native")

            # Remember the raw response so callers (and the web UI) can inspect it.
            self._battery_raw = selected_raw
            self._battery_bytes = (low, high)
            self._battery_voltage = float(voltage)
            self._last_battery_read = now

            debug_candidates = []
            for candidate in candidates:
                debug_candidates.append(
                    {
                        "order": candidate.get("order"),
                        "raw": _safe_int(candidate.get("raw")),
                        "processed": _safe_int(candidate.get("processed")),
                        "scaled": _safe_float(candidate.get("scaled")),
                        "voltage": _safe_float(candidate.get("voltage")),
                    }
                )

            self._battery_debug = {
                "word_native": word_native,
                "word_swapped": word_swapped,
                "raw_selected": selected_raw,
                "configured_order": self.battery_word_order,
                "applied_order": applied_order,
                "processed": processed,
                "scaled": scaled,
                "candidates": debug_candidates,
                "bits": bits,
                "scale": self.battery_scale,
                "offset": self.battery_offset,
                "shift": self.battery_shift,
                "mask": self.battery_mask,
                "signed": self.battery_signed,
            }

            processed_masked = processed if processed is not None else 0
            mask = (1 << bits) - 1
            processed_hex = f"0x{processed_masked & mask:0{digits}X}" if processed is not None else "n/a"
            logger.info(
                "Motor controller battery read: native=0x%0*X swapped=0x%0*X order=%s processed=%s "
                "scaled=%.5f offset=%.3f -> %.3f V",
                digits,
                word_native,
                digits,
                word_swapped,
                applied_order,
                processed_hex,
                scaled,
                self.battery_offset,
                self._battery_voltage,
            )
        except Exception:
            logger.exception("Failed reading battery voltage")
            self._battery_voltage = None
            self._battery_raw = None
            self._battery_bytes = (None, None)
            self._battery_debug = {}

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
        debug_info = dict(self._battery_debug) if isinstance(self._battery_debug, dict) else {}
        return {
            "voltage": round(voltage, 2),
            "percent": round(percent, 1),
            "raw": self._battery_raw,
            "raw_low_byte": self._battery_bytes[0],
            "raw_high_byte": self._battery_bytes[1],
            "debug": debug_info,
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

