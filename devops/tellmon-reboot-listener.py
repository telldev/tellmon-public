#!/usr/bin/env python3
import socket
import os
import subprocess
import re
import ipaddress
import json
import struct
import tempfile
import errno
import sys

# Containers mount this directory (not the socket file), so a socket recreated
# by a listener restart is visible to them without recreating the containers.
SOCKET_DIR = "/docker/tellmon/run"
SOCKET_PATH = os.path.join(SOCKET_DIR, "reboot.sock")
HOST_COMMAND = "/usr/local/lib/tellmon/bin/command.sh"
# Runs on the host so it reports the host's memory, processes, users and disk.
SYSTEM_INFO_SCRIPT = "/usr/local/lib/tellmon/bin/scripts/T02system-info.sh"
SOCKET_GID = int(os.environ.get("TELLMON_SOCKET_GID", "10001"))
ALLOWED_SOCKET_UIDS = {0, int(os.environ.get("TELLMON_SOCKET_UID", "10001"))}

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


# Host name or IPv4 address; no spaces, slashes or shell characters.
NTP_SERVER_PATTERN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
# IANA zone name such as Europe/Budapest or UTC; no dots, so no "..".
TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")
ZONEINFO_DIR = "/usr/share/zoneinfo"
TIMESYNCD_DROPIN = "/etc/systemd/timesyncd.conf.d/tellmon.conf"
NTP_CONF = "/etc/ntp.conf"
# The containers' entrypoints export this as TZ, so Java's local time follows the host.
CONTAINER_TIMEZONE_FILE = "/docker/tellmon/config/timezone"


def is_valid_ntp_server(server):
    return bool(NTP_SERVER_PATTERN.match(server))


def is_valid_timezone(timezone, zoneinfo_dir=ZONEINFO_DIR):
    return bool(TIMEZONE_PATTERN.match(timezone)) and os.path.isfile(os.path.join(zoneinfo_dir, timezone))


def is_valid_system_info_args(args):
    """LAN1, LAN2 and GSM address (IPv4 or "-") followed by six ports (number or
    "-" when the port is not configured, so one missing setting does not hide
    the rest of the system information)."""
    if len(args) != 9:
        return False
    addresses, ports = args[:3], args[3:]
    return (all(address == "-" or is_valid_ipv4(address) for address in addresses)
            and all(port == "-" or (port.isdigit() and 0 < int(port) < 65536) for port in ports))


def system_info(args):
    result = subprocess.run([SYSTEM_INFO_SCRIPT, *args], capture_output=True, text=True,
                            timeout=60, check=False)
    return result.stdout.strip()


def write_file_atomically(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=directory, prefix=".tellmon-") as f:
        f.write(content)
        tmp_path = f.name
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, path)


def set_timezone(timezone):
    """@return True if the host time zone was changed."""
    if subprocess.run(["timedatectl", "set-timezone", timezone], check=False).returncode != 0:
        return False
    write_container_timezone()
    return True


def write_container_timezone():
    """Publish the host time zone for the containers; they read it when they start."""
    result = subprocess.run(["timedatectl", "show", "--property=Timezone", "--value"],
                            capture_output=True, text=True, check=False)
    timezone = result.stdout.strip()
    if result.returncode == 0 and is_valid_timezone(timezone):
        write_file_atomically(CONTAINER_TIMEZONE_FILE, timezone + "\n")


def set_ntp_server(server):
    """Use the host's NTP client: systemd-timesyncd, or the ntp package on older hosts.

    @return True if the NTP client was restarted with the new server."""
    if subprocess.run(["systemctl", "is-active", "--quiet", "ntp"], check=False).returncode == 0:
        with open(NTP_CONF, encoding="utf-8") as f:
            lines = [line for line in f if not line.startswith("pool ")]
        lines.append(f"pool {server} iburst\n")
        write_file_atomically(NTP_CONF, "".join(lines))
        return subprocess.run(["systemctl", "restart", "ntp"], check=False).returncode == 0

    write_file_atomically(TIMESYNCD_DROPIN, f"# Managed by TELLMon (time settings on the web UI).\n[Time]\nNTP={server}\n")
    subprocess.run(["timedatectl", "set-ntp", "true"], check=False)
    return subprocess.run(["systemctl", "restart", "systemd-timesyncd"], check=False).returncode == 0


def is_authorized_peer(conn):
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return uid in ALLOWED_SOCKET_UIDS
    except OSError:
        return False


def prepare_socket_path(path):
    if not os.path.lexists(path):
        return
    # Docker may create a directory when the socket is absent. Preserve
    # unexpected contents for inspection, allowing the listener to recover.
    if os.path.isdir(path) and not os.path.islink(path):
        try:
            os.rmdir(path)
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            backup = tempfile.mkdtemp(prefix="reboot.sock.backup-", dir=os.path.dirname(path))
            os.rename(path, os.path.join(backup, "contents"))
            print(f"Preserved unexpected socket directory at {backup}/contents", file=sys.stderr, flush=True)
    else:
        os.remove(path)


def prepare_socket_dir(path):
    os.makedirs(path, exist_ok=True)
    os.chown(path, 0, SOCKET_GID)
    os.chmod(path, 0o750)


def main():
    # The listener starts before Docker at boot, so the containers start with
    # the current host time zone.
    try:
        write_container_timezone()
    except Exception as error:
        print(f"Could not write {CONTAINER_TIMEZONE_FILE}: {error}", file=sys.stderr, flush=True)
    prepare_socket_dir(SOCKET_DIR)
    prepare_socket_path(SOCKET_PATH)
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

                if cmd in ("REBOOT", "SHUTDOWN"):
                    conn.close()
                    server.close()
                    os.remove(SOCKET_PATH)
                    if cmd == "REBOOT":
                        subprocess.run(["reboot"])
                    else:
                        subprocess.run(["shutdown", "-h", "now"])
                    break
                # Time settings reply OK or ERR, so the web UI can report a
                # rejected or failed change instead of a false success.
                elif cmd == "SET_NTP":
                    ok = len(parts) == 2 and is_valid_ntp_server(parts[1]) and set_ntp_server(parts[1])
                    conn.sendall(b"OK" if ok else b"ERR")
                elif cmd == "SET_TIMEZONE":
                    ok = len(parts) == 2 and is_valid_timezone(parts[1]) and set_timezone(parts[1])
                    conn.sendall(b"OK" if ok else b"ERR")
                elif cmd == "SYSTEM_INFO" and is_valid_system_info_args(parts[1:]):
                    conn.sendall(system_info(parts[1:]).encode("utf-8"))
                elif cmd == "RESTART_RECEIVER":
                    subprocess.run(["docker", "restart", "tellmon_receiver"])
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


if __name__ == "__main__":
    main()
