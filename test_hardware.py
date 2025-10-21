#!/usr/bin/env python3
import sys

print("Testing hardware libraries...")

# Test I2C
try:
    import smbus2
    print("✓ I2C library (smbus2) available")
except ImportError:
    print("✗ I2C library missing")

# Test GPIO
try:
    import RPi.GPIO
    print("✓ GPIO library (RPi.GPIO) available")
except ImportError:
    try:
        import gpiozero
        print("✓ GPIO library (gpiozero) available")
    except ImportError:
        print("✗ GPIO library missing")

# Test camera
try:
    import cv2
    print("✓ OpenCV available")
except ImportError:
    print("✗ OpenCV missing")

try:
    from picamera2 import Picamera2
    print("✓ Picamera2 available")
except ImportError:
    print("✗ Picamera2 missing (USB camera only)")
    print("  Install with: sudo apt install python3-picamera2")

print("\nHardware test complete!")
