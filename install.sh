#!/bin/bash

# install.sh - Crawler installation script for Raspberry Pi 4
# Fixed for Debian Trixie compatibility

set -e

echo "🚀 Installing Crawler Remote Control Vehicle for Pi 4..."

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

print_status() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check if running on Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
    print_warning "Not running on Raspberry Pi. Continuing anyway..."
fi

# Update system
print_status "Updating system packages..."
sudo apt update

# Install system dependencies - Fixed for Debian Trixie
print_status "Installing system dependencies..."
sudo apt install -y \
    python3 python3-pip python3-venv python3-dev \
    git cmake build-essential pkg-config \
    libhdf5-dev libjpeg-dev libopenjp2-7-dev \
    libssl-dev libffi-dev \
    libopenblas-dev || true  # Use openblas instead of atlas

# Install camera packages
print_status "Installing camera packages..."
sudo apt install -y \
    libcamera-apps libcamera-tools \
    v4l-utils ffmpeg \
    python3-opencv || true

# Try to install picamera2 if available
if apt-cache show python3-picamera2 &>/dev/null; then
    sudo apt install -y python3-picamera2
else
    print_warning "python3-picamera2 not found in repos, will install via pip"
fi

# Install I2C for motor controller
print_status "Installing I2C packages..."
sudo apt install -y i2c-tools python3-smbus

# Enable interfaces
print_status "Enabling hardware interfaces..."
if command -v raspi-config &>/dev/null; then
    sudo raspi-config nonint do_camera 0 2>/dev/null || true
    sudo raspi-config nonint do_i2c 0 2>/dev/null || true
else
    print_warning "raspi-config not found, please enable camera and I2C manually"
    # Alternative method for enabling I2C
    sudo modprobe i2c-dev
    sudo modprobe i2c-bcm2835
    echo "i2c-dev" | sudo tee -a /etc/modules > /dev/null
fi

# Create project structure
print_status "Creating project directories..."
mkdir -p backend web/static/css web/static/js web/templates config logs media

# Create Python virtual environment
print_status "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Upgrade pip first
pip install --upgrade pip setuptools wheel

# Create requirements.txt with compatible versions
cat > requirements.txt << 'EOF'
Flask>=2.3.0
Flask-SocketIO>=5.3.0
eventlet>=0.33.3
opencv-python>=4.8.0
Pillow>=9.0.0
numpy>=1.21.0
psutil>=5.9.0
smbus2
EOF

# Install Python packages
print_status "Installing Python dependencies..."
pip install -r requirements.txt

# Try to install picamera2 in venv if not system-installed
if ! python3 -c "import picamera2" 2>/dev/null; then
    print_status "Installing picamera2 in virtual environment..."
    pip install picamera2 2>/dev/null || print_warning "picamera2 installation failed - USB camera only mode"
fi

# Try to install RPi.GPIO or alternative
print_status "Installing GPIO library..."
pip install RPi.GPIO 2>/dev/null || {
    print_warning "RPi.GPIO failed, trying gpiozero..."
    pip install gpiozero
}

# Create a simple test script
cat > test_hardware.py << 'EOF'
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

print("\nHardware test complete!")
EOF

chmod +x test_hardware.py

# Create systemd service
print_status "Creating systemd service..."
sudo tee /etc/systemd/system/crawler.service > /dev/null << EOF
[Unit]
Description=Crawler Robot Control
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PWD
Environment="PATH=$PWD/venv/bin:/usr/bin:/bin"
ExecStart=$PWD/venv/bin/python $PWD/backend/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

# Create start script
cat > start.sh << 'EOF'
#!/bin/bash
source venv/bin/activate
python backend/app.py
EOF
chmod +x start.sh

print_status "Installation complete!"
echo ""
echo "Testing installation..."
source venv/bin/activate
python test_hardware.py
echo ""
echo "To start the crawler:"
echo "  ./start.sh"
echo "  or"
echo "  sudo systemctl start crawler"
echo ""
echo "To enable auto-start on boot:"
echo "  sudo systemctl enable crawler"
echo ""
echo "Access the interface at: http://[pi-ip]:5000"
