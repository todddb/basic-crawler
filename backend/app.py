#!/usr/bin/env python3
# app.py - Simplified Flask application for crawler robot

import os
import sys
import logging
import signal
import subprocess
import time
from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit

from camera_manager import CameraManager
from config_manager import ConfigManager
from hardware_manager import HardwareManager
from utils import setup_logging
from wifi_manager import WifiManager, WifiError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "web", "templates"),
    static_folder=os.path.join(BASE_DIR, "web", "static"),
)
app.config["SECRET_KEY"] = "crawler_secret_key"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Managers
config_manager = None
camera_manager = None
hardware_manager = None
wifi_manager = None
logger = None
telemetry_thread_started = False

def initialize_systems():
    """Initialize all subsystems"""
    global config_manager, camera_manager, hardware_manager, wifi_manager, logger

    config_manager = ConfigManager()
    config = config_manager.get_config()

    logger = setup_logging(config.get("system", {}).get("log_level", "INFO"))
    logger.info("Starting Crawler Robot Control System...")
    
    hardware_manager = HardwareManager(config)
    camera_manager = CameraManager(config)
    wifi_manager = WifiManager(logger)

    logger.info("All systems initialized")
    start_background_tasks()


def start_background_tasks():
    global telemetry_thread_started

    if telemetry_thread_started:
        return

    telemetry_thread_started = True

    def telemetry_loop():
        socketio.sleep(1)
        while True:
            try:
                if hardware_manager:
                    telemetry = hardware_manager.get_telemetry()
                    if telemetry:
                        socketio.emit("telemetry", telemetry)
            except Exception:
                if logger:
                    logger.exception("Telemetry loop failed")
            socketio.sleep(0.5)

    socketio.start_background_task(telemetry_loop)


def _return_to_start_complete(result):
    payload = {
        "success": bool(result.get("success")) if isinstance(result, dict) else False,
        "reason": result.get("reason") if isinstance(result, dict) else None,
    }
    socketio.emit("return_to_start_complete", payload)
    if hardware_manager:
        socketio.emit("telemetry", hardware_manager.get_telemetry())

# Routes
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "success": True,
        "hardware": hardware_manager.get_status() if hardware_manager else {},
        "cameras": camera_manager.get_status() if camera_manager else {},
    })


@app.route("/api/camera/quality", methods=["POST"])
def api_camera_quality():
    if not camera_manager:
        return jsonify({"success": False, "error": "Camera manager unavailable"}), 503

    data = request.get_json(force=True, silent=True) or {}
    profile = data.get("profile", "balanced")
    applied = camera_manager.apply_quality_profile(profile)
    status_code = 200 if applied else 400
    return jsonify({"success": applied, "profile": profile}), status_code


