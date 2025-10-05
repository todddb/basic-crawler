#!/usr/bin/env python3
# app.py - Simplified Flask application for crawler robot

import os
import sys
import logging
import signal
import time
from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit

from camera_manager import CameraManager
from config_manager import ConfigManager
from hardware_manager import HardwareManager
from utils import setup_logging

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
logger = None

def initialize_systems():
    """Initialize all subsystems"""
    global config_manager, camera_manager, hardware_manager, logger
    
    config_manager = ConfigManager()
    config = config_manager.get_config()
    
    logger = setup_logging(config.get("system", {}).get("log_level", "INFO"))
    logger.info("Starting Crawler Robot Control System...")
    
    hardware_manager = HardwareManager(config)
    camera_manager = CameraManager(config)
    
    logger.info("All systems initialized")

# Routes
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "success": True,
        "hardware": hardware_manager.get_status() if hardware_manager else {},
        "cameras": camera_manager.get_status() if camera_manager else {}
    })


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
