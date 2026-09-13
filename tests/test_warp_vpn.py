import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("warp_vpn", ROOT / "lib/warp-vpn/warp_vpn.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def fixtures():
    # Deliberately invalid placeholder keys, never registered with any service.
    account = {"private_key": "TEST-INNER-NOT-A-KEY", "ipv6": "2001:db8::2/128",
               "outer": {"private_key": "TEST-OUTER-NOT-A-KEY", "ipv6": "2001:db8::1/128"}}
    proxies = [{"type": "wireguard", "private-key": account["outer"]["private_key"],
                "name": "outer-original", "peers": [{"server": "192.0.2.1", "port": 2408}]},
               {"type": "wireguard", "private-key": account["private_key"],
                "name": "inner-original", "peers": [{"server": "198.51.100.1", "port": 2408}]}]
    return account, proxies


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for name in ("CONFIG", "STATE", "RUNTIME"):
            path = self.base / name.lower()
            path.mkdir()
            p = patch.object(app, name, path)
            p.start()
            self.addCleanup(p.stop)
        (app.CONFIG / "settings.json").write_text(json.dumps({"service_manager": "systemd"}))


class ConfigurationTests(Isolated):
    def test_chain_and_modes_share_keys_but_not_tun(self):
        account, proxies = fixtures()
        original = copy.deepcopy(proxies)
        full, proxy = app.make_configs(account, proxies, 12345)
        self.assertEqual(original, proxies)
        self.assertTrue(full["tun"]["enable"])
        self.assertFalse(proxy["tun"]["enable"])
        self.assertEqual(full["proxies"], proxy["proxies"])
        self.assertEqual(full["proxies"][1]["dialer-proxy"], "warp-outer")
        self.assertNotIn("dialer-proxy", full["proxies"][0])
        self.assertEqual(full["bind-address"], "127.0.0.1")
        self.assertFalse(full["allow-lan"])
        self.assertEqual(proxy["mixed-port"], 12345)
        self.assertEqual(full["rules"][-1], "MATCH,warp-exit")

    def test_same_device_keys_rejected(self):
        account, proxies = fixtures()
        account["outer"]["private_key"] = account["private_key"]
        with self.assertRaisesRegex(app.Error, "different device keys"):
            app.make_configs(account, proxies)

    def test_mismatched_export_rejected(self):
        account, proxies = fixtures()
        proxies[1]["private-key"] = "wrong-device"
        with self.assertRaisesRegex(app.Error, "mismatch"):
            app.make_configs(account, proxies)

    def test_atomic_private_write_replaces_without_relaxing_permissions(self):
        target = self.base / "private.json"
        target.write_text("old")
        target.chmod(0o644)
        app.atomic_write(target, "replacement")
        self.assertEqual(target.read_text(), "replacement")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.base.glob(".private.json.*")))

    def test_install_rejects_public_listener(self):
        full, proxy = app.make_configs(*fixtures())
        full["allow-lan"] = True
        for name, data in (("tun.json", full), ("proxy.json", proxy)):
            (self.base / name).write_text(json.dumps(data))
        with patch.object(app.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(app.Error, "localhost"):
                app.install_config(self.base)

    def test_install_stops_on_existing_config_without_force(self):
        full, proxy = app.make_configs(*fixtures())
        for name, data in (("tun.json", full), ("proxy.json", proxy)):
            (self.base / name).write_text(json.dumps(data))
        (app.CONFIG / "tun.json").write_text("keep me")
        with patch.object(app.os, "geteuid", return_value=0), patch.object(app, "active", return_value=False):
            with self.assertRaisesRegex(app.Error, "Installed configs exist"):
                app.install_config(self.base)
        self.assertEqual((app.CONFIG / "tun.json").read_text(), "keep me")


class ServiceTests(Isolated):
    def setUp(self):
        super().setUp()
        for name in ("tun.json", "proxy.json"):
            (app.CONFIG / name).write_text("{}")
        for p in (patch.object(app.os, "geteuid", return_value=0),
                  patch.object(app, "control_lock", contextlib.nullcontext)):
            p.start()
            self.addCleanup(p.stop)

    def test_switch_restarts_one_service_and_saves_mode(self):
        app.atomic_write(app.STATE / "mode", "tun\n")
        with patch.object(app, "active", return_value=True), patch.object(app, "command") as run:
            app.control("proxy")
        self.assertEqual(app.saved_mode(), "proxy")
        run.assert_called_once_with(["systemctl", "restart", "warp-vpn.service"])

    def test_already_active_mode_is_idempotent(self):
        app.atomic_write(app.STATE / "mode", "proxy\n")
        with patch.object(app, "active", return_value=True), patch.object(app, "command") as run:
            app.control("proxy")
        run.assert_not_called()

    def test_failed_switch_restores_previous_mode(self):
        app.atomic_write(app.STATE / "mode", "tun\n")
        with patch.object(app, "active", return_value=True), patch.object(app, "command", side_effect=[app.Error("failed"), None]):
            with self.assertRaises(app.Error):
                app.control("proxy")
        self.assertEqual(app.saved_mode(), "tun")

    def test_runit_down_status_is_not_active(self):
        with patch.object(app, "command", return_value=subprocess.CompletedProcess([], 0, "down: /var/service/warp-vpn: 30s\n")):
            self.assertFalse(app.active("runit"))

    def test_runit_running_status_is_active(self):
        with patch.object(app, "command", return_value=subprocess.CompletedProcess([], 0, "run: /var/service/warp-vpn: (pid 123) 30s\n")):
            self.assertTrue(app.active("runit"))

    def test_runit_switch_uses_supervised_service(self):
        (app.CONFIG / "settings.json").write_text('{"service_manager":"runit"}')
        with patch.object(app, "active", return_value=False), patch.object(app, "command") as run:
            app.control("on")
        self.assertEqual(app.saved_mode(), "tun")
        run.assert_called_once_with(["sv", "-w", "20", "up", "/var/service/warp-vpn"])


class LauncherTests(Isolated):
    def test_real_child_inherits_proxy_and_literal_arguments(self):
        # Start a real process tree, isolating only service/network startup.
        # This checks exec/environment behavior, not a live VPN connection.
        script = self.base / "exec-check.py"
        module = ROOT / "lib/warp-vpn"
        child = 'import os,sys,json; print(json.dumps([os.environ["HTTPS_PROXY"],sys.argv[1]]))'
        literal = 'literal $(not-a-command) with spaces'
        parent = f'import subprocess,sys; print(subprocess.check_output([sys.executable,"-c",{child!r},{literal!r}],text=True),end="")'
        script.write_text(f'import sys\nsys.path.insert(0,{str(module)!r})\nimport warp_vpn as app\nfrom pathlib import Path\napp.CONFIG=Path({str(app.CONFIG)!r})\napp.control=lambda action: None\napp.wait_ready=lambda: None\napp.run_program([sys.executable,"-c",{parent!r}])\n')
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["http://127.0.0.1:10881", literal])

    def test_phone_proxy_replaced_and_children_keep_other_environment(self):
        original = {"HTTPS_PROXY": "http://phone:8080", "https_proxy": "http://phone:8080",
                    "NO_PROXY": "*", "PATH": "/usr/bin", "OTHER": "preserved"}
        env = app.proxy_environment(original)
        self.assertEqual(original["NO_PROXY"], "*")
        self.assertEqual(env["OTHER"], "preserved")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.assertEqual(env[key], "http://127.0.0.1:10881")
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")

    def test_403_reconnects_once_then_allows_reachable_endpoint(self):
        answers = [subprocess.CompletedProcess([], 0, "403"), subprocess.CompletedProcess([], 0, "401")]
        with patch.object(app, "command", side_effect=answers), patch.object(app, "control") as control, patch.object(app, "wait_ready") as ready:
            app.check_codex("0.153.3")
        control.assert_called_once_with("restart")
        ready.assert_called_once()

    def test_persistent_403_does_not_retry_forever(self):
        with patch.object(app, "command", return_value=subprocess.CompletedProcess([], 0, "403")), patch.object(app, "control") as control, patch.object(app, "wait_ready"):
            with self.assertRaisesRegex(app.Error, "HTTP 403"):
                app.check_codex("0.153.3")
        control.assert_called_once()

    def test_rate_limit_does_not_trigger_reconnect(self):
        with patch.object(app, "command", return_value=subprocess.CompletedProcess([], 0, "429")), patch.object(app, "control") as control:
            with self.assertRaisesRegex(app.Error, "HTTP 429"):
                app.check_codex("0.153.3")
        control.assert_not_called()

    def test_unavailable_warp_never_launches_program(self):
        with patch.object(app.shutil, "which", return_value="/usr/bin/curl"), patch.object(app, "control"), patch.object(app, "wait_ready", side_effect=app.Error("offline")), patch.object(app.os, "execvpe") as execute:
            with self.assertRaises(app.Error):
                app.run_program(["curl", "https://example.com"])
        execute.assert_not_called()


