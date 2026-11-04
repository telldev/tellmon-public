#!/usr/bin/env python3
"""Wait for the current service container without caching a recreated container ID."""
import argparse
import json
import subprocess
import sys
import time


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


def wait_service(compose, service, timeout):
    deadline = time.monotonic() + timeout
    last_status = "container not found"
    while time.monotonic() < deadline:
        # Resolve on every attempt: an old ID may disappear during recreation.
        containers = run(compose + ["ps", "--all", "--quiet", service])
        ids = containers.stdout.split() if containers.returncode == 0 else []
        if not ids:
            last_status = containers.stderr.strip() or "container not found"
        ready = bool(ids)
        for container_id in ids:
            result = run(["docker", "inspect", "--format", "{{json .State}}", container_id])
            if result.returncode:
                last_status = result.stderr.strip() or "container disappeared"
                ready = False
                break
            state = json.loads(result.stdout)
            status = state.get("Status", "unknown")
            health = state.get("Health", {}).get("Status")
            last_status = f"state={status}, health={health or 'not configured'}"
            if status in ("exited", "dead") or health == "unhealthy":
                raise RuntimeError(last_status)
            ready = ready and state.get("Running", False) and health in (None, "healthy")
        if ready:
            print(f"{service} is ready ({last_status})")
            return
        time.sleep(2)
    raise RuntimeError(f"timed out after {timeout}s: {last_status}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-dir", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    compose = ["docker", "compose", "--project-directory", args.compose_dir,
               "-f", args.compose_dir.rstrip("/") + "/docker-compose.yml"]
    try:
        wait_service(compose, args.service, args.timeout)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Readiness check failed for {args.service}: {error}", file=sys.stderr)
        for command in (["ps", "--all", args.service],
                        ["logs", "--no-color", "--tail", "80", args.service]):
            try:
                result = run(compose + command)
                print(result.stdout + result.stderr, file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as diagnostic_error:
                print(f"Could not collect diagnostics: {diagnostic_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
