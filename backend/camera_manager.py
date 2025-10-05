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
    Front: Picamera2 (if available)
    Rear : USB camera via OpenCV (auto-detected /dev/videoN)
    """

    def __init__(self, config: dict):
        self.config = config or {}

        # --- Configuration ---------------------------------------------------
        cam_cfg = (self.config.get("cameras") or {})
        f_cfg = cam_cfg.get("front", {}) or {}
        r_cfg = cam_cfg.get("rear", {}) or {}

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
        # Front (Picamera2)
        self.front_supported = False
        self.front_active = False
        self._front_thread: Optional[threading.Thread] = None
        self._front_stop = threading.Event()
        self._front_lock = threading.Lock()
        self._front_last_jpeg: Optional[bytes] = None
        self._picam2 = None

        # Rear (USB)
        self.rear_supported = False
        self.rear_active = False
        self._rear_thread: Optional[threading.Thread] = None
        self._rear_stop = threading.Event()
        self._rear_lock = threading.Lock()
        self._rear_last_jpeg: Optional[bytes] = None
        self._rear_cap: Optional[cv2.VideoCapture] = None
        self._rear_index: Optional[int] = None

        # Probes
        self.front_supported = self._probe_front_picamera2()
        self.rear_supported = self._probe_rear_usb()

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
                "resolution": list(self.front_res),
                "fps": self.front_fps,
                "quality": self.front_quality,
            },
            "rear": {
                "active": self.rear_active,
                "supported": self.rear_supported,
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

    def _probe_front_picamera2(self) -> bool:
        """
        Try to import Picamera2 and create a minimal instance to confirm support.
        """
        try:
            from picamera2 import Picamera2  # noqa: F401
        except Exception:
            logger.info("Picamera2 not available; front camera disabled.")
            return False

        # Defer full initialization until thread start; import presence is enough
        logger.info("Picamera2 detected; front camera supported.")
        return True

    def _probe_rear_usb(self) -> bool:
        """
        Try to open /dev/videoN devices to find a working USB camera.
        """
        for idx in (0, 1, 2, 3):
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                continue
            ok, _ = cap.read()
            cap.release()
            if ok:
                self._rear_index = idx
                logger.info(f"USB camera will use /dev/video{idx}")
                return True

        logger.info("No USB camera detected on /dev/video[0-3].")
        return False

    # ---------------------- Front (Picamera2) thread -------------------------

    def _start_front_thread(self):
        if self._front_thread and self._front_thread.is_alive():
            return
        logger.info("Initializing Picamera2 (front)...")
        self._front_stop.clear()
        self._front_thread = threading.Thread(target=self._front_loop, name="front_cam", daemon=True)
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
        if self._picam2 is not None:
            try:
                self._picam2.stop()
            except Exception:
                pass
            try:
                self._picam2.close()
            except Exception:
                pass
            self._picam2 = None
        self.front_active = False
        logger.info("Front camera stopped.")

    def _front_loop(self):
        try:
            from picamera2 import Picamera2
            self._picam2 = Picamera2()

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

    # ---------------------- Rear (USB OpenCV) thread -------------------------

    def _start_rear_thread(self):
        if self._rear_thread and self._rear_thread.is_alive():
            return
        if self._rear_index is None:
            raise RuntimeError("No USB camera index available.")
        logger.info(f"Initializing USB camera (rear) on /dev/video{self._rear_index}...")
        self._rear_stop.clear()
        self._rear_thread = threading.Thread(target=self._rear_loop, name="rear_cam", daemon=True)
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
        # Release capture
        if self._rear_cap is not None:
            try:
                self._rear_cap.release()
            except Exception:
                pass
            self._rear_cap = None
        self.rear_active = False
        logger.info("Rear camera stopped.")

    def _rear_loop(self):
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

                ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    with self._rear_lock:
                        self._rear_last_jpeg = buf.tobytes()

                # Pace
                elapsed = time.time() - t0
                if elapsed < target_delay:
                    time.sleep(target_delay - elapsed)

        except Exception:
            logger.exception("Rear camera loop crashed")
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            self._rear_cap = None
            self.rear_active = False

