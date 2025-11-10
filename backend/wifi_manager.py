"""Wi-Fi manager built around nmcli interactions."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional


class WifiError(RuntimeError):
    """Custom exception raised for Wi-Fi related failures."""


@dataclass
class WifiNetwork:
    ssid: Optional[str]
    security: str
    signal: Optional[int]
    active: bool
    bssid: Optional[str]
    requires_passphrase: bool
    supports_enterprise: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "ssid": self.ssid,
            "security": self.security,
            "signal": self.signal,
            "active": self.active,
            "bssid": self.bssid,
            "requires_passphrase": self.requires_passphrase,
            "supports_enterprise": self.supports_enterprise,
        }


class WifiManager:
    """Wrapper that uses nmcli to inspect and manage Wi-Fi connections."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _split_fields(line: str) -> List[str]:
        """Split nmcli's escaped colon-separated output."""

        fields: List[str] = []
        buffer: List[str] = []
        escape = False

        for char in line:
            if escape:
                buffer.append(char)
                escape = False
                continue

            if char == "\\":
                escape = True
                continue

            if char == ":":
                fields.append("".join(buffer))
                buffer.clear()
                continue

            buffer.append(char)

        fields.append("".join(buffer))
        return fields

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace("\\:", ":").replace("\\\\", "\\")

    def _run_nmcli(self, args: List[str], *, timeout: int = 20) -> str:
        command = ["nmcli", "--terse", "--colors", "no", *args]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise WifiError("nmcli command not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise WifiError("Timed out while communicating with nmcli") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            message = stderr or stdout or "nmcli command failed"

            if "insufficient" in message.lower() and "privilege" in message.lower():
                if hasattr(os, "geteuid"):
                    try:
                        if os.geteuid() != 0:
                            message = (
                                "Insufficient privileges to manage Wi-Fi. "
                                "Run the control server as root or grant nmcli permissions via polkit."
                            )
                        else:
                            message = (
                                "NetworkManager denied the request due to insufficient privileges. "
                                "Ensure the nmcli polkit rules allow this action."
                            )
                    except OSError:
                        message = (
                            "Insufficient privileges to manage Wi-Fi. "
                            "Run the control server as root or grant nmcli permissions via polkit."
                        )
                else:
                    message = (
                        "Insufficient privileges to manage Wi-Fi. "
                        "Run the control server as root or grant nmcli permissions via polkit."
                    )

            raise WifiError(message)

        return result.stdout

    def _run_nmcli_allow_fail(self, args: List[str], *, timeout: int = 20) -> None:
        try:
            self._run_nmcli(args, timeout=timeout)
        except WifiError as exc:
            if self._logger:
                self._logger.debug("nmcli command failed but ignored: %s (%s)", args, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scan_networks(self) -> Dict[str, object]:
        """Return information about nearby Wi-Fi networks."""

        # nmcli needs a rescan occasionally. Errors are ignored.
        self._run_nmcli_allow_fail(["device", "wifi", "rescan"])

        raw_output = self._run_nmcli([
            "--fields",
            "ACTIVE,SSID,SECURITY,SIGNAL,BSSID",
            "device",
            "wifi",
            "list",
        ])

        networks: List[WifiNetwork] = []
        for line in raw_output.strip().splitlines():
            if not line:
                continue

            parts = self._split_fields(line)
            # ACTIVE, SSID, SECURITY, SIGNAL, BSSID
            while len(parts) < 5:
                parts.append("")

            active = parts[0].strip().lower() in {"yes", "y", "1", "true", "*"}
            ssid = self._unescape(parts[1]).strip() or None
            security_raw = self._unescape(parts[2]).strip()
            security = security_raw or "OPEN"
            signal_str = self._unescape(parts[3]).strip()
            bssid = self._unescape(parts[4]).strip() or None

            try:
                signal = int(signal_str)
            except ValueError:
                signal = None

            security_upper = security.upper()
            simplified_security = security_upper.replace(" ", "")
            if simplified_security in {"", "NONE", "OPEN", "--"}:
                requires_passphrase = False
            else:
                requires_passphrase = True

            supports_enterprise = "EAP" in security_upper or "802.1X" in security_upper

            networks.append(
                WifiNetwork(
                    ssid=ssid,
                    security=security,
                    signal=signal,
                    active=active,
                    bssid=bssid,
                    requires_passphrase=requires_passphrase,
                    supports_enterprise=supports_enterprise,
                )
            )

        networks.sort(key=lambda n: (n.signal or 0), reverse=True)

        active_network = next((n for n in networks if n.active), None)

        return {
            "networks": [network.to_dict() for network in networks],
            "active": active_network.to_dict() if active_network else None,
        }

    def _get_wifi_device(self) -> Optional[str]:
        output = self._run_nmcli(["--fields", "DEVICE,TYPE,STATE", "device", "status"])
        for line in output.strip().splitlines():
            parts = self._split_fields(line)
            if len(parts) < 3:
                continue
            device = self._unescape(parts[0]).strip()
            dev_type = self._unescape(parts[1]).strip().lower()
            state = self._unescape(parts[2]).strip().lower()
            if dev_type == "wifi" and state not in {"unavailable", "unmanaged"}:
                return device
        return None

    def connect(
        self,
        ssid: str,
        *,
        psk: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        bssid: Optional[str] = None,
    ) -> Dict[str, object]:
        ssid = (ssid or "").strip()
        if not ssid:
            raise WifiError("SSID is required")

        device = self._get_wifi_device()
        if not device:
            raise WifiError("No Wi-Fi device available")

        connection_name = ssid

        if username:
            if not password:
                raise WifiError("Password is required for enterprise Wi-Fi")

            # Remove any existing connection with the same name to avoid conflicts.
            self._run_nmcli_allow_fail(["connection", "delete", "id", connection_name])

            add_args = [
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                device,
                "con-name",
                connection_name,
                "ssid",
                ssid,
                "wifi-sec.key-mgmt",
                "wpa-eap",
                "802-1x.eap",
                "peap",
                "802-1x.phase2-auth",
                "mschapv2",
                "802-1x.identity",
                username,
                "802-1x.password",
                password,
            ]

            if bssid:
                add_args.extend(["wifi.bssid", bssid])

            self._run_nmcli(add_args)
            self._run_nmcli(["connection", "modify", connection_name, "connection.autoconnect", "yes"])
            self._run_nmcli(["connection", "up", connection_name])
            message = f"Connected to {ssid}"
        else:
            connect_args = [
                "device",
                "wifi",
                "connect",
                ssid,
                "ifname",
                device,
            ]

            if bssid:
                connect_args.extend(["bssid", bssid])

            if psk:
                connect_args.extend(["password", psk])

            self._run_nmcli(connect_args)
            message = f"Connected to {ssid}"

        scan_result = self.scan_networks()
        return {
            "message": message,
            "active": scan_result.get("active"),
            "networks": scan_result.get("networks"),
        }