@app.route("/api/wifi/networks", methods=["GET"])
def api_wifi_networks():
    if not wifi_manager:
        return jsonify({"success": False, "error": "Wi-Fi manager unavailable"}), 503

    try:
        result = wifi_manager.scan_networks()
    except WifiError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({"success": True, **result})


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    if not wifi_manager:
        return jsonify({"success": False, "error": "Wi-Fi manager unavailable"}), 503

    data = request.get_json(force=True, silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"success": False, "error": "SSID is required"}), 400

    psk = data.get("psk")
    username = data.get("username")
    password = data.get("password")
    bssid = data.get("bssid")
    eap_method = data.get("eap_method")
    phase2_auth = data.get("phase2_auth")
    anonymous_identity = data.get("anonymous_identity")
    domain_suffix_match = data.get("domain_suffix_match")
    system_ca_certs = data.get("system_ca_certs")
    ca_cert_pem = data.get("ca_cert_pem")

    if isinstance(system_ca_certs, str):
        system_ca_certs = system_ca_certs.lower() not in {"false", "0", "no", "off"}

    if isinstance(system_ca_certs, (int, float)):
        system_ca_certs = bool(system_ca_certs)

    if isinstance(system_ca_certs, bool) is False and system_ca_certs is not None:
        system_ca_certs = None

    if isinstance(eap_method, str):
        eap_method = eap_method.strip().lower() or None

    if isinstance(phase2_auth, str):
        phase2_auth = phase2_auth.strip().lower() or None

    if isinstance(anonymous_identity, str):
        anonymous_identity = anonymous_identity.strip() or None

    if isinstance(domain_suffix_match, str):
        domain_suffix_match = domain_suffix_match.strip() or None

    if isinstance(ca_cert_pem, str):
        ca_cert_pem = ca_cert_pem.strip() or None

    connect_kwargs = {
        "psk": psk,
        "username": username,
        "password": password,
        "bssid": bssid,
        "eap_method": eap_method,
        "phase2_auth": phase2_auth,
        "anonymous_identity": anonymous_identity,
        "domain_suffix_match": domain_suffix_match,
        "ca_cert_pem": ca_cert_pem,
    }

    if system_ca_certs is not None:
        connect_kwargs["system_ca_certs"] = system_ca_certs

    try:
        result = wifi_manager.connect(ssid, **connect_kwargs)
    except WifiError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    return jsonify({"success": True, **result})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    if not hardware_manager:
        return jsonify({"success": False, "error": "Hardware manager unavailable"}), 503

    logger.warning("Shutdown requested via API")
    try:
        subprocess.Popen(["sudo", "shutdown", "-h", "now"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.exception("Failed to invoke shutdown")
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({"success": True})


@app.route("/video_feed/<camera>")
def video_feed(camera):
    """Stream video from specified camera (front or rear). Auto-start threads on demand."""
    if camera == "front":
        camera_manager.start_cameras(want_front=True, want_rear=False)
    elif camera == "rear":
        camera_manager.start_cameras(want_front=False, want_rear=True)

    def generate():
        while True:
            frame = None
            if camera == "front":
                frame = camera_manager.get_latest_frame("front")
            elif camera == "rear":
                frame = camera_manager.get_latest_frame("rear")

            if frame:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            else:
                time.sleep(0.05)

    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

# WebSocket handlers
@socketio.on("connect")
def ws_connect():
    logger.info(f"Client connected: {request.sid}")
    emit("connected", {"message": "Connected to crawler"})
    if hardware_manager:
        emit("telemetry", hardware_manager.get_telemetry())

@socketio.on("disconnect")
def ws_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on("motor_control")
def ws_motor_control(data):
    left = int(data.get("left", 0))
    right = int(data.get("right", 0))
    if hardware_manager:
        hardware_manager.set_motor_speed(left, right)
    emit("motor_ack", {"left": left, "right": right})

@socketio.on("emergency_stop")
def ws_emergency_stop():
    if hardware_manager:
        hardware_manager.emergency_stop()
    emit("stopped", {"message": "Emergency stop activated"})


@socketio.on("reset_odometry")
def ws_reset_odometry():
    if not hardware_manager:
        emit("odometry_reset", {"success": False, "error": "Hardware manager unavailable"})
        return

    hardware_manager.reset_odometry()
    emit(
        "odometry_reset",
        {"success": True, "sequence": hardware_manager.odometry_sequence},
    )
    socketio.emit("telemetry", hardware_manager.get_telemetry())


@socketio.on("return_to_start")
def ws_return_to_start():
    if not hardware_manager:
        emit(
            "return_to_start_status",
            {"success": False, "message": "Hardware manager unavailable"},
        )
        return

    success, message = hardware_manager.begin_return_to_start(
        on_complete=_return_to_start_complete
    )

    emit(
        "return_to_start_status",
        {"success": success, "message": message, "in_progress": success},
    )

    socketio.emit("telemetry", hardware_manager.get_telemetry())

def cleanup():
    """Cleanup on shutdown"""
    logger.info("Shutting down...")
    if camera_manager:
        camera_manager.cleanup()
    if hardware_manager:
        hardware_manager.cleanup()

def signal_handler(sig, frame):
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    initialize_systems()
    host = "0.0.0.0"
    port = 5000
    logger.info(f"Starting web server on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False)
