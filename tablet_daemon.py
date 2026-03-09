
#!/usr/bin/env python3
"""
tablet-mode-daemon: Manages tablet mode on ASUS 2-in-1 laptops using WMI hinge events.

Listens to acpi_listen for WMI hinge events and toggles keyboard/touchpad
inhibition based on inferred hinge position.

State file: ~/.local/share/tablet-mode/state
Log file:   ~/.local/share/tablet-mode/daemon.log
Socket:     /run/user/<uid>/tablet-mode.sock
"""

import subprocess
import threading
import socket
import os
import sys
import re
import time
import logging
import logging.handlers
import json
import signal
import argparse
from pathlib import Path
from datetime import datetime
import configparser
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
UID = os.getuid()
STATE_DIR = Path.home() / ".local" / "share" / "tablet-mode"
STATE_FILE = STATE_DIR / "state"
LOG_FILE = STATE_DIR / "daemon.log"
CONFIG_FILE = Path.home() / ".config" / "tablet-mode" / "config.ini"
SOCKET_PATH = Path(f"/run/user/{UID}/tablet-mode.sock")



DEFAULTS = {
    "hinge_window": "2.5",
    "keyboard_name_patterns": "keyboard,asus keyboard,at translated",
    "touchpad_name_patterns": "touchpad,synaptics,elan,trackpad",
    "exclude_patterns": "touchscreen,stylus,pen,wacom,digitizer",
}

def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(defaults=DEFAULTS)
    if path.exists():
        cfg.read(path)
    if not cfg.has_section("daemon"):
        cfg.add_section("daemon")
    return cfg

cfg = load_config(CONFIG_FILE)

# HINGE_WINDOW: Time window (seconds) within which two hinge events constitute a transition
# KEYBOARD_NAME_PATTERNS, TOUCHPAD_NAME_PATTERNS: Name substrings used to identify the built-in keyboard and touchpad.
# EXCLUDE_PATTERNS: Devices that should never be inhibited regardless of name match
HINGE_WINDOW = cfg.getfloat("daemon", "hinge_window")
KEYBOARD_NAME_PATTERNS = [s.strip() for s in cfg.get("daemon", "keyboard_name_patterns").split(",")]
TOUCHPAD_NAME_PATTERNS = [s.strip() for s in cfg.get("daemon", "touchpad_name_patterns").split(",")]
EXCLUDE_PATTERNS       = [s.strip() for s in cfg.get("daemon", "exclude_patterns").split(",")]

# ---------------------------------------------------------------------------
# WMI event pattern (matches ASUS hinge events observed via acpi_listen)
# ---------------------------------------------------------------------------
# Matches both 000000b0 and 000000ff as documented
WMI_HINGE_PATTERN = re.compile(r"^wmi PNP0C14:[0-9a-f]+ 0000[0-9a-f]{4} 00000000", re.IGNORECASE)


def parse_input_devices():
    """
    Parse /proc/bus/input/devices and return a dict mapping device names
    to their sysfs inhibited paths.
    Returns: {name: sysfs_inhibit_path}
    """
    devices = {}
    try:
        with open("/proc/bus/input/devices") as f:
            content = f.read()
    except OSError as e:
        logging.error(f"Cannot read /proc/bus/input/devices: {e}")
        return devices

    # Each device block is separated by a blank line
    for block in content.strip().split("\n\n"):
        name = None
        sysfs = None
        handlers = []
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"').lower()
            elif line.startswith("S: Sysfs="):
                sysfs = line.split("=", 1)[1].strip()
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()

        if not name or not sysfs:
            continue

        # Build the inhibited sysfs path
        # sysfs is like /devices/platform/.../input/inputN
        inhibit_path = Path(f"/sys{sysfs}/inhibited")
        if inhibit_path.exists():
            devices[name] = inhibit_path
        else:
            # Try eventN handler path fallback
            for h in handlers:
                if h.startswith("event"):
                    alt = Path(f"/sys/class/input/{h}/device/inhibited")
                    if alt.exists():
                        devices[name] = alt
                        break

    return devices


def find_target_devices(all_devices: dict, patterns: list[str]) -> list[Path]:
    """
    Return sysfs inhibited paths for devices whose names match any pattern
    in `patterns`, excluding devices matching EXCLUDE_PATTERNS.
    """
    results = []
    for name, path in all_devices.items():
        if any(excl in name for excl in EXCLUDE_PATTERNS):
            continue
        if any(pat in name for pat in patterns):
            results.append(path)
    return results


