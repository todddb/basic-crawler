# utils.py - Utility functions

import logging
import os
import sys
from datetime import datetime

def setup_logging(log_level='INFO'):
    """Setup logging configuration"""
    os.makedirs('logs', exist_ok=True)
    
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Configure logging
    logging.basicConfig(
        level=numeric_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('logs/crawler.log')
        ]
    )
    
    return logging.getLogger(__name__)

def get_timestamp():
    """Get current timestamp string"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def clamp(value, min_val, max_val):
    """Clamp value between min and max"""
    return max(min_val, min(max_val, value))
