"""Portable WARP-in-WARP control. Python standard library only."""
import argparse
import contextlib
import copy
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = "0.1.0"
CONFIG = Path("/etc/warp-vpn")
STATE = Path("/var/lib/warp-vpn")
RUNTIME = Path("/run/warp-vpn")
PREFIX = Path(__file__).resolve().parents[2]
SERVICE = "warp-vpn"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
CODEX_URL = "https://chatgpt.com/backend-api/codex/models"


class Error(Exception):
    pass


def command(args, *, capture=False, check=True, **kwargs):
    try:
        return subprocess.run([str(x) for x in args], text=True,
                              capture_output=capture, check=check, **kwargs)
    except FileNotFoundError as exc:
        raise Error(f"Command not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        # Do not dump external-tool output: a config export may contain keys.
        raise Error(f"{args[0]} failed (exit {exc.returncode}).") from exc


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Error(f"Cannot read JSON configuration: {path}") from exc


def settings():
    path = CONFIG / "settings.json"
    values = read_json(path) if path.exists() else {}
    port = values.get("proxy_port", 10881)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise Error("proxy_port must be an integer from 1024 to 65535.")
    return {"proxy_port": port, "service_manager": values.get("service_manager", "auto"),
            "mihomo": values.get("mihomo", "mihomo")}


def backend():
    name = settings()["service_manager"]
    if name == "auto":
        if Path("/run/systemd/system").is_dir():
            name = "systemd"
        elif Path("/var/service").is_dir() and shutil.which("sv"):
            name = "runit"
    if name not in ("systemd", "runit"):
        raise Error("Set service_manager to systemd or runit in /etc/warp-vpn/settings.json.")
    return name


def service_command(action, manager):
    if manager == "systemd":
        return ["systemctl", action, SERVICE + ".service"]
    verbs = {"start": "up", "stop": "down", "restart": "restart"}
    return ["sv", "-w", "20", verbs[action], "/var/service/" + SERVICE]


def active(manager=None):
    manager = manager or backend()
    args = (["systemctl", "is-active", "--quiet", SERVICE + ".service"]
            if manager == "systemd" else ["sv", "status", "/var/service/" + SERVICE])
    result = command(args, capture=True, check=False)
    return result.returncode == 0 and (manager == "systemd" or result.stdout.startswith("run:"))


def saved_mode():
    try:
        mode = (STATE / "mode").read_text().strip()
    except FileNotFoundError:
        return "proxy"
    if mode not in ("tun", "proxy"):
        raise Error("Invalid saved mode; expected tun or proxy.")
    return mode


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def control_lock():
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0:
        raise Error("Runtime directory must be owned by root and not be a symlink.")
    fd = os.open(RUNTIME / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def control(action):
    if action not in ("on", "proxy", "off", "restart", "toggle"):
        raise Error("Invalid service action.")
    manager = backend()
    # Avoid an administrator prompt when the requested mode already runs.
    if action in ("on", "proxy") and active(manager):
        if saved_mode() == ("tun" if action == "on" else "proxy"):
            return
    if os.geteuid() != 0:
        privilege = next((x for x in ("sudo", "doas", "pkexec") if shutil.which(x)), None)
        if not privilege:
            raise Error("Starting/stopping the service requires sudo, doas, or pkexec.")
        command([privilege, PREFIX / "bin/warp-vpn", "_control", action])
        return
    with control_lock():
        STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
        STATE.chmod(0o755)  # Only the mode is public; engine data lives in data/ (0700).
        was_active = active(manager)
        previous = saved_mode()
        if action == "toggle":
            action = "off" if was_active and previous == "tun" else "on"
        if action == "off":
            command(service_command("stop", manager))
            return
        desired = previous if action == "restart" else ("tun" if action == "on" else "proxy")
        if was_active and previous == desired and action != "restart":
            return
        if not (CONFIG / (desired + ".json")).is_file():
            raise Error("Tunnel configuration is missing. Run setup, then install-config.")
        atomic_write(STATE / "mode", desired + "\n", 0o644)
        try:
            command(service_command("restart" if was_active else "start", manager))
        except Error:
            atomic_write(STATE / "mode", previous + "\n", 0o644)
            command(service_command("restart" if was_active else "stop", manager), check=False)
            raise


def proxy_url():
    return f"http://127.0.0.1:{settings()['proxy_port']}"


def curl_args(*, through_proxy=True):
    return ["curl", "--disable", "--silent", "--show-error", "--connect-timeout", "3",
            "--max-time", "10", "--noproxy", "" if through_proxy else "*"] + (
                ["--proxy", proxy_url()] if through_proxy else [])


def trace(through_proxy=True):
    result = command(curl_args(through_proxy=through_proxy) + ["--fail", TRACE_URL],
                     capture=True, check=False)
    if result.returncode:
        raise Error("WARP connectivity check failed.")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def wait_ready():
    for attempt in range(5):
        try:
            if trace().get("warp") in ("on", "plus"):
                return
        except Error:
            pass
        if attempt < 4:
            time.sleep(1)
    raise Error("WARP is unavailable; the program was not started. Try warp-vpn status.")


def proxy_environment(original=None):
    env = dict(os.environ if original is None else original)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = proxy_url()
    env.update(NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1",
               NODE_USE_ENV_PROXY="1")
    return env


def check_codex(version):
    for attempt in range(2):
        result = command(curl_args() + ["--output", "/dev/null", "--write-out", "%{http_code}",
                         "--get", "--data-urlencode", "client_version=" + version, CODEX_URL],
                         capture=True, check=False)
        status = result.stdout.strip() if result.returncode == 0 else "000"
        if status in ("200", "401"):
            return
        if status == "403" and attempt == 0:
            print("Codex rejected this WARP exit (403). Reconnecting once...", file=sys.stderr)
            control("restart")
            wait_ready()
            continue
        raise Error(f"Codex is unavailable through WARP (HTTP {status}); session not started.")


def run_program(args, codex=False):
    if args == ["--help"]:
        print("Usage: codex-warp [CODEX ARGUMENTS...]" if codex else "Usage: warp-run COMMAND [ARGUMENTS...]")
        print("Start the local WARP proxy and inherit it in the program and its children.")
        return
    if args and args[0] == "--":
        args = args[1:]
    if codex:
        binary = shutil.which("codex") or shutil.which("codex-node")
        fallback = Path.home() / ".nix-profile/bin/codex-node"
        if not binary and fallback.is_file() and os.access(fallback, os.X_OK):
            binary = str(fallback)
        if not binary:
            raise Error("Install Codex first (codex or codex-node must be in PATH).")
        version = command([binary, "--version"], capture=True).stdout.strip().split()[-1]
        args = [binary, "--no-alt-screen", *args]
    if not args:
        raise Error("Usage: warp-run COMMAND [ARGUMENTS...]")
    if not shutil.which(args[0]):
        raise Error(f"Command not found: {args[0]}")
    control("proxy")
    wait_ready()
    if codex:
        check_codex(version)
    os.execvpe(args[0], args, proxy_environment())


def serve():
    if os.geteuid() != 0:
        raise Error("_serve is reserved for the system service.")
    # One supervisor and one process lock cover BOTH modes, including runit.
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(RUNTIME / "process.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise Error("Another WARP process is already running.") from exc
    os.set_inheritable(fd, True)
    mode = saved_mode()
    credentials = Path(os.environ.get("CREDENTIALS_DIRECTORY", str(CONFIG)))
    path = credentials / (mode + ".json")
    if not path.is_file():
        raise Error(f"Missing {mode} configuration. Run setup and install-config first.")
    data = STATE / "data"
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary = shutil.which(settings()["mihomo"])
    if not binary:
        raise Error("Mihomo is missing; set its path in settings.json.")
    # Inherited phone/shell proxies must not change the tunnel's upstream path.
    env = {k: v for k, v in os.environ.items() if k.lower() not in
           ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env["SKIP_SYSTEM_IPV6_CHECK"] = "1"
    os.execve(binary, [binary, "-d", str(data), "-f", str(path)], env)


def make_configs(account, proxies, port=10881):
    if not isinstance(proxies, list) or len(proxies) != 2:
        raise Error("Expected a WARP-in-WARP export with exactly two proxies.")
    outer, inner = copy.deepcopy(proxies)
    outer_device = account.get("outer", {})
    if not outer_device.get("private_key") or not account.get("private_key"):
        raise Error("The account needs two devices; use warpscout 0.16.0 or newer.")
    if outer_device["private_key"] == account["private_key"]:
        raise Error("The inner and outer tunnels must have different device keys.")
    outer["name"], inner["name"] = "warp-outer", "warp-exit"
    outer.pop("dialer-proxy", None)
    inner["dialer-proxy"] = "warp-outer"
    inner["remote-dns-resolve"] = True
    inner["dns"] = ["1.1.1.1", "1.0.0.1"]
    for proxy, device, mtu in ((outer, outer_device, 1380), (inner, account, 1280)):
        if proxy.get("type") != "wireguard" or proxy.get("private-key") != device["private_key"]:
            raise Error("Proxy/device mismatch in the WARP export.")
        proxy["mtu"] = mtu
        if device.get("ipv6"):
            proxy["ipv6"] = str(ipaddress.ip_interface(device["ipv6"]).ip)
        if not proxy.get("peers"):
            raise Error("Expected the peer-list format from warpscout mihomo-json.")
        for peer in proxy["peers"]:
            # Fixed IP endpoints avoid depending on pre-tunnel DNS.
            ipaddress.ip_address(peer["server"])
            peer["allowed-ips"] = ["0.0.0.0/0", "::/0"]
        proxy["udp"] = True
    networks = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
    networks6 = ["::1/128", "fc00::/7", "fe80::/10"]
    full = {
        "mixed-port": port, "bind-address": "127.0.0.1", "allow-lan": False,
        "mode": "rule", "log-level": "warning", "ipv6": True, "find-process-mode": "off",
        "proxies": [outer, inner],
        "dns": {"enable": True, "listen": "127.0.0.1:1053", "ipv6": True,
                "enhanced-mode": "redir-host", "nameserver": [
                    "https://1.1.1.1/dns-query#warp-exit", "https://1.0.0.1/dns-query#warp-exit"]},
        "tun": {"enable": True, "device": "warp-vpn", "stack": "gvisor", "mtu": 1280,
                "auto-route": True, "auto-redirect": True, "auto-detect-interface": True,
                "strict-route": True, "dns-hijack": ["any:53", "tcp://any:53"],
                "inet6-address": ["fdfe:dcba:9876::1/126"]},
        "rules": [f"IP-CIDR,{net},DIRECT,no-resolve" for net in networks]
                 + [f"IP-CIDR6,{net},DIRECT,no-resolve" for net in networks6] + ["MATCH,warp-exit"],
    }
    proxy = copy.deepcopy(full)
    proxy["tun"]["enable"] = False
    return full, proxy


def setup(args):
    output = args.output.expanduser().resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    if any((output / name).exists() for name in ("tun.json", "proxy.json")) and not args.force:
        raise Error("Generated configs already exist; use --force to replace them (keys are reused).")
    for binary in ("warpscout", "mihomo"):
        if not shutil.which(binary):
            raise Error(f"Install {binary} first; see the platform instructions.")
    account_path = (args.account.expanduser().resolve() if args.account else output / "warpscout-account.json")
    previous_umask = os.umask(0o077)
    try:
        if not account_path.exists():
            account_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            print("Registering your own WARP devices...", flush=True)
            command(["warpscout", "register", "-account", account_path, "-relay", "none", "-plain"])
        account_path.chmod(0o600)
        common = ["-account", account_path, "-proto", "awg", "-plain", "-no-report", "-sample", str(args.sample)]
        outer = args.outer
        if not outer:
            filters = []
            if args.country:
                filters = ["-country", args.country.upper()]
            else:
                exclude = args.exclude_country or trace(through_proxy=False).get("loc")
                if not exclude or not re.fullmatch(r"[A-Za-z]{2}(,[A-Za-z]{2})*", exclude):
                    raise Error("Cannot detect your country; pass --exclude-country explicitly.")
                filters = ["-exclude-country", exclude.upper()]
            print("Selecting an outer endpoint on a foreign Cloudflare node...", flush=True)
            result = command(["warpscout", "scan", *common, *filters, "-best"], capture=True)
            outer = result.stdout.strip()
        if not re.fullmatch(r"(?:[0-9.]+|\[[0-9a-fA-F:]+\]):[0-9]{1,5}", outer):
            raise Error("Expected an outer endpoint in IP:port format (IPv6 in brackets).")
        export = output / "nested-proxies.json"
        scan = ["warpscout", "scan", *common, "-through", outer, "-inner-proto", "wg",
                "-conf", export, "-conf-type", "mihomo-json"]
        if args.inner:
            host, port = args.inner.rsplit(":", 1)
            ipaddress.ip_address(host.strip("[]"))
            if not 1 <= int(port) <= 65535:
                raise Error("Invalid inner endpoint port.")
            scan += ["-target", host.strip("[]"), "-port", port]
        print("Checking the inner tunnel and exporting the chain...", flush=True)
        command(scan)
        export.chmod(0o600)
        full, proxy = make_configs(read_json(account_path), read_json(export), args.port)
        for name, contents in (("tun.json", full), ("proxy.json", proxy)):
            atomic_write(output / name, json.dumps(contents, indent=2) + "\n")
            command(["mihomo", "-t", "-d", output / "state", "-f", output / name])
        atomic_write(output / "settings.json", json.dumps({"proxy_port": args.port}, indent=2) + "\n")
    finally:
        os.umask(previous_umask)
    print(f"Private configs saved in {output}\nNext: sudo warp-vpn install-config {output}")


def install_config(source, force=False):
    if os.geteuid() != 0:
        raise Error("Use sudo/doas warp-vpn install-config DIRECTORY.")
    source = source.expanduser().resolve()
    full, proxy = (read_json(source / name) for name in ("tun.json", "proxy.json"))
    for data in (full, proxy):
        if data.get("allow-lan") is not False or data.get("bind-address") != "127.0.0.1":
            raise Error("The proxy must listen on localhost only.")
    if not full.get("tun", {}).get("enable") or proxy.get("tun", {}).get("enable"):
        raise Error("Expected tun.json with TUN enabled and proxy.json with TUN disabled.")
    port = full.get("mixed-port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or proxy.get("mixed-port") != port:
        raise Error("Both configs must use the same unprivileged mixed-port.")
    old_settings = settings()
    try:
        if active():
            raise Error("Stop WARP before replacing its configuration: warp-vpn off")
    except Error as exc:
        if "Set service_manager" not in str(exc):
            raise
    if any((CONFIG / name).exists() for name in ("tun.json", "proxy.json")) and not force:
        raise Error("Installed configs exist; use --force after making a private backup.")
    CONFIG.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name, contents in (("tun.json", full), ("proxy.json", proxy)):
        atomic_write(CONFIG / name, json.dumps(contents, indent=2) + "\n")
    old_settings["proxy_port"] = port
    atomic_write(CONFIG / "settings.json", json.dumps(old_settings, indent=2) + "\n", 0o644)
    print("Installed private configs; WARP remains off.")


def cli(argv):
    if argv[:1] == ["_serve"]:
        if len(argv) != 1:
            raise Error("_serve takes no arguments.")
        serve()
        return
    if argv[:1] == ["_control"]:
        if len(argv) != 2 or os.geteuid() != 0:
            raise Error("_control requires root and one service action.")
        control(argv[1])
        return
    parser = argparse.ArgumentParser(description="WARP-in-WARP for Linux (systemd and runit).")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("on", "proxy", "off", "restart", "toggle", "status", "doctor"):
        sub.add_parser(action)
    p = sub.add_parser("setup", help="register devices, select endpoints, generate private configs")
    p.add_argument("--output", type=Path, default=Path.home() / ".local/share/warp-vpn")
    p.add_argument("--account", type=Path)
    countries = p.add_mutually_exclusive_group()
    countries.add_argument("--country")
    countries.add_argument("--exclude-country")
    p.add_argument("--outer", help="outer IP:port; omit for automatic selection")
    p.add_argument("--inner", help="inner IP:port; omit for automatic selection")
    p.add_argument("--sample", type=int, default=3, choices=range(1, 257), metavar="1..256")
    p.add_argument("--port", type=int, default=10881)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("install-config", help="install generated configs, as root")
    p.add_argument("directory", type=Path)
    p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "setup":
        if not 1024 <= args.port <= 65535:
            raise Error("--port must be between 1024 and 65535.")
        setup(args)
    elif args.action == "install-config":
        install_config(args.directory, args.force)
    elif args.action in ("status", "doctor"):
        manager = backend()
        running = active(manager)
        print(f"Service manager: {manager}; mode: {saved_mode() if running else 'off'}")
        if args.action == "doctor":
            for name in ("curl", "mihomo", "warpscout"):
                print(f"{name}: {shutil.which(settings()['mihomo'] if name == 'mihomo' else name) or 'missing'}")
            print(f"Proxy: {proxy_url()}; private configs: {CONFIG}")
        if running:
            facts = trace(through_proxy=saved_mode() == "proxy")
            for key in ("loc", "colo", "warp"):
                print(f"{key}={facts.get(key, '?')}")
    else:
        control(args.action)
        if args.action in ("on", "proxy", "restart"):
            wait_ready()


def main(program="warp-vpn"):
    try:
        if program == "warp-run":
            run_program(sys.argv[1:])
        elif program == "codex-warp":
            run_program(sys.argv[1:], codex=True)
        else:
            cli(sys.argv[1:])
    except (Error, ValueError, OSError) as exc:
        print(f"warp-vpn: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