def set_inhibited(paths: list[Path], inhibit: bool):
    """Write 1 (inhibit) or 0 (enable) to each sysfs inhibited file."""
    value = b"1" if inhibit else b"0"
    for path in paths:
        try:
            path.write_bytes(value)
            logging.info(f"{'Inhibited' if inhibit else 'Enabled'} {path}")
        except OSError as e:
            logging.warning(f"Failed to write {path}: {e} — trying sudo")
            # Fallback: attempt via subprocess with pkexec/sudo
            try:
                subprocess.run(
                    ["sudo", "tee", str(path)],
                    input=value,
                    check=True,
                    capture_output=True,
                )
                logging.info(f"  (sudo succeeded for {path})")
            except subprocess.CalledProcessError as se:
                logging.error(f"  sudo also failed for {path}: {se}")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

class TabletState:
    """
    Manages persistent state.

    state file format (JSON):
        {"tablet_mode": 0|1, "override": null|0|1}
    """

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.lock = threading.Lock()
        self._tablet_mode = 0   # 0 = computer, 1 = tablet
        self._override = None   # None = auto, 0/1 = forced
        self._load()

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text())
            self._tablet_mode = int(data.get("tablet_mode", 0))
            ov = data.get("override")
            self._override = None if ov is None else int(ov)
            logging.info(f"Loaded state: tablet_mode={self._tablet_mode} override={self._override}")
        except (OSError, json.JSONDecodeError, ValueError):
            logging.info("No valid state file found; starting with defaults.")

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"tablet_mode": self._tablet_mode, "override": self._override}
        self.state_file.write_text(json.dumps(data))

    @property
    def tablet_mode(self) -> int:
        with self.lock:
            return self._tablet_mode

    @property
    def override(self):
        with self.lock:
            return self._override

    def effective_mode(self) -> int:
        """Return the active mode, honoring override if set."""
        with self.lock:
            return self._override if self._override is not None else self._tablet_mode

    def toggle_auto(self) -> int:
        """Toggle tablet_mode (auto); no-op if override is active. Returns new effective mode."""
        with self.lock:
            if self._override is not None:
                logging.info("Hinge event ignored: override is active.")
                return self._override
            self._tablet_mode = 1 - self._tablet_mode
            self._save()
            logging.info(f"Auto toggle → tablet_mode={self._tablet_mode}")
            return self._tablet_mode

    def set_override(self, mode: int):
        with self.lock:
            self._override = mode
            self._tablet_mode = mode
            self._save()
            logging.info(f"Override set → mode={mode}")

    def clear_override(self, starting_mode: int | None = None):
        with self.lock:
            self._override = None
            if starting_mode is not None:
                self._tablet_mode = starting_mode
            self._save()
            logging.info(f"Override cleared; tablet_mode={self._tablet_mode}")

    def force_mode(self, mode: int):
        """Set state without locking override (used internally for apply-only)."""
        with self.lock:
            self._tablet_mode = mode
            self._save()


# ---------------------------------------------------------------------------
# Device controller
# ---------------------------------------------------------------------------

class DeviceController:
    def __init__(self):
        self._kb_paths: list[Path] = []
        self._tp_paths: list[Path] = []
        self.refresh()

    def refresh(self):
        all_devs = parse_input_devices()
        self._kb_paths = find_target_devices(all_devs, KEYBOARD_NAME_PATTERNS)
        self._tp_paths = find_target_devices(all_devs, TOUCHPAD_NAME_PATTERNS)
        logging.info(f"Keyboard sysfs paths: {self._kb_paths}")
        logging.info(f"Touchpad sysfs paths: {self._tp_paths}")

    def apply(self, tablet_mode: int):
        inhibit = bool(tablet_mode)
        logging.info(f"Applying mode {tablet_mode}: inhibit={inhibit}")
        set_inhibited(self._kb_paths, inhibit)
        set_inhibited(self._tp_paths, inhibit)


# ---------------------------------------------------------------------------
# ACPI listener
# ---------------------------------------------------------------------------

