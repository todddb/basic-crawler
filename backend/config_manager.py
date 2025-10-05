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
                    "resolution": [1280, 720],
                    "fps": 30
                },
                "rear": {
                    "type": "usb",
                    "resolution": [640, 480],
                    "fps": 15
                }
            },
            "motors": {
                "i2c_address": "0x34",
                "max_speed": 100
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