class PackagingTests(unittest.TestCase):
    def test_reinstall_updates_backend_and_path_but_preserves_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "etc/warp-vpn/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"service_manager":"systemd","mihomo":"/old/mihomo","proxy_port":12345}')
            subprocess.run([sys.executable, str(ROOT / "install.py"), "--backend", "runit",
                            "--mihomo", "/usr/local/bin/mihomo", "--destdir", directory], check=True, capture_output=True)
            self.assertEqual(json.loads(settings.read_text()), {"service_manager":"runit", "mihomo":"/usr/local/bin/mihomo", "proxy_port":12345})

    def test_staged_install_for_both_supervisors(self):
        for manager in ("runit", "systemd"):
            with self.subTest(manager=manager), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(["python3", str(ROOT / "install.py"), "--backend", manager,
                                "--mihomo", "/usr/bin/mihomo", "--destdir", directory], check=True, capture_output=True)
                binary = root / "usr/local/bin/warp-vpn"
                self.assertTrue(os.access(binary, os.X_OK))
                result = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), app.VERSION)
                self.assertFalse((root / "etc/warp-vpn/tun.json").exists())
                self.assertFalse((root / "etc/warp-vpn/proxy.json").exists())
                if manager == "runit":
                    self.assertTrue((root / "etc/sv/warp-vpn/down").is_file())
                    self.assertEqual(os.readlink(root / "var/service/warp-vpn"), "/etc/sv/warp-vpn")
                else:
                    self.assertIn("RuntimeDirectoryPreserve=yes", (root / "etc/systemd/system/warp-vpn.service").read_text())
                    self.assertFalse((root / "etc/systemd/system/multi-user.target.wants").exists())


if __name__ == "__main__":
    unittest.main()
