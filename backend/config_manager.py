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
                "battery_register": "0x40",
                "battery_scale": 0.0015384615384615385,
                "battery_counts_per_volt": 650.0,
                "battery_offset": 0.0,
                "battery_full_voltage": 12.6,
                "battery_empty_voltage": 9.0,
                "battery_refresh": 1.5,
                "battery_word_order": "auto",
                "battery_shift": 0,
                "battery_mask": None,
                "battery_bits": 16,
                "battery_signed": False,
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
