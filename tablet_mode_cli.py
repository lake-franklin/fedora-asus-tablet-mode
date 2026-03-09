#!/usr/bin/env python3
"""
tablet-mode: CLI client for tablet-mode-daemon.

Usage:
    tablet-mode set 0            # Force computer mode
    tablet-mode set 1            # Force tablet mode
    tablet-mode unlock           # Return to automatic behavior
    tablet-mode unlock --mode 0  # Return to auto, start in computer mode
    tablet-mode unlock --mode 1  # Return to auto, start in tablet mode
    tablet-mode status           # Print current state
    tablet-mode refresh-devices  # Re-scan input devices (e.g. after suspend/resume)
"""

import socket
import sys
import os
from pathlib import Path

UID = os.getuid()
SOCKET_PATH = Path(f"/run/user/{UID}/tablet-mode.sock")


def send_command(cmd: str) -> str:
    if not SOCKET_PATH.exists():
        print(f"ERROR: socket not found at {SOCKET_PATH}. Is tablet-mode-daemon running?", file=sys.stderr)
        sys.exit(1)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(SOCKET_PATH))
            s.sendall(cmd.encode())
            return s.recv(1024).decode().strip()
    except ConnectionRefusedError:
        print("ERROR: daemon is not running or socket is stale.", file=sys.stderr)
        sys.exit(1)


def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]

    if cmd == "set":
        if len(args) < 2 or args[1] not in ("0", "1"):
            print("Usage: tablet-mode set <0|1>", file=sys.stderr)
            sys.exit(1)
        response = send_command(f"set {args[1]}")

    elif cmd == "unlock":
        payload = "unlock"
        if "--mode" in args:
            idx = args.index("--mode")
            if idx + 1 >= len(args) or args[idx + 1] not in ("0", "1"):
                print("Usage: tablet-mode unlock [--mode <0|1>]", file=sys.stderr)
                sys.exit(1)
            payload += f" --mode {args[idx + 1]}"
        response = send_command(payload)

    elif cmd == "status":
        response = send_command("status")

    elif cmd == "refresh-devices":
        response = send_command("refresh-devices")

    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)

    print(response)
    if response.startswith("ERROR"):
        sys.exit(1)


if __name__ == "__main__":
    main()
