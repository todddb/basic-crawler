"""Tests for WifiManager.connect() nmcli command construction."""

import os
import sys
import unittest
from typing import List
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from wifi_manager import WifiError, WifiManager  # noqa: E402


class ConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = WifiManager()
        self.calls: List[List[str]] = []

        def fake_run_nmcli(args, *, timeout=20):
            self.calls.append(list(args))
            if args[:1] == ["--fields"] and "device" in args and "status" in args:
                return "wlan0:wifi:disconnected\neth0:ethernet:connected\n"
            if args[:3] == ["connection", "delete", "id"]:
                raise WifiError("no such connection")
            return ""

        patcher = mock.patch.object(self.manager, "_run_nmcli", side_effect=fake_run_nmcli)
        patcher.start()
        self.addCleanup(patcher.stop)

        scan = mock.patch.object(
            self.manager, "scan_networks", return_value={"active": None, "networks": []}
        )
        scan.start()
        self.addCleanup(scan.stop)

    def _connection_calls(self) -> List[List[str]]:
        return [c for c in self.calls if c[:1] == ["connection"]]

    def _assert_add_sequence(self, ssid: str, expected_add: List[str]) -> None:
        self.assertEqual(
            self._connection_calls(),
            [
                ["connection", "delete", "id", ssid],
                expected_add,
                ["connection", "modify", ssid, "connection.autoconnect", "yes"],
                ["connection", "up", ssid],
            ],
        )
        self.assertFalse(
            any(c[:3] == ["device", "wifi", "connect"] for c in self.calls),
            "should not use 'nmcli device wifi connect'",
        )

    def test_psk_network_uses_connection_add_with_wpa_psk(self) -> None:
        result = self.manager.connect("HomeNet", psk="s3cret-pass")

        self._assert_add_sequence(
            "HomeNet",
            [
                "connection", "add", "type", "wifi", "ifname", "wlan0",
                "con-name", "HomeNet", "ssid", "HomeNet",
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "s3cret-pass",
            ],
        )
        self.assertEqual(result["message"], "Connected to HomeNet")

    def test_psk_network_with_bssid(self) -> None:
        self.manager.connect("HomeNet", psk="s3cret-pass", bssid="AA:BB:CC:DD:EE:FF")

        self._assert_add_sequence(
            "HomeNet",
            [
                "connection", "add", "type", "wifi", "ifname", "wlan0",
                "con-name", "HomeNet", "ssid", "HomeNet",
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "s3cret-pass",
                "wifi.bssid", "AA:BB:CC:DD:EE:FF",
            ],
        )

    def test_open_network_omits_security_settings(self) -> None:
        self.manager.connect("CoffeeShop")

        self._assert_add_sequence(
            "CoffeeShop",
            [
                "connection", "add", "type", "wifi", "ifname", "wlan0",
                "con-name", "CoffeeShop", "ssid", "CoffeeShop",
            ],
        )
        add_call = self._connection_calls()[1]
        self.assertFalse(any(arg.startswith("wifi-sec.") for arg in add_call))

    def test_enterprise_network_still_uses_wpa_eap(self) -> None:
        self.manager.connect("CorpNet", username="alice", password="pw", system_ca_certs=None)

        self._assert_add_sequence(
            "CorpNet",
            [
                "connection", "add", "type", "wifi", "ifname", "wlan0",
                "con-name", "CorpNet", "ssid", "CorpNet",
                "wifi-sec.key-mgmt", "wpa-eap",
                "802-1x.eap", "peap",
                "802-1x.phase2-auth", "mschapv2",
                "802-1x.identity", "alice",
                "802-1x.password", "pw",
            ],
        )


if __name__ == "__main__":
    unittest.main()