class HingeListener:
    """
    Spawns acpi_listen as a subprocess and reads its stdout line by line.
    Detects two WMI hinge events within HINGE_WINDOW seconds → triggers toggle.
    """

    def __init__(self, on_transition):
        self._on_transition = on_transition
        self._last_event_time: float | None = None
        self._lock = threading.Lock()

    def run(self):
        logging.info("Starting acpi_listen subprocess.")
        while True:
            try:
                proc = subprocess.Popen(
                    ["acpi_listen"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                for line in proc.stdout:
                    line = line.strip()
                    if WMI_HINGE_PATTERN.match(line):
                        self._handle_hinge_event(line)
                proc.wait()
            except FileNotFoundError:
                logging.critical("acpi_listen not found. Install acpid.")
                sys.exit(1)
            except Exception as e:
                logging.error(f"acpi_listen error: {e}; restarting in 5s")
                time.sleep(5)

    def _handle_hinge_event(self, line: str):
        now = time.monotonic()
        logging.info(f"Hinge event: {line}")
        with self._lock:
            if self._last_event_time is not None:
                delta = now - self._last_event_time
                if delta <= HINGE_WINDOW:
                    logging.info(f"Transition detected (Δt={delta:.2f}s)")
                    self._last_event_time = None
                    # Run callback in a separate thread to avoid blocking acpi_listen reads
                    threading.Thread(target=self._on_transition, daemon=True).start()
                    return
            self._last_event_time = now


# ---------------------------------------------------------------------------
# Unix socket IPC server (for CLI commands)
# ---------------------------------------------------------------------------

class IPCServer:
    def __init__(self, socket_path: Path, state: TabletState, controller: DeviceController):
        self._path = socket_path
        self._state = state
        self._controller = controller

    def run(self):
        if self._path.exists():
            self._path.unlink()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self._path))
        srv.listen(5)
        logging.info(f"IPC socket listening at {self._path}")
        while True:
            try:
                conn, _ = srv.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except Exception as e:
                logging.error(f"IPC accept error: {e}")

    def _handle(self, conn: socket.socket):
        try:
            data = conn.recv(256).decode().strip()
            logging.info(f"IPC command: {data!r}")
            response = self._dispatch(data)
            conn.sendall((response + "\n").encode())
        except Exception as e:
            logging.error(f"IPC handler error: {e}")
        finally:
            conn.close()

    def _dispatch(self, cmd: str) -> str:
        parts = cmd.split()
        if not parts:
            return "ERROR: empty command"

        if parts[0] == "set" and len(parts) == 2 and parts[1] in ("0", "1"):
            mode = int(parts[1])
            self._state.set_override(mode)
            self._controller.apply(mode)
            return f"OK: override set to {mode}"

        elif parts[0] == "unlock":
            starting = None
            if "--mode" in parts:
                idx = parts.index("--mode")
                try:
                    starting = int(parts[idx + 1])
                except (IndexError, ValueError):
                    return "ERROR: --mode requires 0 or 1"
            self._state.clear_override(starting)
            self._controller.apply(self._state.effective_mode())
            return f"OK: override cleared; mode={self._state.effective_mode()}"

        elif parts[0] == "status":
            mode = self._state.effective_mode()
            ov = self._state.override
            auto = self._state.tablet_mode
            return (
                f"tablet_mode={mode} "
                f"auto_state={auto} "
                f"override={ov if ov is not None else 'None'}"
            )

        elif parts[0] == "refresh-devices":
            self._controller.refresh()
            self._controller.apply(self._state.effective_mode())
            return "OK: device list refreshed"

        else:
            return f"ERROR: unknown command: {cmd!r}"


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_file: Path):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Rotating file handler: rotate every 24h, keep 2 backups
    fh = logging.handlers.TimedRotatingFileHandler(
        str(log_file), when="midnight", interval=1, backupCount=1
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

def run_daemon():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(LOG_FILE)
    logging.info("=== tablet-mode-daemon starting ===")

    state = TabletState(STATE_FILE)
    controller = DeviceController()

    # Apply persisted state on startup
    controller.apply(state.effective_mode())

    def on_transition():
        new_mode = state.toggle_auto()
        controller.apply(new_mode)

    listener = HingeListener(on_transition)
    ipc = IPCServer(SOCKET_PATH, state, controller)

    # Graceful shutdown
    def _shutdown(sig, frame):
        logging.info(f"Received signal {sig}; shutting down.")
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # IPC server in background thread
    ipc_thread = threading.Thread(target=ipc.run, daemon=True)
    ipc_thread.start()

    # ACPI listener blocks in main thread
    listener.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tablet mode daemon for ASUS 2-in-1 on Linux")
    parser.add_argument("--hinge-window", type=float, default=HINGE_WINDOW,
                        help=f"Seconds between two hinge events to count as a transition (default: {HINGE_WINDOW})")
    args = parser.parse_args()
    if args.hinge_window is not None:
        HINGE_WINDOW = args.hinge_window
    run_daemon()