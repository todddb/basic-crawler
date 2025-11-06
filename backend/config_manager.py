# config_manager.py - Configuration management

import json
import os
import logging

class ConfigManager:
    def __init__(self, config_file="config/crawler_config.json"):
        self.logger = logging.getLogger(__name__)
        self.config_file = config_file
        self.config = self._load_config()
    
    def _load_config(self):
        """Load configuration or use defaults"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load config: {e}")
        
        # Return default configuration
        return {
            "system": {
                "log_level": "INFO"
            },
            "cameras": {
                "front": {
                    "type": "picamera2",
                    "camera_id": 0,
                    "resolution": [768, 432],
                    "fps": 24,
                    "quality": 78
                },
                "rear": {
                    "type": "usb",
                    "camera_id": 1,
                    "resolution": [320, 240],
                    "fps": 20,
                    "quality": 70
                }
            },
            "motors": {
                "i2c_address": "0x34",
                "max_speed": 100,
                "left_channel": 2,
                "right_channel": 3,
            },
            "battery": {
                "voltage_register": "0x2A",
                "voltage_scale": 0.01,
                "divider_ratio": 2.0,
                "cells": 3,
                "warn_cell_voltage": 3.5,
                "critical_cell_voltage": 3.3,
                "full_voltage": 12.6,
                "empty_voltage": 9.9,
                "ema_alpha": 0.3
            },
            "encoders": {
                "left_register": "0x40",
                "right_register": "0x44",
                "reset_register": "0x50",
                "reset_value": 1,
                "counts_per_revolution": 1024,
                "wheel_diameter_in": 2.64,
                "track_width_in": 7.5,
                "gear_ratio": 1.0,
                "max_path_points": 600
            },
            "lighting": {
                "led_bar": {
                    "pin": 12,
                    "default_state": False
                }
            }
        }
    
    def get_config(self):
        return self.config
    
    def save_config(self):
        """Save current configuration to file"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
            self.logger.info("Configuration saved")
        except Exception as e:
            self.logger.error(f"Failed to save config: {e}")
