#!/usr/bin/env python
"""
All-in-one Python script that:
1. Auto-creates a virtual environment (if needed) and installs required packages (pyserial, Flask, Flask-SocketIO),
   then re-launches itself from the venv.
2. Loads configuration from config.json if available.
3. For the serial device and baudrate, attempts to use the saved values; if the connection fails,
   auto-tries /dev/ttyUSB0 and /dev/ttyUSB1 at 115200; if those fail, prompts for new values.
4. For the network host, port, and route, attempts to use saved values; if any fail,
   prompts for new ones.
5. Saves any new valid configuration automatically.
6. Sets up a Flask + WebSocket server with rolling key authentication:
      /auth endpoint for password  session key exchange
      HTTP POSTs at your chosen route (requires valid session key)
      WebSocket at /ws (requires valid session key)
   Commands can be "home" or any subset of fields X,Y,Z,H,S,A,R,P.
7. Maintains a running `current_state` so partial updates merge into a full command string.
8. Session keys expire after a configurable timeout (default 300 seconds).
"""

import os
import sys
import subprocess
import json
import datetime
import re
import socket
import secrets
import time
import threading
import platform
from threading import Lock

# Import terminal UI
try:
    from terminal_ui import CategorySpec, ConfigSpec, SettingSpec, TerminalUI
    UI_AVAILABLE = True
except ImportError:
    UI_AVAILABLE = False
    CategorySpec = None
    ConfigSpec = None
    SettingSpec = None
    TerminalUI = None
    print("Warning: terminal_ui.py not found, running without UI")

# Global UI instance
ui = None

# --- Virtual Environment Setup ---
ADAPTER_VENV_DIR_NAME = "adapter_venv"
ADAPTER_CLOUDFLARED_BASENAME = "adapter_cloudflared"


def ensure_venv():
    script_dir = os.path.abspath(os.path.dirname(__file__))
    venv_dir = os.path.join(script_dir, ADAPTER_VENV_DIR_NAME)
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(venv_dir)):
        return

    # Determine paths based on OS
    if os.name == 'nt':
        pip_path = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_path = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_path = os.path.join(venv_dir, "bin", "pip")
        python_path = os.path.join(venv_dir, "bin", "python")

    # Create venv if it doesn't exist
    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment in '{ADAPTER_VENV_DIR_NAME}' directory...")
        import venv
        venv.create(venv_dir, with_pip=True)
        print("Installing required packages: pyserial, Flask, Flask-SocketIO, Flask-CORS...")
        subprocess.check_call([pip_path, "install", "pyserial", "Flask", "Flask-SocketIO", "Flask-CORS"])
    else:
        # Venv exists - check if packages are installed
        try:
            result = subprocess.run(
                [python_path, "-c", "import serial, flask, flask_socketio, flask_cors"],
                capture_output=True,
                timeout=5
            )
            if result.returncode != 0:
                print("Installing missing packages...")
                subprocess.check_call([pip_path, "install", "pyserial", "Flask", "Flask-SocketIO", "Flask-CORS"])
        except:
            print("Installing required packages: pyserial, Flask, Flask-SocketIO, Flask-CORS...")
            subprocess.check_call([pip_path, "install", "pyserial", "Flask", "Flask-SocketIO", "Flask-CORS"])

    print("Re-launching script from the virtual environment...")
    os.execv(python_path, [python_path] + sys.argv)

ensure_venv()

try:
    import serial
except ImportError:
    print("pyserial is not installed. Exiting.")
    sys.exit(1)

try:
    from flask import Flask, request, jsonify
    from flask_socketio import SocketIO, send as ws_send
    from flask_cors import CORS
except ImportError:
    print("Flask, Flask-SocketIO, or Flask-CORS is not installed. Exiting.")
    sys.exit(1)

# --- Allowed Ranges for Fields ---
allowed_ranges = {
    "X": (-700, 700, int),
    "Y": (-700, 700, int),
    "Z": (-700, 700, int),
    "H": (0, 70, int),
    "S": (0, 10, float),
    "A": (0, 10, float),
    "R": (-700, 700, int),
    "P": (-700, 700, int)
}

# --- Default Current State ---
current_state = {k: (1.0 if k in ("S","A") else 0) for k in allowed_ranges}

# --- Session Management ---
sessions = {}  # {session_key: {"created_at": timestamp, "last_used": timestamp}}
sessions_lock = Lock()
serial_io_lock = Lock()
SESSION_TIMEOUT = 300  # seconds (5 minutes)
DEFAULT_PASSWORD = "neck2025"  # Should be changed via config

# --- Config Defaults ---
CONFIG_PATH = "config.json"
DEFAULT_BAUDRATE = 115200
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 5001
DEFAULT_LISTEN_ROUTE = "/send_command"
DEFAULT_ENABLE_TUNNEL = True
DEFAULT_AUTO_INSTALL_CLOUDFLARED = True
AUTO_SERIAL_CANDIDATES = ("/dev/ttyUSB0", "/dev/ttyUSB1")

# --- Cloudflare Tunnel ---
tunnel_url = None
tunnel_url_lock = Lock()
tunnel_process = None
tunnel_last_error = ""
tunnel_desired = False
tunnel_restart_lock = Lock()

