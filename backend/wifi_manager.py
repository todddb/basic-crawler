"""Wi-Fi manager built around nmcli interactions."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
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
        self._cert_dir = Path.home() / ".cache" / "crawler" / "wifi_certs"

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

    def _wrap_with_pkexec(self, command: List[str]) -> Optional[List[str]]:
        """Return a pkexec-wrapped command when available."""

        pkexec_path = shutil.which("pkexec")
        if not pkexec_path:
            return None

        # pkexec requires the command to be provided without shell quoting.
        return [pkexec_path, *command]

    def _run_nmcli(self, args: List[str], *, timeout: int = 20) -> str:
        base_command = ["nmcli", "--terse", "--colors", "no", *args]
        command = list(base_command)
        attempted_priv_escalation = False

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

        while result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            message = stderr or stdout or "nmcli command failed"

            if "textual authentication agent" in message.lower() or "/dev/tty" in message:
                message = (
                    "Authentication prompt could not be shown. Run the control server as root "
                    "or configure pkexec/polkit so nmcli can run without a tty."
                )

            if "insufficient" in message.lower() and "privilege" in message.lower():
                if not attempted_priv_escalation:
                    wrapped_command = self._wrap_with_pkexec(base_command)
                    if wrapped_command:
                        attempted_priv_escalation = True
                        try:
                            result = subprocess.run(
                                wrapped_command,
                                capture_output=True,
                                check=False,
                                text=True,
                                timeout=timeout,
                            )
                            command = wrapped_command
                            continue
                        except subprocess.TimeoutExpired as exc:
                            raise WifiError(
                                "Timed out while attempting to elevate nmcli permissions with pkexec"
                            ) from exc

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

    def _store_ca_certificate(self, ca_cert_pem: str) -> str:
        """Persist a CA certificate and return the absolute filesystem path."""

        normalized = (ca_cert_pem or "").strip()
        if not normalized:
            raise WifiError("CA certificate content is empty")

        if "BEGIN CERTIFICATE" not in normalized:
            raise WifiError("CA certificate must be in PEM format")

        if not normalized.endswith("\n"):
            normalized = f"{normalized}\n"

        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        try:
            self._cert_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WifiError("Unable to prepare certificate storage directory") from exc

        cert_path = self._cert_dir / f"{digest}.pem"

        try:
            if not cert_path.exists() or cert_path.read_text(encoding="utf-8") != normalized:
                cert_path.write_text(normalized, encoding="utf-8")
        except OSError as exc:
            raise WifiError("Failed to store CA certificate") from exc

        return str(cert_path)

    def _connection_exists(self, name: str) -> bool:
        output = self._run_nmcli(["--fields", "NAME", "connection", "show"])
        for line in output.strip().splitlines():
            if self._unescape(line).strip() == name:
                return True
        return False

    def _is_connection_active(self, name: str) -> bool:
        output = self._run_nmcli(["--fields", "NAME", "connection", "show", "--active"])
        for line in output.strip().splitlines():
            if self._unescape(line).strip() == name:
                return True
        return False

    def _get_connection_value(self, name: str, field: str) -> Optional[str]:
        try:
            output = self._run_nmcli(["--get-values", field, "connection", "show", name])
        except WifiError:
            return None

        value = (output or "").strip()
        return self._unescape(value) if value else None

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
        eap_method: Optional[str] = None,
        phase2_auth: Optional[str] = None,
        anonymous_identity: Optional[str] = None,
        domain_suffix_match: Optional[str] = None,
        system_ca_certs: Optional[bool] = True,
        ca_cert_pem: Optional[str] = None,
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

            eap = (eap_method or "peap").strip().lower()
            if eap not in {"peap", "ttls"}:
                raise WifiError("Unsupported enterprise EAP method")

            if phase2_auth:
                phase2 = phase2_auth.strip().lower()
            else:
                phase2 = "mschapv2" if eap == "peap" else "pap"

            if phase2 not in {"mschapv2", "pap", "gtc"}:
                raise WifiError("Unsupported enterprise inner authentication method")

            cert_path: Optional[str] = None
            if ca_cert_pem:
                cert_path = self._store_ca_certificate(ca_cert_pem)

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
                eap,
                "802-1x.phase2-auth",
                phase2,
                "802-1x.identity",
                username,
                "802-1x.password",
                password,
            ]

            if anonymous_identity:
                add_args.extend([
                    "802-1x.anonymous-identity",
                    anonymous_identity.strip(),
                ])

            if domain_suffix_match:
                add_args.extend([
                    "802-1x.domain-suffix-match",
                    domain_suffix_match.strip(),
                ])

            if system_ca_certs is not None:
                add_args.extend([
                    "802-1x.system-ca-certs",
                    "yes" if system_ca_certs else "no",
                ])

            if cert_path:
                add_args.extend(["802-1x.ca-cert", cert_path])

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

    def start_hotspot(
        self,
        *,
        ssid: str = "crawler",
        password: str = "crawler1234",
        band: str = "bg",
        channel: Optional[int] = None,
        connection_name: str = "crawler-hotspot",
    ) -> Dict[str, object]:
        device = self._get_wifi_device()
        if not device:
            raise WifiError("No Wi-Fi device available for hotspot")

        ssid = (ssid or "crawler").strip()
        if not ssid:
            raise WifiError("Hotspot SSID cannot be empty")

        if not password or len(password) < 8:
            raise WifiError("Hotspot password must be at least 8 characters")

        if band not in {"a", "bg"}:
            raise WifiError("Band must be 'a' (5GHz) or 'bg' (2.4GHz)")

        if not self._connection_exists(connection_name):
            self._run_nmcli(
                [
                    "connection",
                    "add",
                    "type",
                    "wifi",
                    "ifname",
                    device,
                    "con-name",
                    connection_name,
                    "autoconnect",
                    "no",
                    "ssid",
                    ssid,
                ]
            )

        modify_args = [
            "connection",
            "modify",
            connection_name,
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.band",
            band,
            "ipv4.method",
            "shared",
            "ipv6.method",
            "shared",
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            password,
            "connection.autoconnect",
            "no",
        ]

        if channel:
            modify_args.extend(["802-11-wireless.channel", str(int(channel))])
        else:
            modify_args.extend(["802-11-wireless.channel", ""])

        self._run_nmcli(modify_args)

        if ssid:
            self._run_nmcli([
                "connection",
                "modify",
                connection_name,
                "802-11-wireless.ssid",
                ssid,
            ])

        self._run_nmcli(["connection", "up", connection_name, "ifname", device])

        return {
            "message": f"Hotspot '{ssid}' enabled",
            "ssid": ssid,
            "password": password,
            "connection_name": connection_name,
            "active": True,
        }

    def stop_hotspot(self, connection_name: str = "crawler-hotspot") -> Dict[str, object]:
        self._run_nmcli_allow_fail(["connection", "down", connection_name])

        return {
            "message": "Hotspot stopped",
            "connection_name": connection_name,
            "active": False,
        }

    def get_hotspot_status(self, connection_name: str = "crawler-hotspot") -> Dict[str, object]:
        exists = self._connection_exists(connection_name)
        active = self._is_connection_active(connection_name) if exists else False
        ssid = self._get_connection_value(connection_name, "802-11-wireless.ssid") if exists else None

        return {
            "connection_name": connection_name,
            "exists": exists,
            "active": active,
            "ssid": ssid,
        }
