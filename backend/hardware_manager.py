# backend/hardware_manager.py
import logging
import math
import struct
import threading
import time
from collections import deque

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

        self.battery_config = self.config.get("battery", {})
        self.battery_register = self._parse_register(self.battery_config.get("voltage_register"))
        self.battery_scale = float(self.battery_config.get("voltage_scale", 0.01))
        self.battery_divider = float(self.battery_config.get("divider_ratio", 1.0))
        self.battery_cells = int(self.battery_config.get("cells", 0) or 0)
        self.battery_full = float(
            self.battery_config.get(
                "full_voltage",
                self.battery_cells * 4.2 if self.battery_cells else 0,
            )
        )
        self.battery_empty = float(
            self.battery_config.get(
                "empty_voltage",
                self.battery_cells * 3.3 if self.battery_cells else 0,
            )
        )
        self.warn_cell_voltage = float(self.battery_config.get("warn_cell_voltage", 0))
        self.critical_cell_voltage = float(self.battery_config.get("critical_cell_voltage", 0))
        self.battery_alpha = float(self.battery_config.get("ema_alpha", 0.3))
        self._battery_voltage = None

        self.encoder_config = self.config.get("encoders", {})
        self.left_encoder_register = self._parse_register(
            self.encoder_config.get("left_register")
        )
        self.right_encoder_register = self._parse_register(
            self.encoder_config.get("right_register")
        )
        self.encoder_total_register = self._parse_register(
            self.encoder_config.get("total_register")
        )
        self.encoder_reset_register = self._parse_register(
            self.encoder_config.get("reset_register")
        )
        reset_value = self._parse_register(self.encoder_config.get("reset_value"))
        self.encoder_reset_value = reset_value if reset_value is not None else 0

        self.encoder_left_indices = self._parse_index_list(
            self.encoder_config.get("left_indices")
        )
        self.encoder_right_indices = self._parse_index_list(
            self.encoder_config.get("right_indices")
        )
        self.encoder_total_count = int(
            self.encoder_config.get("total_count")
            or self._infer_total_count(self.encoder_left_indices, self.encoder_right_indices)
            or 0
        )

        counts_per_rev = float(self.encoder_config.get("counts_per_revolution", 0) or 0)
        gear_ratio = float(self.encoder_config.get("gear_ratio", 1.0) or 1.0)
        wheel_diameter_in = float(self.encoder_config.get("wheel_diameter_in", 0) or 0)
        distance_scale = float(self.encoder_config.get("distance_scale", 1.0) or 1.0)
        self.track_width_in = float(self.encoder_config.get("track_width_in", 0) or 0)
        self.distance_per_tick_in = 0.0
        if counts_per_rev > 0 and wheel_diameter_in > 0 and gear_ratio > 0:
            effective_counts = counts_per_rev * gear_ratio
            circumference = math.pi * wheel_diameter_in
            self.distance_per_tick_in = circumference / effective_counts

        if distance_scale > 0:
            self.distance_per_tick_in *= distance_scale

        self.motion_log = []
        self._last_command_time = None
        self._log_lock = threading.Lock()
        self._return_lock = threading.Lock()
        self._return_abort = None
        self.returning_to_start = False

        max_path_points = int(self.encoder_config.get("max_path_points", 600) or 600)
        self.path_points = deque(maxlen=max(2, max_path_points))
        self.path_points.append({"x": 0.0, "y": 0.0})
        self.last_encoder_counts = None
        self.odometry_pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
        self.total_distance_ft = 0.0
        self.odometry_sequence = 0

        self.odometry_enabled = (
            self.distance_per_tick_in > 0
            and self.track_width_in > 0
            and (
                (
                    self.left_encoder_register is not None
                    and self.right_encoder_register is not None
                )
                or (
                    self.encoder_total_register is not None
                    and self.encoder_total_count >= 2
                    and self.encoder_left_indices
                    and self.encoder_right_indices
                )
            )
        )

        self._initialize_hardware()

    def _initialize_hardware(self):
        # I²C only (no GPIO on this build)
        try:
            self.bus = SMBus(1)  # Pi 4 default I²C bus
            logger.info(f"I2C motor controller ready at 0x{self.i2c_address:02X}")
        except Exception:
            logger.exception("Failed to initialize I2C bus")
            self.bus = None

    def _clamp(self, value, lo, hi):
        return max(lo, min(hi, value))

    def _parse_register(self, value):
        if value is None:
            return None
        try:
            if isinstance(value, str):
                value = value.strip()
                if value.lower().startswith("0x"):
                    return int(value, 16)
                return int(value, 10)
            return int(value)
        except (TypeError, ValueError):
            logger.warning("Invalid register value %r", value)
            return None

    def _parse_index_list(self, value):
        if value is None:
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, (list, tuple)):
            indices = []
            for item in value:
                try:
                    indices.append(int(item))
                except (TypeError, ValueError):
                    logger.warning("Invalid encoder index %r", item)
            return indices
        try:
            return [int(value)]
        except (TypeError, ValueError):
            logger.warning("Invalid encoder index value %r", value)
            return []

    def _infer_total_count(self, left_indices, right_indices):
        if not left_indices and not right_indices:
            return 0
        highest = max(left_indices + right_indices)
        return highest + 1

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

    def set_motor_speed(self, left_speed: int, right_speed: int, *, source: str = "manual"):
        """
        left_speed/right_speed are -100..100 (percent). Map to your controller format here.
        """
        left_speed = self._clamp(int(left_speed), -self.max_speed, self.max_speed)
        right_speed = self._clamp(int(right_speed), -self.max_speed, self.max_speed)
        self.left_speed, self.right_speed = left_speed, right_speed

        if source != "auto":
            self._abort_return_to_start()

        self._record_motion_command(left_speed, right_speed, source=source)

        # Example mapping: write signed speeds to two registers (adjust to your controller)
        if self.bus is None:
            return

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
            status = {
                "motors": {"left": self.left_speed, "right": self.right_speed},
                "system": {"cpu": cpu, "mem": mem},
            }
            battery = self.get_battery_status()
            if battery:
                status["battery"] = battery
            status["odometry"] = self._odometry_snapshot()
            return status
        except Exception:
            logger.exception("Status read failed")
            return {"motors": {"left": 0, "right": 0}, "system": {}}

    def get_telemetry(self):
        telemetry = {
            "motors": {"left": self.left_speed, "right": self.right_speed},
        }

        battery = self.get_battery_status()
        if battery:
            telemetry["battery"] = battery

        if self.odometry_enabled:
            telemetry["odometry"] = self.update_odometry()
        else:
            telemetry["odometry"] = self._odometry_snapshot()

        return telemetry

    def read_battery_voltage(self):
        if self.bus is None or self.battery_register is None:
            return None

        try:
            data = self.bus.read_i2c_block_data(self.i2c_address, self.battery_register, 2)
            raw = data[0] | (data[1] << 8)
            voltage = raw * self.battery_scale
            if self.battery_divider > 0:
                voltage *= self.battery_divider

            if self._battery_voltage is None or self.battery_alpha <= 0:
                self._battery_voltage = voltage
            else:
                alpha = max(0.0, min(1.0, self.battery_alpha))
                self._battery_voltage = (
                    alpha * voltage + (1.0 - alpha) * self._battery_voltage
                )
            return self._battery_voltage
        except Exception:
            logger.exception("Failed reading battery voltage")
            return None

    def get_battery_status(self):
        voltage = self.read_battery_voltage()
        if voltage is None:
            return {}

        status = {"voltage": voltage}
        percent = None
        if self.battery_full > self.battery_empty:
            span = self.battery_full - self.battery_empty
            percent = (voltage - self.battery_empty) / span * 100.0
            percent = max(0.0, min(100.0, percent))
            status["percent"] = percent

        if self.battery_cells:
            cell_voltage = voltage / self.battery_cells
            status["cell_voltage"] = cell_voltage
        else:
            cell_voltage = None

        level = "normal"
        threshold = self.warn_cell_voltage
        critical = self.critical_cell_voltage
        compare_voltage = cell_voltage if cell_voltage else voltage
        if critical and compare_voltage <= critical:
            level = "critical"
        elif threshold and compare_voltage <= threshold:
            level = "warning"

        status["state"] = level
        return status

    def _read_encoder_counts(self):
        if self.bus is None:
            return None

        try:
            if (
                self.encoder_total_register is not None
                and self.encoder_total_count >= 2
                and (self.encoder_left_indices or self.encoder_right_indices)
            ):
                length = max(self.encoder_total_count * 4, 8)
                data = self.bus.read_i2c_block_data(
                    self.i2c_address, self.encoder_total_register, length
                )
                needed = self.encoder_total_count * 4
                if len(data) < needed:
                    logger.warning(
                        "Encoder block read returned %d bytes; expected %d",
                        len(data),
                        needed,
                    )
                    return None
                fmt = "<" + "i" * self.encoder_total_count
                counts = struct.unpack(fmt, bytes(data[:needed]))
                left_values = [
                    counts[i]
                    for i in self.encoder_left_indices
                    if 0 <= i < len(counts)
                ]
                right_values = [
                    counts[i]
                    for i in self.encoder_right_indices
                    if 0 <= i < len(counts)
                ]
                left = (
                    int(round(sum(left_values) / len(left_values)))
                    if left_values
                    else 0
                )
                right = (
                    int(round(sum(right_values) / len(right_values)))
                    if right_values
                    else 0
                )
                return {"left": left, "right": right}

            if self.left_encoder_register is None or self.right_encoder_register is None:
                return None

            left_bytes = self.bus.read_i2c_block_data(
                self.i2c_address, self.left_encoder_register, 4
            )
            right_bytes = self.bus.read_i2c_block_data(
                self.i2c_address, self.right_encoder_register, 4
            )
            left = struct.unpack("<i", bytes(left_bytes))[0]
            right = struct.unpack("<i", bytes(right_bytes))[0]
            return {"left": left, "right": right}
        except Exception:
            logger.exception("Failed reading encoder counts")
            return None

    def update_odometry(self):
        if not self.odometry_enabled:
            return self._odometry_snapshot()

        counts = self._read_encoder_counts()
        if counts is None:
            return self._odometry_snapshot()

        if self.last_encoder_counts is None:
            self.last_encoder_counts = counts
            return self._odometry_snapshot()

        delta_left = counts["left"] - self.last_encoder_counts["left"]
        delta_right = counts["right"] - self.last_encoder_counts["right"]

        if delta_left == 0 and delta_right == 0:
            return self._odometry_snapshot()

        self.last_encoder_counts = counts

        left_distance_in = delta_left * self.distance_per_tick_in
        right_distance_in = delta_right * self.distance_per_tick_in

        distance_in = (left_distance_in + right_distance_in) / 2.0
        delta_theta = (right_distance_in - left_distance_in) / self.track_width_in
        theta = self.odometry_pose["heading"]

        if abs(delta_theta) < 1e-9:
            dx = distance_in * math.cos(theta)
            dy = distance_in * math.sin(theta)
            segment_in = abs(distance_in)
        else:
            theta_new = theta + delta_theta
            radius = distance_in / delta_theta
            dx = radius * (math.sin(theta_new) - math.sin(theta))
            dy = -radius * (math.cos(theta_new) - math.cos(theta))
            segment_in = abs(delta_theta * radius)

        self.odometry_pose["x"] += dx
        self.odometry_pose["y"] += dy
        self.odometry_pose["heading"] = ((theta + delta_theta + math.pi) % (2 * math.pi)) - math.pi

        self.total_distance_ft += segment_in / 12.0

        self.path_points.append(
            {
                "x": self.odometry_pose["x"] / 12.0,
                "y": self.odometry_pose["y"] / 12.0,
            }
        )

        return self._odometry_snapshot()

    def _odometry_snapshot(self):
        return {
            "pose": {
                "x": self.odometry_pose["x"] / 12.0,
                "y": self.odometry_pose["y"] / 12.0,
                "heading_rad": self.odometry_pose["heading"],
            },
            "total_distance_ft": self.total_distance_ft,
            "path": list(self.path_points),
            "sequence": self.odometry_sequence,
            "return_available": self._has_unconsumed_manual_entries(),
            "return_in_progress": self._is_return_in_progress(),
        }

    def reset_odometry(self):
        self._abort_return_to_start()
        self.last_encoder_counts = None
        self.odometry_pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
        self.total_distance_ft = 0.0
        self.odometry_sequence += 1
        self.path_points.clear()
        self.path_points.append({"x": 0.0, "y": 0.0})
        self.reset_motion_log()

        if self.bus is None or self.encoder_reset_register is None:
            return

        try:
            self.bus.write_byte_data(
                self.i2c_address,
                self.encoder_reset_register,
                self.encoder_reset_value & 0xFF,
            )
        except Exception:
            logger.exception("Failed to reset encoder counters over I2C")

    def reset_motion_log(self):
        with self._log_lock:
            self.motion_log.clear()
            self._last_command_time = None

    def begin_return_to_start(self, on_complete=None):
        with self._return_lock:
            if self.returning_to_start:
                return False, "Return-to-start already in progress."

            segments = self._collect_return_segments()
            if not segments:
                return False, "No recorded motion available to retrace."

            self.returning_to_start = True
            self._return_abort = threading.Event()

        logger.info("Starting return-to-start sequence (%d segments)", len(segments))

        def worker():
            result = {"success": True, "reason": "complete"}
            try:
                for segment in reversed(segments):
                    if self._return_abort and self._return_abort.is_set():
                        result = {"success": False, "reason": "aborted"}
                        break

                    duration = float(segment["duration"] or 0.0)
                    if duration <= 0:
                        continue

                    left = -segment["left"]
                    right = -segment["right"]

                    if left == 0 and right == 0:
                        continue

                    self.set_motor_speed(left, right, source="auto")

                    end_time = time.monotonic() + duration
                    while time.monotonic() < end_time:
                        if self._return_abort and self._return_abort.is_set():
                            result = {"success": False, "reason": "aborted"}
                            break
                        time.sleep(0.05)

                    if not result.get("success", False):
                        break
            except Exception:
                logger.exception("Return-to-start execution failed")
                result = {"success": False, "reason": "error"}
            finally:
                try:
                    self.set_motor_speed(0, 0, source="auto")
                except Exception:
                    logger.exception("Failed to stop motors after return-to-start")

                if result.get("success"):
                    with self._log_lock:
                        for segment in segments:
                            entry = segment.get("entry")
                            if entry and entry.get("source") == "manual":
                                entry["consumed"] = True

                with self._return_lock:
                    self.returning_to_start = False
                    self._return_abort = None

                if on_complete:
                    try:
                        on_complete(result)
                    except Exception:
                        logger.exception("Return-to-start completion callback failed")

            logger.info(
                "Return-to-start finished: %s", result.get("reason", "unknown")
            )

        threading.Thread(target=worker, daemon=True).start()
        return True, "Returning to start"

    def _record_motion_command(self, left_speed, right_speed, *, source):
        now = time.monotonic()
        with self._log_lock:
            if self.motion_log and self._last_command_time is not None:
                elapsed = max(0.0, now - self._last_command_time)
                self.motion_log[-1]["duration"] += elapsed

            if self.motion_log:
                last = self.motion_log[-1]
                if (
                    last["left"] == left_speed
                    and last["right"] == right_speed
                    and last.get("source") == source
                ):
                    self._last_command_time = now
                    return

            entry = {
                "left": left_speed,
                "right": right_speed,
                "duration": 0.0,
                "source": source,
            }

            if source == "manual":
                entry["consumed"] = False

            self.motion_log.append(entry)
            self._last_command_time = now

    def _collect_return_segments(self):
        now = time.monotonic()
        with self._log_lock:
            if self.motion_log and self._last_command_time is not None:
                elapsed = max(0.0, now - self._last_command_time)
                self.motion_log[-1]["duration"] += elapsed
                self._last_command_time = now

            segments = []
            for entry in self.motion_log:
                if entry.get("source") != "manual" or entry.get("consumed"):
                    continue
                duration = float(entry.get("duration") or 0.0)
                if duration <= 0:
                    continue
                segments.append(
                    {
                        "entry": entry,
                        "left": entry["left"],
                        "right": entry["right"],
                        "duration": duration,
                    }
                )

            return segments

    def _has_unconsumed_manual_entries(self):
        with self._log_lock:
            for entry in self.motion_log:
                if (
                    entry.get("source") == "manual"
                    and not entry.get("consumed")
                    and float(entry.get("duration") or 0.0) > 0
                ):
                    return True
        return False

    def _is_return_in_progress(self):
        with self._return_lock:
            return self.returning_to_start

    def _abort_return_to_start(self):
        with self._return_lock:
            if self._return_abort:
                self._return_abort.set()

    def cleanup(self):
        try:
            if self.bus is not None:
                self.bus.close()
                self.bus = None
        except Exception:
            logger.exception("Cleanup failed")

