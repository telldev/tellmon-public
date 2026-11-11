#!/usr/bin/env python3
"""Restore host prerequisites and run Compose's ordered startup after boot."""

import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time


PROJECT_DIR = Path("/docker/tellmon")


def wait_for(check, description, timeout=90):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {description}")
        time.sleep(1)


def socket_ready(path):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
        return True
    except OSError:
        return False


def prepare_devices(model):
    devices = {
        device["source"]
        for service in model["services"].values()
        for device in service.get("devices", [])
    }
    # /dev is rebuilt on every boot. Installation-time mknod and symlinks
    # therefore cannot be relied on when Docker restores the containers.
    if "/dev/ppp" in devices:
        subprocess.run(["modprobe", "ppp_generic"], check=True)
        ppp = Path("/dev/ppp")
        if not ppp.exists():
            os.mknod(ppp, stat.S_IFCHR | 0o600, os.makedev(108, 0))
        ppp.chmod(0o666)

    for alias, target in {
        "/dev/ttyUSB-UC20-AT": "/dev/ttyUSB2",
        "/dev/ttyUSB-UC20-Modem": "/dev/ttyUSB3",
    }.items():
        if alias in devices and not os.path.lexists(alias):
            os.symlink(target, alias)

    for device in sorted(devices):
        wait_for(lambda: Path(device).is_char_device(), device)


def main():
    os.chdir(PROJECT_DIR)
    compose = ["docker", "compose", "-f", "docker-compose.yml"]
    # Keep configuration (which can contain passwords) out of the journal.
    model = json.loads(subprocess.check_output(compose + ["config", "--format", "json"]))
    prepare_devices(model)
    wait_for(lambda: socket_ready(PROJECT_DIR / "reboot.sock"), "host reboot listener")
    # Use the same dependency order as installation and updates. Separate
    # Compose calls avoid stale IDs when replacing dependencies and socket mounts.
    subprocess.run(
        ["/usr/bin/python3", "/usr/local/bin/tellmon-compose-start.py",
         "--compose-dir", str(PROJECT_DIR)],
        check=True,
    )


if __name__ == "__main__":
    main()
