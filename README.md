PLEASE BE CAREFUL IF YOU TRY TO BUILD THIS CRAWLER!
I provide this as a free resource, but I am not responsible for inuury to damage to your equipment for any reason if you try to build this.

# Crawler Remote Control Vehicle

This repository contains the software for a Raspberry Pi 5-based crawler robot with dual cameras, I²C motor control, and a web-based driver interface. The project targets a clean Raspberry Pi OS installation that boots from the **SanDisk 128 GB Ultra USB drive** listed in the [materials list](doc/parts.md) and uses the wiring shown in [doc/wiring.png](doc/wiring.png).

## Hardware and Materials

* Review the complete bill of materials in [doc/parts.md](doc/parts.md) before you begin ordering parts.
* Choose cameras from the list: either the Arducam wide modules in the bill of materials **or** Raspberry Pi Camera Module 3 units. The official Pi cameras cost a little more but remain fully compatible; just be sure to use the correct flat-flex cables for the Pi 5 connectors.
* Wire the motors, cameras, power, and Raspberry Pi 5 following the reference wiring diagram in [doc/wiring.png](doc/wiring.png). The diagram aligns with the default configuration files in this repository.

## 1. Prepare the Raspberry Pi OS USB Drive

1. **Download Raspberry Pi Imager** for your workstation from <https://www.raspberrypi.com/software/>.
2. **Insert the SanDisk 128 GB Ultra USB drive** (the one listed in the materials) into your workstation.
3. Launch Raspberry Pi Imager and choose:
   * **Device**: Raspberry Pi 5.
   * **Operating System**: Raspberry Pi OS (64-bit) – Bookworm or later.
   * **Storage**: The SanDisk 128 GB USB drive.
4. Click the settings icon (⚙) and configure:
   * Hostname, username, and password to match your preferred credentials.
   * Enable SSH (use password or key-based authentication).
   * Optional Wi-Fi credentials if you will not use Ethernet on first boot.
5. Start the imaging process and wait for it to finish. When prompted, remove the USB drive safely.

## 2. First Boot and System Updates

1. Insert the prepared USB drive into your Raspberry Pi 5. Disconnect any SD cards so the Pi boots from USB.
2. Connect the Raspberry Pi 5 to power, a monitor, and input devices (or rely on SSH if configured).
3. Complete any first-boot prompts, then open a terminal and run the following commands to update the operating system:

   ```bash
   sudo apt update
   sudo apt full-upgrade -y
   sudo reboot
   ```

   The reboot ensures the kernel and firmware updates are loaded.
4. **If you power the Pi 5 from a battery or non-PD USB-C supply:** the firmware performs a power-supply negotiation check on every boot and may pause with an on-screen prompt. To bypass the check once you have verified your supply is stable enough for the robot, edit the bootloader configuration and disable the check:

   ```bash
   sudo -E rpi-eeprom-config --edit
   ```

   Add (or update) the following line in the editor that opens, then save and exit:

   ```
   POWER_SUPPLY_CHECK=0
   ```

   Reboot to apply the change. The Pi will now boot without waiting for power confirmation. You can re-enable the safeguard later by setting the value back to `1`.

## 3. Install Project Dependencies

1. After the reboot, log back in (SSH or local terminal) and install Git if it is not already present:

   ```bash
   sudo apt install -y git
   ```

2. Clone this repository and move into the project directory:

   ```bash
   git clone https://github.com/todddb/basic-crawler.git
   cd basic-crawler
   ```

3. Run the installation script. It creates a Python virtual environment that **shares system packages**, ensuring the PiCamera libraries provided by Raspberry Pi OS remain available:

   ```bash
   chmod +x install.sh
   ./install.sh
   ```

   The script enables the camera and I²C interfaces, installs required system packages, and installs Python dependencies from `requirements.txt` inside the virtual environment.

## 4. Verify Hardware Access

1. Activate the virtual environment and run the hardware smoke test:

   ```bash
   source venv/bin/activate
   python test_hardware.py
   ```

   Confirm that I²C, OpenCV, and (if connected) Picamera2 succeed.

2. Start the web control interface:

   ```bash
   ./start.sh
   ```

   By default the Flask application listens on port 5000. Visit `http://<pi-address>:5000/` in your browser to view the live streams and controls.

## 5. Next Steps

* Customize `config/default_config.json` (or your chosen config file) to match any hardware changes.
* Revisit [doc/parts.md](doc/parts.md) and [doc/wiring.png](doc/wiring.png) whenever you adjust hardware; the software assumes those defaults.
* Enable the optional `crawler.service` systemd unit created by the installer to auto-start on boot:

  ```bash
  sudo systemctl enable crawler
  sudo systemctl start crawler
  ```

## Wi-Fi notes

The Flask API already exposes endpoints for network scanning (`/api/wifi/networks`) and connecting (`/api/wifi/connect`). If WPA2-Enterprise prompts fail with an error such as `Error creating textual authentication agent (/dev/tty)`, run the control server as `root` or add a `pkexec`/polkit rule that lets `nmcli` execute without opening an interactive terminal.

To offer a fallback access method, the backend now includes a small hotspot helper that can run alongside normal infrastructure Wi-Fi (hardware permitting). Use the following calls:

* `POST /api/wifi/hotspot/start` with optional JSON body `{ "ssid": "crawler", "password": "crawler1234", "band": "bg", "channel": 6 }` to broadcast a WPA2-PSK network that shares the Pi's upstream connection.
* `POST /api/wifi/hotspot/stop` to turn the hotspot off.
* `GET /api/wifi/hotspot/status` to see whether the hotspot profile exists and whether it is active.

Most Wi-Fi chipsets can handle station mode plus an AP on 2.4 GHz, but if the interface cannot multitask it may need to drop the infrastructure connection while the hotspot is active. In that case, start the hotspot only when you intend to manage the crawler locally, then stop it before reconnecting to an existing network.


## Support

If you encounter issues with cameras or I²C devices, double-check that the Raspberry Pi OS image is fully updated and that the interfaces are enabled. File an issue in this repository with logs from `install.sh` and `test_hardware.py` to help diagnose problems.
