#!/usr/bin/env python3
"""Install to Linux, or stage files with --destdir (no host service changes)."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parent


def install(args):
    staging = args.destdir is not None
    root = args.destdir.resolve() if staging else Path("/")
    if not staging and os.geteuid() != 0:
        raise SystemExit("Run with sudo/doas, or use --destdir for packaging.")
    if not staging and Path("/etc/NIXOS").exists():
        raise SystemExit("On NixOS use nix/module.nix; see docs/nixos.md.")
    prefix = Path(args.prefix)
    if not prefix.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(prefix)) or ".." in prefix.parts:
        raise SystemExit("--prefix must use only letters, digits, '/', '.', '_' and '-', without '..'.")
    manager = args.backend
    if manager == "auto":
        manager = "systemd" if Path("/run/systemd/system").is_dir() else "runit" if Path("/var/service").is_dir() else None
    if not manager:
        raise SystemExit("Specify --backend systemd or --backend runit.")
    mihomo = args.mihomo or shutil.which("mihomo")
    if not mihomo or not Path(mihomo).is_absolute() or any(c.isspace() for c in mihomo):
        raise SystemExit("Install Mihomo first or pass its absolute path with --mihomo.")

    def destination(path):
        return root / str(path).lstrip("/")

    def write(path, text, mode=0o644):
        target = destination(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(mode)

    for name in ("warp-vpn", "warp-run", "codex-warp"):
        write(prefix / "bin" / name, (SOURCE / "bin" / name).read_text(), 0o755)
    write(prefix / "lib/warp-vpn/warp_vpn.py", (SOURCE / "lib/warp-vpn/warp_vpn.py").read_text())
    control_path = destination("/etc/warp-vpn/settings.json")
    control = json.loads(control_path.read_text()) if control_path.exists() else {"proxy_port": 10881}
    control.update(service_manager=manager, mihomo=mihomo)
    write("/etc/warp-vpn/settings.json", json.dumps(control, indent=2) + "\n")
    for path, mode in (("/var/lib/warp-vpn", 0o755), ("/var/lib/warp-vpn/data", 0o700),
                       ("/run/warp-vpn", 0o755)):
        target = destination(path)
        target.mkdir(mode=mode, parents=True, exist_ok=True)
        target.chmod(mode)
    replacements = {"@PREFIX@": str(prefix)}
    if manager == "systemd":
        text = (SOURCE / "packaging/systemd/warp-vpn.service").read_text()
        for old, new in replacements.items():
            text = text.replace(old, new)
        write("/etc/systemd/system/warp-vpn.service", text)
        if not staging:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
    else:
        text = (SOURCE / "packaging/runit/run").read_text().replace("@PREFIX@", str(prefix))
        write("/etc/sv/warp-vpn/run", text, 0o755)
        write("/etc/sv/warp-vpn/down", "")
        # On a real Void host /var/service is a symlink to the active runsvdir.
        link = destination("/var/service/warp-vpn")
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists() and not link.is_symlink():
            link.symlink_to("/etc/sv/warp-vpn")
    for name in ("warp-vpn.desktop", "codex-warp.desktop"):
        text = (SOURCE / "packaging" / name).read_text().replace("@PREFIX@", str(prefix))
        write(prefix / "share/applications" / name, text)
    print(f"{'Staged' if staging else 'Installed'} for {manager} at {root}. Service not started; autostart not enabled.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("auto", "systemd", "runit"), default="auto")
    parser.add_argument("--prefix", default="/usr/local")
    parser.add_argument("--destdir", type=Path)
    parser.add_argument("--mihomo", help="absolute path of the installed Mihomo binary")
    install(parser.parse_args())
