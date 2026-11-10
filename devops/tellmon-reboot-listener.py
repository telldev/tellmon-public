#!/usr/bin/env python3
import socket
import os
import subprocess
import re
import base64
import ipaddress
import json
import struct
import tempfile

SOCKET_PATH = "/docker/tellmon/reboot.sock"
HOST_COMMAND = "/usr/local/lib/tellmon/bin/command-host.sh"
COPY_FILE_COMMAND = "/usr/local/lib/tellmon/bin/scripts/T02copy-file.sh"
SOCKET_GID = int(os.environ.get("TELLMON_SOCKET_GID", "10001"))
ALLOWED_SOCKET_UIDS = {0, int(os.environ.get("TELLMON_SOCKET_UID", "10001"))}

# Whitelist of services allowed to be restarted via the socket
ALLOWED_SERVICES = {"tomcat", "ntp", "postgresql", "caddy", "docker"}


def get_ip_addresses():
    """Return the host's non-loopback IPv4 addresses and interface details."""
    result = subprocess.run(
        ["ip", "-j", "address", "show"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )

    addresses = []
    for interface in json.loads(result.stdout):
        interface_name = interface.get("ifname")
        mac_address = interface.get("address")

        for address_info in interface.get("addr_info", []):
            ip_address = address_info.get("local")
            if not interface_name or not ip_address:
                continue

            parsed_ip = ipaddress.ip_address(ip_address)
            if parsed_ip.version != 4 or parsed_ip.is_loopback:
                continue

            addresses.append({
                "ipAddress": ip_address,
                "ifaceName": interface_name,
                "macAddress": mac_address,
            })

    return addresses

def is_valid_ipv4(ip):
    """Validate that the string is a valid IPv4 address."""
    pattern = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
    if not pattern.match(ip):
        return False
    return all(0 <= int(num) <= 255 for num in ip.split("."))


def is_authorized_peer(conn):
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return uid in ALLOWED_SOCKET_UIDS
    except OSError:
        return False


def apply_netplan_config(decoded_content):
    if len(decoded_content) > 65536 or "\x00" in decoded_content:
        return

    with tempfile.NamedTemporaryFile("w", delete=False, dir="/etc/netplan", prefix="tellmon-", suffix=".yaml") as f:
        tmp_path = f.name
        f.write(decoded_content)

    target_path = "/etc/netplan/00-installer-config.yaml"
    backup_path = target_path + ".tellmon-backup"
    try:
        if os.path.exists(target_path):
            os.replace(target_path, backup_path)
        os.replace(tmp_path, target_path)
        generated = subprocess.run(["netplan", "generate"], check=False)
        if generated.returncode == 0:
            subprocess.run(["netplan", "apply"], check=False)
        elif os.path.exists(backup_path):
            os.replace(backup_path, target_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


if os.path.exists(SOCKET_PATH):
    # Docker's short bind syntax creates a directory if the socket is absent
    # during daemon startup. Remove only an empty directory left by that race;
    # fail visibly for any other unexpected contents or permission problem.
    if os.path.isdir(SOCKET_PATH):
        os.rmdir(SOCKET_PATH)
    else:
        os.remove(SOCKET_PATH)

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(SOCKET_PATH)
os.chown(SOCKET_PATH, 0, SOCKET_GID)
os.chmod(SOCKET_PATH, 0o660)
server.listen(1)

try:
    while True:
        conn, _ = server.accept()
        try:
            if not is_authorized_peer(conn):
                continue
            data = conn.recv(1024).decode('utf-8').strip()
            parts = data.split()
            if not parts:
                continue
            
            cmd = parts[0]
            
            if cmd == "REBOOT":
                conn.close()
                server.close()
                os.remove(SOCKET_PATH)
                subprocess.run(["reboot"])
                break
            elif cmd == "RESTART_RECEIVER":
                subprocess.run(["docker", "restart", "tellmon_receiver"])
            elif cmd == "LOAD_3G_STICK_DRIVERS":
                subprocess.run([HOST_COMMAND, "load_3g_stick_drivers"])
            elif cmd == "ROUTE_3G" and len(parts) == 2 and is_valid_ipv4(parts[1]):
                subprocess.run([HOST_COMMAND, "route_3g", parts[1]])
            elif cmd == "DELETE_WVDIAL_CONFIG":
                subprocess.run([HOST_COMMAND, "delete_wvdial_config_file"])
            elif cmd == "COPY_WVDIAL_CONFIG":
                subprocess.run([HOST_COMMAND, "copy_wvdial_config_file"])
            elif cmd == "RESTART_INTERNAL_3G":
                subprocess.run([HOST_COMMAND, "restart_internal_3g"])
            elif cmd == "RESTART_WVDIAL_3G":
                subprocess.run([HOST_COMMAND, "restart_wvdial_3g"])
            elif cmd == "START_MODEM":
                subprocess.run([HOST_COMMAND, "start_modem"])
            elif cmd == "LOAD_PCI_MODEM_DRIVER":
                subprocess.run([HOST_COMMAND, "load_pci_modem_driver"])
            elif cmd == "RESTART_SERVICE" and len(parts) == 2 and parts[1] in ALLOWED_SERVICES:
                subprocess.run([HOST_COMMAND, "restart_service", parts[1]])
            elif cmd == "RESET_NETWORK":
                subprocess.run([COPY_FILE_COMMAND, "reset-network"])
            elif cmd == "SET_NETWORK" and len(parts) == 2:
                try:
                    decoded_content = base64.b64decode(parts[1], validate=True).decode('utf-8')
                    apply_netplan_config(decoded_content)
                except Exception:
                    pass
            elif cmd == "GET_IP_ADDRESSES" and len(parts) == 1:
                conn.sendall(json.dumps(get_ip_addresses()).encode("utf-8"))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
except KeyboardInterrupt:
    pass
finally:
    if os.path.exists(SOCKET_PATH):
        try:
            os.remove(SOCKET_PATH)
        except OSError:
            pass
