# backend/camera_manager.py
import logging
import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("camera_manager")


QUALITY_PROFILES = {
    "low": {
        "front": {"resolution": (640, 360), "fps": 24, "quality": 68},
        "rear": {"resolution": (320, 180), "fps": 18, "quality": 60},
    },
    "balanced": {
        "front": {"resolution": (768, 432), "fps": 24, "quality": 78},
        "rear": {"resolution": (320, 240), "fps": 20, "quality": 70},
    },
    "high": {
        "front": {"resolution": (960, 540), "fps": 30, "quality": 85},
        "rear": {"resolution": (480, 360), "fps": 24, "quality": 78},
    },
}


def _make_blank_jpeg(text: str, size: Tuple[int, int] = (640, 480)) -> bytes:
    """Return a simple black JPEG with centered text; used as a placeholder when no frame is available."""
    w, h = size
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    cv2.putText(frame, text, (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    return buf.tobytes() if ok else b""


class CameraManager:
    """
    Front: Picamera2 (if available/configured)
    Rear : Picamera2 or USB camera via OpenCV (auto-detected /dev/videoN)
    """

    def __init__(self, config: dict):
        self.config = config or {}

        # --- Configuration ---------------------------------------------------
        cam_cfg = (self.config.get("cameras") or {})
        f_cfg = cam_cfg.get("front", {}) or {}
        r_cfg = cam_cfg.get("rear", {}) or {}

        self.front_type = (f_cfg.get("type") or "picamera2").lower()
        self.rear_type = (r_cfg.get("type") or "usb").lower()

        def _resolve_camera_id(label: str, cfg: dict, default: Optional[int]) -> Optional[int]:
            for key in ("camera_id", "sensor_id"):
                if key in cfg and cfg[key] is not None:
                    try:
                        return int(cfg[key])
                    except (TypeError, ValueError):
                        logger.warning(
                            "%s camera configuration has invalid %s=%r; falling back to %r.",
                            label,
                            key,
                            cfg[key],
                            default,
                        )
            return default

        self.front_camera_id = _resolve_camera_id(
            "front", f_cfg, 0 if self.front_type == "picamera2" else None
        )
        self.rear_camera_id = _resolve_camera_id(
            "rear", r_cfg, 1 if self.rear_type == "picamera2" else None
        )

        # Front (PiCam) desired settings
        self.front_res = tuple(f_cfg.get("resolution", (1280, 720)))
        self.front_fps = int(f_cfg.get("fps", 30))
        self.front_quality = int(f_cfg.get("quality", 85))

        # Rear (USB) desired settings
        self.rear_res = tuple(r_cfg.get("resolution", (640, 480)))
        self.rear_fps = int(r_cfg.get("fps", 15))
        self.rear_quality = int(r_cfg.get("quality", 75))
        self.current_profile = self._detect_profile()

        # --- Runtime state ---------------------------------------------------
        # Front camera runtime state
        self.front_supported = False
        self.front_active = False
        self.front_backend: Optional[str] = None  # "picamera2" (default) or "gstreamer"
        self._front_thread: Optional[threading.Thread] = None
        self._front_stop = threading.Event()
        self._front_lock = threading.Lock()
        self._front_last_jpeg: Optional[bytes] = None
        self._front_cap: Optional[cv2.VideoCapture] = None
        self._picam2 = None

        # Rear camera runtime state
        self.rear_supported = False
        self.rear_active = False
        self.rear_backend: Optional[str] = None  # mirrors front backend behaviour
        self._rear_thread: Optional[threading.Thread] = None
        self._rear_stop = threading.Event()
        self._rear_lock = threading.Lock()
        self._rear_last_jpeg: Optional[bytes] = None
        self._rear_cap: Optional[cv2.VideoCapture] = None
        self._rear_index: Optional[int] = None
        self._rear_picam2 = None

        # Probes
        self.front_supported = self._probe_front_camera()
        self.rear_supported = self._probe_rear_camera()

        # Placeholders so the UI shows *something* even before frames arrive
        if not self._front_last_jpeg:
            self._front_last_jpeg = _make_blank_jpeg("Front camera not started", self.front_res)
        if not self._rear_last_jpeg:
            self._rear_last_jpeg = _make_blank_jpeg("Rear camera not started", self.rear_res)

    # ---------------------- Quality profiles ---------------------------------

    def _detect_profile(self) -> str:
        for name, profile in QUALITY_PROFILES.items():
            front = profile.get("front", {})
            rear = profile.get("rear", {})
            if (tuple(front.get("resolution", ())) == tuple(self.front_res)
                    and int(front.get("fps", 0)) == int(self.front_fps)
                    and int(front.get("quality", 0)) == int(self.front_quality)
                    and tuple(rear.get("resolution", ())) == tuple(self.rear_res)
                    and int(rear.get("fps", 0)) == int(self.rear_fps)
                    and int(rear.get("quality", 0)) == int(self.rear_quality)):
                return name
        return "custom"

    def apply_quality_profile(self, profile_name: str) -> bool:
        profile_key = (profile_name or "").lower()
        profile = QUALITY_PROFILES.get(profile_key)
        if not profile:
            logger.warning("Unknown camera quality profile '%s'", profile_name)
            return False

        front_was_running = self.front_active
        rear_was_running = self.rear_active

        if front_was_running:
            self._stop_front_thread()
        if rear_was_running:
            self._stop_rear_thread()

        f_cfg = profile.get("front", {})
        r_cfg = profile.get("rear", {})

        self.front_res = tuple(f_cfg.get("resolution", self.front_res))
        self.front_fps = int(f_cfg.get("fps", self.front_fps))
        self.front_quality = int(f_cfg.get("quality", self.front_quality))

        self.rear_res = tuple(r_cfg.get("resolution", self.rear_res))
        self.rear_fps = int(r_cfg.get("fps", self.rear_fps))
        self.rear_quality = int(r_cfg.get("quality", self.rear_quality))

        # Persist back into config so the selection survives restarts when saved.
        cam_cfg = self.config.setdefault("cameras", {})
        cam_cfg.setdefault("front", {})
        cam_cfg.setdefault("rear", {})
        cam_cfg["front"].update({
            "resolution": list(self.front_res),
            "fps": self.front_fps,
            "quality": self.front_quality,
        })
        cam_cfg["rear"].update({
            "resolution": list(self.rear_res),
            "fps": self.rear_fps,
            "quality": self.rear_quality,
        })

        self.current_profile = profile_key
        logger.info("Applied camera quality profile '%s'", self.current_profile)

        restart_errors = False
        if front_was_running:
            try:
                self._start_front_thread()
            except Exception:
                restart_errors = True
                logger.exception("Failed to restart front camera after quality change")
        if rear_was_running:
            try:
                self._start_rear_thread()
            except Exception:
                restart_errors = True
                logger.exception("Failed to restart rear camera after quality change")

        return not restart_errors

    def get_status(self) -> dict:
        return {
            "front": {
                "active": self.front_active,
                "supported": self.front_supported,
                "type": self.front_type,
                "camera_id": self.front_camera_id,
                "resolution": list(self.front_res),
                "fps": self.front_fps,
                "quality": self.front_quality,
            },
            "rear": {
                "active": self.rear_active,
                "supported": self.rear_supported,
                "type": self.rear_type,
                "camera_id": self.rear_camera_id,
                "resolution": list(self.rear_res),
                "fps": self.rear_fps,
                "quality": self.rear_quality,
            },
            "profile": self.current_profile,
        }

    # ---------------------- Public API ---------------------------------------

    def start_cameras(self, want_front: bool = True, want_rear: bool = True) -> dict:
        """
        Start requested cameras; tolerate partial success.
        Returns: {"front": bool, "rear": bool}
        """
        results = {"front": False, "rear": False}

        if want_front and self.front_supported and not self.front_active:
            try:
                self._start_front_thread()
                results["front"] = True
            except Exception:
                logger.exception("Failed to start FRONT camera")
                self.front_active = False
        else:
            results["front"] = self.front_active

        if want_rear and self.rear_supported and not self.rear_active:
            try:
                self._start_rear_thread()
                results["rear"] = True
            except Exception:
                logger.exception("Failed to start REAR camera")
                self.rear_active = False
        else:
            results["rear"] = self.rear_active

        return results

    def stop_cameras(self):
        self._stop_front_thread()
        self._stop_rear_thread()

    def shutdown(self):
        """Alias for stop + cleanup resources."""
        self.stop_cameras()

    def cleanup(self):
        """Public cleanup hook used by the app on shutdown."""
        self.stop_cameras()
        # Drop references so large frame buffers can be GC'd promptly
        with self._front_lock:
            self._front_last_jpeg = None
        with self._rear_lock:
            self._rear_last_jpeg = None

    def get_latest_frame(self, which: str) -> Optional[bytes]:
        """Return the latest JPEG frame for 'front' or 'rear'."""
        if which == "front":
            with self._front_lock:
                return self._front_last_jpeg
        elif which == "rear":
            with self._rear_lock:
                return self._rear_last_jpeg
        return None

    def mjpeg_generator(self, which: str, boundary: str = "frame"):
        """
        Flask route helper:
        return Response(camera_manager.mjpeg_generator("front"), mimetype="multipart/x-mixed-replace; boundary=frame")
        """
        placeholder = _make_blank_jpeg(f"{which.capitalize()} camera unavailable",
                                       self.front_res if which == "front" else self.rear_res)
        while True:
            frame = self.get_latest_frame(which) or placeholder
            yield (
                b"--" + boundary.encode("ascii") + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode("ascii") + b"\r\n\r\n" +
                frame + b"\r\n"
            )
            # 30 fps cap for MJPEG stream pacing (adjust if needed)
            time.sleep(1 / 30.0)

    # ---------------------- Probing ------------------------------------------

    def _probe_front_camera(self) -> bool:
        if self.front_type == "picamera2":
            return self._probe_picamera2(self.front_camera_id, "front")

        if self.front_type == "usb":
            logger.info(
                "Front camera configured for USB; front USB streaming is not currently implemented."
            )
            return False

        logger.warning("Unknown front camera type '%s'; disabling front camera.", self.front_type)
        return False

    def _probe_rear_camera(self) -> bool:
        if self.rear_type == "picamera2":
            self._rear_index = None
            return self._probe_picamera2(self.rear_camera_id, "rear")

        if self.rear_type == "usb":
            supported, index = self._probe_usb_camera("rear")
            self._rear_index = index if supported else None
            return supported

        logger.warning("Unknown rear camera type '%s'; disabling rear camera.", self.rear_type)
        self._rear_index = None
        return False

    def _probe_picamera2(self, camera_id: Optional[int], which: str) -> bool:
        """Instantiate Picamera2 with the requested sensor ID to confirm availability."""
        try:
            from picamera2 import Picamera2
        except ModuleNotFoundError as exc:
            logger.info(
                "Picamera2 not available for %s camera (%s); install with 'sudo apt install "
                "python3-picamera2' to enable native support. Falling back to libcamerasrc.",
                which,
                exc,
            )
            return self._probe_libcamera_gstreamer(which, camera_id)
        except Exception as exc:
            logger.warning(
                "Picamera2 import failed for %s camera (camera_id=%s): %s. Attempting libcamerasrc fallback.",
                which,
                camera_id,
                exc,
            )
            return self._probe_libcamera_gstreamer(which, camera_id)

        cam = None
        try:
            kwargs = {}
            if camera_id is not None:
                kwargs["camera_num"] = int(camera_id)
            cam = Picamera2(**kwargs)
            if which == "front":
                self.front_backend = "picamera2"
            else:
                self.rear_backend = "picamera2"
            logger.info(
                "Picamera2 detected for %s camera (camera_id=%s).",
                which,
                camera_id if camera_id is not None else "default",
            )
            return True
        except Exception as exc:
            logger.warning(
                "Failed to initialize Picamera2 for %s camera (camera_id=%s): %s",
                which,
                camera_id,
                exc,
            )
            return self._probe_libcamera_gstreamer(which, camera_id)
        finally:
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass

    def _probe_usb_camera(self, which: str) -> Tuple[bool, Optional[int]]:
        """Try to open /dev/videoN devices to find a working USB camera."""
        for idx in (0, 1, 2, 3):
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                continue
            ok, _ = cap.read()
            cap.release()
            if ok:
                logger.info("%s camera will use /dev/video%d", which.capitalize(), idx)
                return True, idx

        logger.info("No USB camera detected for %s camera on /dev/video[0-3].", which)
        return False, None

    def _probe_libcamera_gstreamer(self, which: str, camera_id: Optional[int]) -> bool:
        """Attempt to stream using libcamerasrc via OpenCV/GStreamer as a fallback."""
        pipeline = self._build_gstreamer_pipeline(which, camera_id)
        if not pipeline:
            return False

        cap = self._open_gstreamer_capture(pipeline)
        if cap is None or not cap.isOpened():
            logger.info(
                "libcamerasrc fallback failed to open for %s camera (pipeline=%s)",
                which,
                pipeline,
            )
            if cap is not None:
                cap.release()
            return False

        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            logger.info("libcamerasrc fallback produced no frames for %s camera", which)
            return False

        if which == "front":
            self.front_backend = "gstreamer"
        else:
            self.rear_backend = "gstreamer"

        logger.info("%s camera streaming via libcamerasrc (GStreamer) backend.", which.capitalize())
        return True

    # ---------------------- Helper utilities ---------------------------------

    def _build_gstreamer_pipeline(self, which: str, camera_id: Optional[int]) -> Optional[str]:
        """Construct a libcamerasrc pipeline string for the requested camera."""
        if which == "front":
            width, height = self.front_res
            fps = int(max(1, self.front_fps))
        else:
            width, height = self.rear_res
            fps = int(max(1, self.rear_fps))

        # libcamerasrc currently selects the first camera when no name is provided.
        # Passing camera-name is fragile across OS versions, so we rely on default
        # ordering for the fallback to maximise compatibility.
        src_props = []
        if camera_id is not None:
            try:
                src_props.append(f"camera-id={int(camera_id)}")
            except (TypeError, ValueError):
                logger.debug("Invalid camera_id %r for %s camera; ignoring.", camera_id, which)

        src = "libcamerasrc"
        if src_props:
            src = f"{src} {' '.join(src_props)}"
        caps = f"video/x-raw,width={int(width)},height={int(height)},framerate={fps}/1"
        pipeline = (
            f"{src} ! {caps} ! videoconvert ! video/x-raw,format=BGR ! "
            "appsink drop=true max-buffers=1"
        )
        return pipeline

    def _open_gstreamer_capture(self, pipeline: str) -> Optional[cv2.VideoCapture]:
        """Open a GStreamer pipeline via OpenCV, trying CAP_GSTREAMER first."""
        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if cap.isOpened():
            return cap
        cap.release()
        cap = cv2.VideoCapture(pipeline)
        if cap.isOpened():
            return cap
        cap.release()
        return None

    # ---------------------- Front (Picamera2) thread -------------------------

    def _start_front_thread(self):
        if self._front_thread and self._front_thread.is_alive():
            return
        if self.front_type != "picamera2":
            raise RuntimeError(
                f"Front camera type '{self.front_type}' is not supported for streaming threads."
            )

        backend = self.front_backend or "picamera2"
        if backend == "picamera2":
            logger.info(
                "Initializing Picamera2 (front, camera_id=%s)...",
                self.front_camera_id if self.front_camera_id is not None else "default",
            )
            target = self._front_loop
        elif backend == "gstreamer":
            pipeline = self._build_gstreamer_pipeline("front", self.front_camera_id)
            logger.info(
                "Initializing libcamerasrc (front) via GStreamer pipeline: %s",
                pipeline,
            )
            target = self._front_loop_gstreamer
        else:
            raise RuntimeError("Front camera backend not available")

        self._front_stop.clear()
        self._front_thread = threading.Thread(target=target, name="front_cam", daemon=True)
        self._front_thread.start()
        # Wait a moment to confirm frames begin
        time.sleep(0.25)
        self.front_active = True

    def _stop_front_thread(self):
        if not self._front_thread:
            return
        self._front_stop.set()
        self._front_thread.join(timeout=2.0)
        self._front_thread = None
        # Close camera instance
        if self.front_backend == "picamera2" and self._picam2 is not None:
            try:
                self._picam2.stop()
            except Exception:
                pass
            try:
                self._picam2.close()
            except Exception:
                pass
            self._picam2 = None
        elif self.front_backend == "gstreamer" and self._front_cap is not None:
            try:
                self._front_cap.release()
            except Exception:
                pass
            self._front_cap = None
        self.front_active = False
        logger.info("Front camera stopped.")

    def _front_loop(self):
        try:
            from picamera2 import Picamera2
            kwargs = {}
            if self.front_camera_id is not None:
                kwargs["camera_num"] = int(self.front_camera_id)
            self._picam2 = Picamera2(**kwargs)

            # Configure for streaming video
            # Use RGB888 to avoid color surprises; convert to BGR for OpenCV JPEG encode.
            video_config = self._picam2.create_video_configuration(
                main={"size": tuple(self.front_res), "format": "RGB888"},
                buffer_count=4,
            )
            self._picam2.configure(video_config)
            self._picam2.start()

            target_delay = 1.0 / max(1, self.front_fps)
            quality = int(self.front_quality)

            while not self._front_stop.is_set():
                t0 = time.time()
                frame_rgb = self._picam2.capture_array()
                # Robustness: sometimes None may occur if pipeline hiccups
                if frame_rgb is None or not isinstance(frame_rgb, np.ndarray):
                    time.sleep(0.01)
                    continue

                # Picamera2 returns frames that are already in BGR order for JPEG encoding.
                # Avoid swapping channels so the streamed colours remain natural.
                frame_bgr = np.ascontiguousarray(frame_rgb)

                # Flip horizontally so the feed mirrors the actual orientation
                frame_bgr = cv2.flip(frame_bgr, 1)
                ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    with self._front_lock:
                        self._front_last_jpeg = buf.tobytes()

                # Pace loop to approximate requested FPS
                elapsed = time.time() - t0
                if elapsed < target_delay:
                    time.sleep(target_delay - elapsed)

        except Exception:
            logger.exception("Front camera loop crashed")
        finally:
            # Ensure hardware is released
            try:
                if self._picam2 is not None:
                    self._picam2.stop()
            except Exception:
                pass
            try:
                if self._picam2 is not None:
                    self._picam2.close()
            except Exception:
                pass
            self._picam2 = None
            self.front_active = False

    def _front_loop_gstreamer(self):
        cap = None
        try:
            pipeline = self._build_gstreamer_pipeline("front", self.front_camera_id)
            cap = self._open_gstreamer_capture(pipeline)
            if cap is None or not cap.isOpened():
                logger.error("Failed to open GStreamer pipeline for front camera: %s", pipeline)
                return

            self._front_cap = cap
            target_delay = 1.0 / max(1, self.front_fps)
            quality = int(self.front_quality)

            while not self._front_stop.is_set():
                t0 = time.time()
                ok, frame_bgr = cap.read()
                if not ok or frame_bgr is None:
                    time.sleep(0.02)
                    continue

                frame_bgr = np.ascontiguousarray(frame_bgr)
                h, w = frame_bgr.shape[:2]
                if (w, h) != tuple(self.front_res):
                    frame_bgr = cv2.resize(frame_bgr, self.front_res, interpolation=cv2.INTER_AREA)

                frame_bgr = cv2.flip(frame_bgr, 1)
                ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    with self._front_lock:
                        self._front_last_jpeg = buf.tobytes()

                elapsed = time.time() - t0
                if elapsed < target_delay:
                    time.sleep(target_delay - elapsed)

        except Exception:
            logger.exception("Front camera GStreamer loop crashed")
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            self._front_cap = None
            self.front_active = False

    # ---------------------- Rear camera thread -------------------------------

    def _start_rear_thread(self):
        if self._rear_thread and self._rear_thread.is_alive():
            return

        self._rear_stop.clear()
        if self.rear_type == "picamera2":
            backend = self.rear_backend or "picamera2"
            if backend == "picamera2":
                logger.info(
                    "Initializing Picamera2 (rear, camera_id=%s)...",
                    self.rear_camera_id if self.rear_camera_id is not None else "default",
                )
                target = self._rear_loop_picamera2
            elif backend == "gstreamer":
                pipeline = self._build_gstreamer_pipeline("rear", self.rear_camera_id)
                logger.info(
                    "Initializing libcamerasrc (rear) via GStreamer pipeline: %s",
                    pipeline,
                )
                target = self._rear_loop_gstreamer
            else:
                raise RuntimeError("Rear camera backend not available")
        elif self.rear_type == "usb":
            if self._rear_index is None:
                raise RuntimeError("No USB camera index available.")
            logger.info(f"Initializing USB camera (rear) on /dev/video{self._rear_index}...")
            target = self._rear_loop_usb
        else:
            raise RuntimeError(f"Unsupported rear camera type '{self.rear_type}'.")

        self._rear_thread = threading.Thread(target=target, name="rear_cam", daemon=True)
        self._rear_thread.start()
        # Wait a moment to confirm frames begin
        time.sleep(0.25)
        self.rear_active = True

    def _stop_rear_thread(self):
        if not self._rear_thread:
            return
        self._rear_stop.set()
        self._rear_thread.join(timeout=2.0)
        self._rear_thread = None

        if self.rear_type == "picamera2":
            if self.rear_backend == "picamera2" and self._rear_picam2 is not None:
                try:
                    self._rear_picam2.stop()
                except Exception:
                    pass
                try:
                    self._rear_picam2.close()
                except Exception:
                    pass
                self._rear_picam2 = None
            elif self.rear_backend == "gstreamer" and self._rear_cap is not None:
                try:
                    self._rear_cap.release()
                except Exception:
                    pass
                self._rear_cap = None
        else:
            if self._rear_cap is not None:
                try:
                    self._rear_cap.release()
                except Exception:
                    pass
                self._rear_cap = None

        self.rear_active = False
        logger.info("Rear camera stopped.")

    def _rear_loop_usb(self):
        cap = None
        try:
            cap = cv2.VideoCapture(self._rear_index, cv2.CAP_V4L2)
            self._rear_cap = cap

            # Try to set requested resolution/fps; ignore failures
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.rear_res[0]))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.rear_res[1]))
            cap.set(cv2.CAP_PROP_FPS, float(self.rear_fps))

            target_delay = 1.0 / max(1, self.rear_fps)
            quality = int(self.rear_quality)

            while not self._rear_stop.is_set():
                t0 = time.time()
                ok, frame_bgr = cap.read()
                if not ok or frame_bgr is None:
                    # Camera hiccup; small backoff
                    time.sleep(0.01)
                    continue

                # Optionally resize to requested resolution if device ignored set()
                h, w = frame_bgr.shape[:2]
                if (w, h) != tuple(self.rear_res):
                    frame_bgr = cv2.resize(frame_bgr, self.rear_res, interpolation=cv2.INTER_AREA)

                frame_bgr = cv2.flip(frame_bgr, 0)

                ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    with self._rear_lock:
                        self._rear_last_jpeg = buf.tobytes()

                # Pace
                elapsed = time.time() - t0
                if elapsed < target_delay:
                    time.sleep(target_delay - elapsed)

        except Exception:
            logger.exception("Rear camera USB loop crashed")
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            self._rear_cap = None
            self.rear_active = False

    def _rear_loop_picamera2(self):
        try:
            from picamera2 import Picamera2
            kwargs = {}
            if self.rear_camera_id is not None:
                kwargs["camera_num"] = int(self.rear_camera_id)
            self._rear_picam2 = Picamera2(**kwargs)

            video_config = self._rear_picam2.create_video_configuration(
                main={"size": tuple(self.rear_res), "format": "RGB888"},
                buffer_count=4,
            )
            self._rear_picam2.configure(video_config)
            self._rear_picam2.start()

            target_delay = 1.0 / max(1, self.rear_fps)
            quality = int(self.rear_quality)

            while not self._rear_stop.is_set():
                t0 = time.time()
                frame_rgb = self._rear_picam2.capture_array()
                if frame_rgb is None or not isinstance(frame_rgb, np.ndarray):
                    time.sleep(0.01)
                    continue

                frame_bgr = np.ascontiguousarray(frame_rgb)
                frame_bgr = cv2.flip(frame_bgr, 0)

                ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    with self._rear_lock:
                        self._rear_last_jpeg = buf.tobytes()

                elapsed = time.time() - t0
                if elapsed < target_delay:
                    time.sleep(target_delay - elapsed)

        except Exception:
            logger.exception("Rear camera Picamera2 loop crashed")
        finally:
            try:
                if self._rear_picam2 is not None:
                    self._rear_picam2.stop()
            except Exception:
                pass
            try:
                if self._rear_picam2 is not None:
                    self._rear_picam2.close()
            except Exception:
                pass
            self._rear_picam2 = None
            self.rear_active = False

    def _rear_loop_gstreamer(self):
        cap = None
        try:
            pipeline = self._build_gstreamer_pipeline("rear", self.rear_camera_id)
            cap = self._open_gstreamer_capture(pipeline)
            if cap is None or not cap.isOpened():
                logger.error("Failed to open GStreamer pipeline for rear camera: %s", pipeline)
                return

            self._rear_cap = cap
            target_delay = 1.0 / max(1, self.rear_fps)
            quality = int(self.rear_quality)

            while not self._rear_stop.is_set():
                t0 = time.time()
                ok, frame_bgr = cap.read()
                if not ok or frame_bgr is None:
                    time.sleep(0.02)
                    continue

                frame_bgr = np.ascontiguousarray(frame_bgr)
                h, w = frame_bgr.shape[:2]
                if (w, h) != tuple(self.rear_res):
                    frame_bgr = cv2.resize(frame_bgr, self.rear_res, interpolation=cv2.INTER_AREA)

                frame_bgr = cv2.flip(frame_bgr, 0)

                ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    with self._rear_lock:
                        self._rear_last_jpeg = buf.tobytes()

                elapsed = time.time() - t0
                if elapsed < target_delay:
                    time.sleep(target_delay - elapsed)

        except Exception:
            logger.exception("Rear camera GStreamer loop crashed")
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            self._rear_cap = None
            self.rear_active = False