# --- Runtime Control ---
SCRIPT_WATCH_INTERVAL_SECONDS = 1.0
DEFAULT_TUNNEL_RESTART_DELAY_SECONDS = 3.0
service_running = threading.Event()


def stop_cloudflared_tunnel():
    """Stop the cloudflared subprocess if it is running."""
    global tunnel_process, tunnel_last_error, tunnel_url, tunnel_desired
    tunnel_desired = False
    process = tunnel_process
    if not process:
        with tunnel_url_lock:
            tunnel_url = None
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        pass
    finally:
        tunnel_process = None
        with tunnel_url_lock:
            tunnel_url = None
        tunnel_last_error = "Tunnel stopped"


def restart_current_process(reason="Restarting process"):
    """Restart this script using the current Python interpreter."""
    log(reason)
    stop_cloudflared_tunnel()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# --- Utility Logging Function ---
def log(message):
    if ui and UI_AVAILABLE:
        ui.log(message)
    else:
        print(f"[{datetime.datetime.now()}] {message}")

# --- Command Validation ---
def _normalize_command_token(cmd):
    raw = str(cmd).strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _normalized_home_command(cmd):
    normalized = _normalize_command_token(cmd)
    if not normalized:
        return None

    # Keep "home" mapped to the brute sequence for backward compatibility.
    brute_aliases = {
        "home",
        "home_brute",
        "homebrute",
        "brute_home",
        "brutehome",
    }
    soft_aliases = {
        "home_soft",
        "homesoft",
        "soft_home",
        "softhome",
    }

    if normalized in brute_aliases:
        return "home_brute"
    if normalized in soft_aliases:
        return "home_soft"
    return None


def validate_command(cmd):
    cmd = str(cmd).strip()
    if not cmd:
        return False
    if _normalized_home_command(cmd):
        return True
    seen = set()
    for token in cmd.split(","):
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", token.strip())
        if not m or m.group(1) in seen:
            return False
        seen.add(m.group(1))
        low, high, cast = allowed_ranges[m.group(1)]
        try:
            val = cast(m.group(2))
        except:
            return False
        if not (low <= val <= high):
            return False
    return True

# --- Merge Partial into State ---
def _reset_state_to_home_defaults():
    for k in current_state:
        current_state[k] = 1.0 if k in ("S", "A") else 0


def merge_into_state(cmd):
    if _normalized_home_command(cmd):
        _reset_state_to_home_defaults()
        return
    for token in cmd.split(","):
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", token.strip())
        if m:
            k, raw = m.group(1), m.group(2)
            low, high, cast = allowed_ranges[k]
            v = cast(raw)
            current_state[k] = v

# --- Assemble Full Command ---
def assemble_full_command():
    return ",".join(f"{k}{current_state[k]}" for k in ["X","Y","Z","H","S","A","R","P"])

# --- Session Management Functions ---
def create_session():
    """Generate a new session key and register it."""
    session_key = secrets.token_urlsafe(32)
    now = time.time()
    with sessions_lock:
        sessions[session_key] = {"created_at": now, "last_used": now}
    return session_key

def validate_session(session_key):
    """Check if a session key is valid and not expired."""
    if not session_key:
        return False
    with sessions_lock:
        if session_key not in sessions:
            return False
        session = sessions[session_key]
        now = time.time()
        if now - session["last_used"] > SESSION_TIMEOUT:
            del sessions[session_key]
            return False
        session["last_used"] = now
        return True

def cleanup_expired_sessions():
    """Remove all expired sessions."""
    now = time.time()
    with sessions_lock:
        expired = [k for k, v in sessions.items() if now - v["last_used"] > SESSION_TIMEOUT]
        for k in expired:
            del sessions[k]
        if expired:
            log(f"Cleaned up {len(expired)} expired session(s)")

