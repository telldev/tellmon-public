#!/usr/bin/env python3
"""Start services separately so dependency checks never retain obsolete IDs."""
import argparse
import json
import subprocess
import sys
import time


SERVICES = ("postgres", "manager", "receiver", "backend", "caddy")


def inspect_command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


def wait_service(compose, service, timeout):
    deadline = time.monotonic() + timeout
    last_status = "container not found"
    while time.monotonic() < deadline:
        # Resolve IDs on every attempt, including after another recreation.
        result = inspect_command(compose + ["ps", "--all", "--quiet", service])
        ids = result.stdout.split() if result.returncode == 0 else []
        ready = bool(ids)
        if not ids:
            last_status = result.stderr.strip() or "container not found"
        for container_id in ids:
            result = inspect_command(["docker", "inspect", "--format", "{{json .State}}", container_id])
            if result.returncode:
                ready = False
                last_status = result.stderr.strip() or "container disappeared"
                break
            state = json.loads(result.stdout)
            status = state.get("Status", "unknown")
            health = state.get("Health", {}).get("Status")
            last_status = f"state={status}, health={health or 'not configured'}"
            if status in ("exited", "dead") or health == "unhealthy":
                raise RuntimeError(f"{service}: {last_status}")
            ready = ready and status == "running" and state.get("Running", False) and health in (None, "healthy")
        if ready:
            print(f"{service} is ready ({last_status})", flush=True)
            return
        time.sleep(2)
    raise RuntimeError(f"{service}: timed out after {timeout}s ({last_status})")


def start_stack(compose, pull=False, timeout=300):
    subprocess.run(compose + ["config", "--quiet"], check=True)
    if pull:
        # A failed download must leave the running installation intact.
        print("Pulling updated images", flush=True)
        try:
            subprocess.run(compose + ["pull", "--quiet"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            details = (error.stderr or error.stdout or str(error)).strip()
            raise RuntimeError(
                f"Image download failed; containers have not been stopped.\n{details}"
            ) from error
    for service in SERVICES:
        try:
            # The listener replaces its socket at boot/update. Rebind consumers
            # and reload shared properties, but let Compose reuse postgres/caddy
            # when their image and configuration have not changed.
            recreate = ["--force-recreate"] if service in ("manager", "receiver", "backend") else []
            print(f"Starting {service}", flush=True)
            subprocess.run(compose + ["up", "--detach", "--no-deps"] + recreate +
                           ["--pull", "never", service], check=True)
            wait_service(compose, service, timeout)
        except (subprocess.SubprocessError, RuntimeError, ValueError, OSError):
            # Surface the actual container failure instead of a missing dependency ID.
            for diagnostic in (["ps", "--all", service], ["logs", "--no-color", "--tail", "80", service]):
                try:
                    result = inspect_command(compose + diagnostic)
                    print(result.stdout + result.stderr, file=sys.stderr, flush=True)
                except (OSError, subprocess.SubprocessError) as error:
                    print(f"Could not collect {service} diagnostics: {error}", file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-dir", default="/docker/tellmon")
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    compose = ["docker", "compose", "--project-directory", args.compose_dir,
               "-f", args.compose_dir.rstrip("/") + "/docker-compose.yml"]
    try:
        start_stack(compose, args.pull, args.timeout)
    except (subprocess.SubprocessError, RuntimeError, ValueError, OSError) as error:
        print(f"Tellmon startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