# --- Port Availability Check ---
def is_port_available(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

# --- Cloudflared Installation ---
def get_cloudflared_path():
    """Get the path to cloudflared binary."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.name == 'nt':
        return os.path.join(script_dir, f"{ADAPTER_CLOUDFLARED_BASENAME}.exe")
    else:
        return os.path.join(script_dir, ADAPTER_CLOUDFLARED_BASENAME)

def is_cloudflared_installed():
    """Check if cloudflared is installed."""
    cloudflared_path = get_cloudflared_path()
    if os.path.exists(cloudflared_path):
        return True
    # Check if it's in PATH
    try:
        subprocess.run(["cloudflared", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def install_cloudflared():
    """Download and install cloudflared."""
    log("Installing cloudflared...")
    cloudflared_path = get_cloudflared_path()

    system = platform.system().lower()
    machine = platform.machine().lower()

    # Determine download URL based on platform
    if system == "windows":
        if "amd64" in machine or "x86_64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-386.exe"
    elif system == "linux":
        if "aarch64" in machine or "arm64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
        elif "arm" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    elif system == "darwin":
        if "arm" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    else:
        log(f"[ERROR] Unsupported platform: {system} {machine}")
        return False

    try:
        import urllib.request
        log(f"Downloading cloudflared from {url}...")
        urllib.request.urlretrieve(url, cloudflared_path)

        # Make executable on Unix-like systems
        if os.name != 'nt':
            os.chmod(cloudflared_path, 0o755)

        log("[OK] Cloudflared installed successfully")
        return True
    except Exception as e:
        log(f"[ERROR] Failed to install cloudflared: {e}")
        return False

def start_cloudflared_tunnel(local_port, command_route="/send_command"):
    """Start cloudflared tunnel in background and capture the URL."""
    global tunnel_url, tunnel_process, tunnel_last_error, tunnel_desired
    with tunnel_restart_lock:
        if tunnel_process is not None and tunnel_process.poll() is None:
            return True
        tunnel_desired = True

    cloudflared_path = get_cloudflared_path()
    if not os.path.exists(cloudflared_path):
        # Try using cloudflared from PATH
        cloudflared_path = "cloudflared"

    if not command_route.startswith("/"):
        command_route = f"/{command_route}"

    with tunnel_url_lock:
        tunnel_url = None
    tunnel_last_error = ""

    url = f"http://localhost:{local_port}"

    try:
        log("[START] Starting Cloudflare Tunnel...")
        process = subprocess.Popen(
            [cloudflared_path, "tunnel", "--protocol", "http2", "--url", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        tunnel_process = process

        # Start a thread to monitor output and capture the URL
        def monitor_tunnel():
            global tunnel_url, tunnel_process, tunnel_last_error
            found_url = False
            captured_url = ""
            if process.stdout is None:
                return

            for raw_line in iter(process.stdout.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue
                lowered = line.lower()
                if any(token in lowered for token in ("error", "failed", "unable", "panic")):
                    log(f"[CLOUDFLARED] {line}")

                if "trycloudflare.com" in line:
                    url_match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                    if not url_match:
                        url_match = re.search(r"https://[^\s]+trycloudflare\.com[^\s]*", line)
                    if url_match:
                        with tunnel_url_lock:
                            if tunnel_url is None:
                                captured_url = url_match.group(0)
                                tunnel_url = captured_url
                                found_url = True
                                tunnel_last_error = ""
                                log("")
                                log(f"{'='*60}")
                                log(f"[TUNNEL] Cloudflare Tunnel URL: {tunnel_url}")
                                log(f"{'='*60}")
                                log("")
                                log(f"Use this URL in app.py for remote access:")
                                log(f"  HTTP URL: {tunnel_url}{command_route}")
                                log(f"  WS URL:   {tunnel_url.replace('https://', 'wss://')}/ws")
                                log("")

            return_code = process.poll()
            with tunnel_restart_lock:
                if tunnel_process is process:
                    tunnel_process = None

            if captured_url:
                with tunnel_url_lock:
                    if tunnel_url == captured_url:
                        tunnel_url = None

            if return_code is not None:
                if found_url:
                    tunnel_last_error = f"cloudflared exited (code {return_code}); tunnel URL expired"
                    log(f"[WARN] {tunnel_last_error}")
                else:
                    tunnel_last_error = f"cloudflared exited before URL (code {return_code})"
                    log(f"[ERROR] {tunnel_last_error}")

                if tunnel_desired and service_running.is_set():
                    delay = DEFAULT_TUNNEL_RESTART_DELAY_SECONDS
                    log(f"[WARN] Restarting cloudflared in {delay:.1f}s...")
                    time.sleep(delay)
                    if tunnel_desired and service_running.is_set():
                        start_cloudflared_tunnel(local_port, command_route)

        thread = threading.Thread(target=monitor_tunnel, daemon=True)
        thread.start()

        return True
    except Exception as e:
        log(f"[ERROR] Failed to start cloudflared tunnel: {e}")
        return False

# --- Config Helpers ---
_MISSING = object()


def _get_nested(data, path, default=_MISSING):
    current = data
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def _set_nested(data, path, value):
    current = data
    keys = path.split(".")
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def _has_nested(data, path):
    return _get_nested(data, path, _MISSING) is not _MISSING


def _read_config_value(config, path, default=_MISSING, legacy_keys=()):
    value = _get_nested(config, path, _MISSING)
    if value is not _MISSING:
        return value
    for key in legacy_keys:
        if key in config:
            return config[key]
    return default


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return default


def _as_int(value, default, minimum=None, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    if maximum is not None and parsed > maximum:
        return default
    return parsed


def _normalize_route(value):
    route = str(value).strip()
    if not route:
        return DEFAULT_LISTEN_ROUTE
    if not route.startswith("/"):
        route = f"/{route}"
    return route


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            loaded = json.load(fp)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, indent=4)
        print(f"Configuration saved to {CONFIG_PATH}.")
    except OSError as exc:
        print(f"Failed to save configuration: {exc}")


def _load_adapter_settings(config):
    """Resolve adapter settings and promote them into adapter.* nested paths."""
    changed = False

    def promote(path, value):
        nonlocal changed
        current = _get_nested(config, path, _MISSING)
        if current is _MISSING or current != value:
            _set_nested(config, path, value)
            changed = True

    serial_device_raw = _read_config_value(
        config, "adapter.serial.device", _MISSING, legacy_keys=("serial_device",)
    )
    serial_device = None
    if serial_device_raw is not _MISSING:
        serial_device = str(serial_device_raw).strip() or None
        if serial_device:
            promote("adapter.serial.device", serial_device)

    baudrate = _as_int(
        _read_config_value(
            config, "adapter.serial.baudrate", DEFAULT_BAUDRATE, legacy_keys=("baudrate",)
        ),
        DEFAULT_BAUDRATE,
        minimum=300,
        maximum=2_000_000,
    )
    promote("adapter.serial.baudrate", baudrate)

    listen_host = str(
        _read_config_value(
            config, "adapter.network.listen_host", DEFAULT_LISTEN_HOST, legacy_keys=("listen_host",)
        )
    ).strip() or DEFAULT_LISTEN_HOST
    promote("adapter.network.listen_host", listen_host)

    listen_port = _as_int(
        _read_config_value(
            config, "adapter.network.listen_port", DEFAULT_LISTEN_PORT, legacy_keys=("listen_port",)
        ),
        DEFAULT_LISTEN_PORT,
        minimum=1,
        maximum=65535,
    )
    promote("adapter.network.listen_port", listen_port)

    listen_route = _normalize_route(
        _read_config_value(
            config, "adapter.network.listen_route", DEFAULT_LISTEN_ROUTE, legacy_keys=("listen_route",)
        )
    )
    promote("adapter.network.listen_route", listen_route)

    password = str(
        _read_config_value(
            config, "adapter.security.password", DEFAULT_PASSWORD, legacy_keys=("password",)
        )
    )
    password = password if password else DEFAULT_PASSWORD
    promote("adapter.security.password", password)

    session_timeout = _as_int(
        _read_config_value(
            config,
            "adapter.security.session_timeout",
            SESSION_TIMEOUT,
            legacy_keys=("session_timeout",),
        ),
        SESSION_TIMEOUT,
        minimum=30,
        maximum=86400,
    )
    promote("adapter.security.session_timeout", session_timeout)

    enable_tunnel = _as_bool(
        _read_config_value(
            config, "adapter.tunnel.enable", DEFAULT_ENABLE_TUNNEL, legacy_keys=("enable_tunnel",)
        ),
        default=DEFAULT_ENABLE_TUNNEL,
    )
    promote("adapter.tunnel.enable", enable_tunnel)

    auto_install_cloudflared = _as_bool(
        _read_config_value(
            config,
            "adapter.tunnel.auto_install_cloudflared",
            DEFAULT_AUTO_INSTALL_CLOUDFLARED,
            legacy_keys=("auto_install_cloudflared",),
        ),
        default=DEFAULT_AUTO_INSTALL_CLOUDFLARED,
    )
    promote("adapter.tunnel.auto_install_cloudflared", auto_install_cloudflared)

    return {
        "serial_device": serial_device,
        "baudrate": baudrate,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "listen_route": listen_route,
        "password": password,
        "session_timeout": session_timeout,
        "enable_tunnel": enable_tunnel,
        "auto_install_cloudflared": auto_install_cloudflared,
    }, changed


def _build_adapter_config_spec():
    if not UI_AVAILABLE:
        return None
    return ConfigSpec(
        label="Neck Adapter",
        categories=(
            CategorySpec(
                id="serial",
                label="Serial",
                settings=(
                    SettingSpec(
                        id="serial_device",
                        label="Serial Device",
                        path="adapter.serial.device",
                        value_type="str",
                        default="",
                        description="Serial device path, e.g. COM3 or /dev/ttyUSB0.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="baudrate",
                        label="Baudrate",
                        path="adapter.serial.baudrate",
                        value_type="int",
                        default=DEFAULT_BAUDRATE,
                        min_value=300,
                        max_value=2000000,
                        description="UART baudrate for the neck controller.",
                        restart_required=True,
                    ),
                ),
            ),
            CategorySpec(
                id="network",
                label="Network",
                settings=(
                    SettingSpec(
                        id="listen_host",
                        label="Listen Host",
                        path="adapter.network.listen_host",
                        value_type="str",
                        default=DEFAULT_LISTEN_HOST,
                        description="Bind host for Flask/Socket.IO server.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="listen_port",
                        label="Listen Port",
                        path="adapter.network.listen_port",
                        value_type="int",
                        default=DEFAULT_LISTEN_PORT,
                        min_value=1,
                        max_value=65535,
                        description="Bind port for HTTP and WebSocket traffic.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="listen_route",
                        label="Command Route",
                        path="adapter.network.listen_route",
                        value_type="str",
                        default=DEFAULT_LISTEN_ROUTE,
                        description="HTTP POST route for commands, e.g. /send_command.",
                        restart_required=True,
                    ),
                ),
            ),
            CategorySpec(
                id="security",
                label="Security",
                settings=(
                    SettingSpec(
                        id="password",
                        label="Password",
                        path="adapter.security.password",
                        value_type="secret",
                        default=DEFAULT_PASSWORD,
                        sensitive=True,
                        description="Password used by /auth to mint session keys.",
                    ),
                    SettingSpec(
                        id="session_timeout",
                        label="Session Timeout (s)",
                        path="adapter.security.session_timeout",
                        value_type="int",
                        default=SESSION_TIMEOUT,
                        min_value=30,
                        max_value=86400,
                        description="Idle timeout before session keys expire.",
                    ),
                ),
            ),
            CategorySpec(
                id="tunnel",
                label="Tunnel",
                settings=(
                    SettingSpec(
                        id="enable_tunnel",
                        label="Enable Tunnel",
                        path="adapter.tunnel.enable",
                        value_type="bool",
                        default=DEFAULT_ENABLE_TUNNEL,
                        description="Enable Cloudflare Tunnel for remote access.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="auto_install_cloudflared",
                        label="Auto-install Cloudflared",
                        path="adapter.tunnel.auto_install_cloudflared",
                        value_type="bool",
                        default=DEFAULT_AUTO_INSTALL_CLOUDFLARED,
                        description="Install cloudflared automatically when missing.",
                        restart_required=True,
                    ),
                ),
            ),
        ),
    )

# --- Process a Single Command ---
def process_command(cmd, ser):
    log(f"Received command: {cmd}")
    if not validate_command(cmd):
        log(f"Rejected invalid command: {repr(cmd)}")
        return {"status":"error","message":"Invalid command"}
    merge_into_state(cmd)

    home_cmd = _normalized_home_command(cmd)
    if home_cmd:
        outbound = home_cmd.upper()
        try:
            with serial_io_lock:
                if ser is None or not getattr(ser, "is_open", True):
                    return {"status":"error","message":"Serial port is not connected"}
                ser.write((outbound + "\n").encode("utf-8"))
            log(f"Sent command: {outbound}")
            return {"status":"success","command":outbound}
        except Exception as e:
            log(f"Serial write error: {e}")
            return {"status":"error","message":str(e)}

    full = assemble_full_command()
    try:
        with serial_io_lock:
            if ser is None or not getattr(ser, "is_open", True):
                return {"status":"error","message":"Serial port is not connected"}
            ser.write((full + "\n").encode("utf-8"))
        log(f"Sent command: {full}")
        return {"status":"success","command":full}
    except Exception as e:
        log(f"Serial write error: {e}")
        return {"status":"error","message":str(e)}

# --- Main Application ---
def main():
    global ui
    global SESSION_TIMEOUT

    config = load_config()
    adapter_settings, config_changed = _load_adapter_settings(config)
    SESSION_TIMEOUT = adapter_settings["session_timeout"]

    if UI_AVAILABLE:
        ui = TerminalUI(
            "Neck Adapter",
            config_spec=_build_adapter_config_spec(),
            config_path=CONFIG_PATH,
        )
        ui.log("Starting Neck Adapter...")

    runtime_security = {
        "password": adapter_settings["password"],
    }

    def _persist(path, value):
        nonlocal config_changed
        current = _get_nested(config, path, _MISSING)
        if current is _MISSING or current != value:
            _set_nested(config, path, value)
            config_changed = True

    def _apply_runtime_config(saved_config):
        global SESSION_TIMEOUT
        password = str(
            _read_config_value(
                saved_config,
                "adapter.security.password",
                runtime_security["password"],
                legacy_keys=("password",),
            )
        ).strip() or DEFAULT_PASSWORD
        timeout = _as_int(
            _read_config_value(
                saved_config,
                "adapter.security.session_timeout",
                SESSION_TIMEOUT,
                legacy_keys=("session_timeout",),
            ),
            SESSION_TIMEOUT,
            minimum=30,
            maximum=86400,
        )
        runtime_security["password"] = password
        SESSION_TIMEOUT = timeout
        if ui:
            ui.update_metric("Session Timeout (s)", str(SESSION_TIMEOUT))
            ui.log("Applied live security updates from config save")

    if ui:
        ui.on_save(_apply_runtime_config)

    # --- Serial Connection Setup ---
    ser = None
    serial_device = adapter_settings["serial_device"]
    baudrate = adapter_settings["baudrate"]

    # 1) Try saved config
    if serial_device:
        try:
            ser = serial.Serial(serial_device, int(baudrate), timeout=1)
            print("Serial connection OK from saved config")
        except Exception as exc:
            print(f"Saved config failed: {exc}")

    # 2) Auto-try default candidates at default baudrate
    if ser is None:
        for dev in AUTO_SERIAL_CANDIDATES:
            try:
                ser = serial.Serial(dev, DEFAULT_BAUDRATE, timeout=1)
                print(f"Auto-connected: {dev}@{DEFAULT_BAUDRATE}")
                serial_device = dev
                baudrate = DEFAULT_BAUDRATE
                _persist("adapter.serial.device", serial_device)
                _persist("adapter.serial.baudrate", baudrate)
                break
            except Exception:
                continue

    # 3) Interactive fallback
    while ser is None:
        device = input("Serial device (e.g. /dev/ttyUSB0 or COM3): ").strip()
        baud_in = input(f"Baudrate (default {DEFAULT_BAUDRATE}): ").strip() or str(DEFAULT_BAUDRATE)
        try:
            baud = int(baud_in)
        except ValueError:
            print("Invalid baudrate.\n")
            continue
        try:
            ser = serial.Serial(device, baud, timeout=1)
            print("Serial connection successful!")
            serial_device = device
            baudrate = baud
            _persist("adapter.serial.device", serial_device)
            _persist("adapter.serial.baudrate", baudrate)
        except Exception as exc:
            print(f"Serial connect error: {exc}\n")

    def reset_serial_connection(trigger_home=True, home_command="HOME_BRUTE"):
        """Disconnect and reconnect serial port, optionally issuing a home command."""
        nonlocal ser, serial_device, baudrate

        normalized_home = _normalized_home_command(str(home_command)) or "home_brute"
        outbound_home = normalized_home.upper()

        if not serial_device:
            return False, "No serial device configured", None

        with serial_io_lock:
            if ser is not None:
                try:
                    if getattr(ser, "is_open", False):
                        ser.close()
                        log(f"Serial disconnected: {serial_device}@{baudrate}")
                except Exception as close_exc:
                    log(f"Serial close warning: {close_exc}")

            # Give USB CDC device a moment to drop before reconnect attempts.
            time.sleep(0.35)

            last_exc = None
            reconnect_attempts = 8
            for attempt in range(1, reconnect_attempts + 1):
                try:
                    ser = serial.Serial(serial_device, int(baudrate), timeout=1)
                    log(f"Serial reconnected: {serial_device}@{baudrate} (attempt {attempt})")
                    break
                except Exception as open_exc:
                    last_exc = open_exc
                    if attempt < reconnect_attempts:
                        time.sleep(0.4)
            else:
                return False, f"Reconnect failed: {last_exc}", None

            # Give firmware a moment after reconnect before optional home command.
            time.sleep(0.2)

            if trigger_home:
                try:
                    ser.write((outbound_home + "\n").encode("utf-8"))
                    _reset_state_to_home_defaults()
                    log(f"Sent command after serial reset: {outbound_home}")
                except Exception as home_exc:
                    return False, f"Reconnect succeeded but home send failed: {home_exc}", outbound_home

        return True, "Serial port reset complete", outbound_home if trigger_home else None

    # --- Network Host/Port/Route ---
    listen_host = adapter_settings["listen_host"]
    listen_route = _normalize_route(adapter_settings["listen_route"])
    listen_port = None
    configured_port = adapter_settings["listen_port"]

    if is_port_available(configured_port, listen_host):
        listen_port = configured_port
        print(f"Using saved port: {configured_port}")
    else:
        print(f"Saved port {configured_port} unavailable on {listen_host}.")

    while listen_port is None:
        p_in = input(f"Listen port (1-65535) [{configured_port}]: ").strip()
        if not p_in:
            p = configured_port
        else:
            try:
                p = int(p_in)
            except ValueError:
                print("Invalid port.\n")
                continue
        if is_port_available(p, listen_host):
            listen_port = p
        else:
            print("Port not available.\n")

    _persist("adapter.network.listen_host", listen_host)
    _persist("adapter.network.listen_port", listen_port)
    _persist("adapter.network.listen_route", listen_route)
    _persist("adapter.security.password", runtime_security["password"])
    _persist("adapter.security.session_timeout", SESSION_TIMEOUT)
    _persist("adapter.tunnel.enable", adapter_settings["enable_tunnel"])
    _persist("adapter.tunnel.auto_install_cloudflared", adapter_settings["auto_install_cloudflared"])

    if config_changed:
        save_config(config)

    # --- Flask + WebSocket ---
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secrets.token_hex(16)

    # Enable CORS for all routes to allow cross-origin requests from frontend
    CORS(app, resources={r"/*": {"origins": "*"}})

    socketio = SocketIO(app, cors_allowed_origins="*")

    @app.route("/auth", methods=["POST"])
    def authenticate():
        """Authenticate with password and receive a session key."""
        data = request.get_json() or {}
        provided_password = str(data.get("password", ""))
        if provided_password == runtime_security["password"]:
            session_key = create_session()
            log(f"New session created: {session_key[:8]}...")
            return jsonify(
                {
                    "status": "success",
                    "session_key": session_key,
                    "timeout": SESSION_TIMEOUT,
                }
            )
        log("Authentication failed: invalid password")
        return jsonify({"status": "error", "message": "Invalid password"}), 401

    def _build_adapter_discovery_payload():
        process_running = tunnel_process is not None and tunnel_process.poll() is None
        with tunnel_url_lock:
            current_tunnel = tunnel_url if process_running else ""
            stale_tunnel = tunnel_url if (tunnel_url and not process_running) else ""
            current_error = tunnel_last_error

        local_base = f"http://127.0.0.1:{listen_port}"
        local_http = f"{local_base}{listen_route}"
        local_ws = f"ws://127.0.0.1:{listen_port}/ws"
        tunnel_http = f"{current_tunnel}{listen_route}" if current_tunnel else ""
        tunnel_ws = f"{current_tunnel.replace('https://', 'wss://')}/ws" if current_tunnel else ""

        tunnel_state = "active" if (process_running and current_tunnel) else ("starting" if process_running else "inactive")
        if stale_tunnel and not process_running:
            tunnel_state = "stale"
        if current_error and not process_running and not current_tunnel and not stale_tunnel:
            tunnel_state = "error"

        return {
            "service": "adapter",
            "running": process_running,
            "tunnel_url": current_tunnel,
            "http_endpoint": tunnel_http,
            "ws_endpoint": tunnel_ws,
            "local_base_url": local_base,
            "local_http_endpoint": local_http,
            "local_ws_endpoint": local_ws,
            "stale_tunnel_url": stale_tunnel,
            "error": current_error,
            "local": {
                "base_url": local_base,
                "listen_host": listen_host,
                "listen_port": int(listen_port),
                "command_route": listen_route,
                "auth_route": "/auth",
                "ws_path": "/ws",
                "http_endpoint": local_http,
                "ws_endpoint": local_ws,
            },
            "tunnel": {
                "state": tunnel_state,
                "tunnel_url": current_tunnel,
                "http_endpoint": tunnel_http,
                "ws_endpoint": tunnel_ws,
                "stale_tunnel_url": stale_tunnel,
                "error": current_error,
            },
            "security": {
                "require_auth": True,
                "password_required": True,
                "session_timeout": int(SESSION_TIMEOUT),
            },
        }

    @app.route("/tunnel_info", methods=["GET"])
    def get_tunnel_info():
        """Get the Cloudflare Tunnel URL if available."""
        payload = _build_adapter_discovery_payload()
        current_tunnel = str(payload.get("tunnel_url") or "").strip()
        stale_tunnel = str(payload.get("stale_tunnel_url") or "").strip()
        current_error = str(payload.get("error") or "").strip()

        if current_tunnel:
            payload["status"] = "success"
            payload["message"] = "Tunnel URL available"
        elif stale_tunnel:
            payload["status"] = "error"
            payload["error"] = current_error or "Tunnel URL expired"
            payload["message"] = "Tunnel process is not running; URL is stale"
        elif current_error:
            payload["status"] = "error"
            payload["message"] = "Tunnel failed to start"
        else:
            payload["status"] = "pending"
            payload["message"] = "Tunnel URL not yet available"

        return jsonify(payload)

    @app.route("/router_info", methods=["GET"])
    def router_info():
        """Discovery payload for the NKN router sidecar."""
        payload = _build_adapter_discovery_payload()
        payload["status"] = "success"
        return jsonify(payload)

    @app.route("/serial_reset", methods=["POST"])
    def http_serial_reset():
        """Disconnect/reconnect serial and optionally send a post-reset home command."""
        data = request.get_json() or {}
        session_key = data.get("session_key", "")

        if not validate_session(session_key):
            return jsonify({"status": "error", "message": "Invalid or expired session"}), 401

        trigger_home = _as_bool(data.get("trigger_home", True), default=True)
        home_command = str(data.get("home_command", "HOME_BRUTE")).strip() or "HOME_BRUTE"

        ok, message, home_sent = reset_serial_connection(
            trigger_home=trigger_home,
            home_command=home_command,
        )
        if not ok:
            return jsonify({"status": "error", "message": message}), 500

        response = {
            "status": "success",
            "message": message,
            "serial_device": serial_device,
            "baudrate": int(baudrate),
            "home_sent": home_sent,
        }
        return jsonify(response)

    @app.route(listen_route, methods=["POST"])
    def http_receive():
        """Handle HTTP command with session key validation."""
        data = request.get_json() or {}
        session_key = data.get("session_key", "")

        if not validate_session(session_key):
            return jsonify({"status": "error", "message": "Invalid or expired session"}), 401

        cmd = data.get("command", "").strip()
        return jsonify(process_command(cmd, ser))

    @socketio.on("connect")
    def ws_connect():
        """Handle WebSocket connection."""
        log("WebSocket client connected")
        cleanup_expired_sessions()

    @socketio.on("disconnect")
    def ws_disconnect():
        """Handle WebSocket disconnection."""
        log("WebSocket client disconnected")

    @socketio.on("authenticate")
    def ws_authenticate(data):
        """Authenticate WebSocket connection."""
        if isinstance(data, str):
            data = json.loads(data)
        session_key = data.get("session_key", "")
        if validate_session(session_key):
            ws_send(json.dumps({"status": "authenticated"}))
            log(f"WebSocket authenticated with session: {session_key[:8]}...")
        else:
            ws_send(json.dumps({"status": "error", "message": "Invalid or expired session"}))

    @socketio.on("message")
    def ws_receive(data):
        """Handle WebSocket command with session key validation."""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {"command": data, "session_key": ""}

        session_key = data.get("session_key", "")
        if not validate_session(session_key):
            ws_send(json.dumps({"status": "error", "message": "Invalid or expired session"}))
            return

        cmd = data.get("command", "").strip()
        result = process_command(cmd, ser)
        ws_send(json.dumps(result))

    service_running.set()

    # --- Cloudflare Tunnel Setup ---
    enable_tunnel = adapter_settings["enable_tunnel"]
    if enable_tunnel:
        if not is_cloudflared_installed():
            if adapter_settings["auto_install_cloudflared"]:
                log("Cloudflared not found, attempting to install...")
                if not install_cloudflared():
                    log("Failed to install cloudflared. Remote access will not be available.")
                    log("You can still use the adapter locally.")
                    enable_tunnel = False
            else:
                log("Cloudflared not found and auto-install is disabled. Tunnel is disabled.")
                enable_tunnel = False

        if enable_tunnel:
            # Start tunnel in background thread (it will capture and display the URL)
            def start_tunnel_delayed():
                time.sleep(2)
                start_cloudflared_tunnel(listen_port, listen_route)

            tunnel_thread = threading.Thread(target=start_tunnel_delayed, daemon=True)
            tunnel_thread.start()

    # --- Startup Log & Run ---
    hint = f"http://{listen_host}:{listen_port}{listen_route}"
    if listen_host == "0.0.0.0":
        try:
            lan = socket.gethostbyname(socket.gethostname())
            lan_hint = f"http://{lan}:{listen_port}{listen_route}"
            hint += f"  (LAN: {lan_hint})"
        except Exception:
            pass

    log(f"Starting server on {hint}")
    if enable_tunnel:
        log("Cloudflare Tunnel will be available shortly...")
        log("Remote URL will be displayed once tunnel is established.")

    # Update initial metrics
    if ui:
        ui.update_metric("Serial Port", serial_device or "N/A")
        ui.update_metric("Baudrate", str(baudrate))
        ui.update_metric("HTTP Endpoint", hint)
        ui.update_metric("WebSocket", f"ws://{listen_host}:{listen_port}/ws")
        ui.update_metric("Session Timeout (s)", str(SESSION_TIMEOUT))
        ui.update_metric("Sessions", "0")
        ui.update_metric("Commands", "0")
        ui.update_metric("Tunnel Status", "Starting..." if enable_tunnel else "Disabled")

    # Metrics update thread
    command_count = {"value": 0}
    restart_state = {"requested": False, "reason": ""}
    restart_lock = Lock()

    def update_metrics_loop():
        while ui and ui.running:
            with sessions_lock:
                session_count = len(sessions)
            ui.update_metric("Sessions", str(session_count))
            ui.update_metric("Commands", str(command_count["value"]))

            process_running = tunnel_process is not None and tunnel_process.poll() is None
            with tunnel_url_lock:
                current_tunnel = tunnel_url if process_running else None
                stale_tunnel = tunnel_url if (tunnel_url and not process_running) else None
                current_error = tunnel_last_error

                if current_tunnel:
                    ui.update_metric("Tunnel URL", current_tunnel)
                    ui.update_metric("Tunnel Status", "Active")
                elif stale_tunnel and not process_running:
                    ui.update_metric("Tunnel URL", "Stale")
                    ui.update_metric("Tunnel Status", "Stale URL")
                elif current_error:
                    ui.update_metric("Tunnel URL", "N/A")
                    ui.update_metric("Tunnel Status", f"Error: {current_error}")
                elif process_running:
                    ui.update_metric("Tunnel URL", "Pending...")
                    ui.update_metric("Tunnel Status", "Starting...")
                else:
                    ui.update_metric("Tunnel URL", "N/A")
                    ui.update_metric("Tunnel Status", "Inactive")

            time.sleep(1)

    def request_restart(reason):
        with restart_lock:
            if restart_state["requested"]:
                return
            restart_state["requested"] = True
            restart_state["reason"] = reason
        log(reason)
        if ui:
            ui.set_status("Source change detected; restarting...")
            ui.stop()

    def watch_source_changes():
        source_path = os.path.abspath(__file__)
        try:
            last_mtime = os.path.getmtime(source_path)
        except OSError:
            log(f"Unable to watch {source_path} for changes")
            return

        while ui and ui.running:
            time.sleep(SCRIPT_WATCH_INTERVAL_SECONDS)
            try:
                current_mtime = os.path.getmtime(source_path)
            except OSError:
                continue
            if current_mtime != last_mtime:
                request_restart(
                    f"Detected change in {os.path.basename(source_path)}; restarting adapter..."
                )
                return
            last_mtime = current_mtime

    # Wrap process_command to count commands
    original_process = process_command

    def counted_process_command(cmd, serial_conn):
        result = original_process(cmd, serial_conn)
        if result.get("status") == "success":
            command_count["value"] += 1
        return result

    # Replace process_command references
    globals()["process_command"] = counted_process_command

    if ui and UI_AVAILABLE:
        # Run Flask in background thread
        flask_thread = threading.Thread(
            target=lambda: socketio.run(
                app,
                host=listen_host,
                port=listen_port,
                debug=False,
                use_reloader=False,
            ),
            daemon=True,
        )
        flask_thread.start()

        # Mark UI active before starting background updaters.
        ui.running = True

        # Start metrics updater
        metrics_thread = threading.Thread(target=update_metrics_loop, daemon=True)
        metrics_thread.start()

        # Start source watcher to restart on adapter.py edits.
        watch_thread = threading.Thread(target=watch_source_changes, daemon=True)
        watch_thread.start()

        log("Server started successfully")
        log("Terminal UI active - Press Ctrl+C to exit")

        # Run UI (blocking)
        try:
            ui.start()
        except KeyboardInterrupt:
            pass
        finally:
            service_running.clear()
            with restart_lock:
                restart_requested = restart_state["requested"]
                restart_reason = restart_state["reason"]

            if restart_requested:
                restart_current_process(restart_reason)
            else:
                stop_cloudflared_tunnel()
                log("Shutting down...")
    else:
        # Run Flask normally without UI
        try:
            socketio.run(app, host=listen_host, port=listen_port, debug=False, use_reloader=False)
        finally:
            service_running.clear()
            stop_cloudflared_tunnel()
if __name__ == "__main__":
    main()
