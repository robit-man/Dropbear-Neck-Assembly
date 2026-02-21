#!/usr/bin/env python
"""
All-in-one Python script that:
1. Auto-creates a virtual environment (if needed) and installs required packages (pyserial, Flask, Flask-SocketIO),
   then re-launches itself from the venv.
2. Loads configuration from config.json if available.
3. For the serial controller, probes available ports with a HEALTH command, validates DEVICE key,
   and binds the matching controller (default DEVICE=NECK).
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
import random
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
    from flask import Flask, jsonify, render_template_string, request
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
DEFAULT_LISTEN_PORT = 5180
LEGACY_DEFAULT_LISTEN_PORTS = {5001, 5060, 5160}
DEFAULT_LISTEN_ROUTE = "/send_command"
DEFAULT_ENABLE_TUNNEL = True
DEFAULT_AUTO_INSTALL_CLOUDFLARED = True
DEFAULT_SERIAL_EXPECTED_DEVICE_KEY = "NECK"
AUTO_SERIAL_USB_INDEX_MIN = 0
AUTO_SERIAL_USB_INDEX_MAX = 5
AUTO_SERIAL_PROBE_COMMAND = "HEALTH"
AUTO_SERIAL_PROBE_ATTEMPTS = 3
AUTO_SERIAL_PROBE_WARMUP_SECONDS = 0.35
AUTO_SERIAL_PROBE_RESPONSE_WINDOW_SECONDS = 0.9
AUTO_SERIAL_PROBE_READ_TIMEOUT_SECONDS = 0.15
AUTO_SERIAL_PROBE_WRITE_TIMEOUT_SECONDS = 0.6

# --- Cloudflare Tunnel ---
tunnel_url = None
tunnel_url_lock = Lock()
tunnel_process = None
tunnel_last_error = ""
tunnel_desired = False
tunnel_restart_lock = Lock()
tunnel_restart_failures = 0

# --- Runtime Control ---
SCRIPT_WATCH_INTERVAL_SECONDS = 1.0
DEFAULT_TUNNEL_RESTART_DELAY_SECONDS = 3.0
DEFAULT_TUNNEL_RATE_LIMIT_DELAY_SECONDS = 45.0
MAX_TUNNEL_RESTART_DELAY_SECONDS = 300.0
DEFAULT_ENABLE_UPNP_FALLBACK = True
UPNP_FALLBACK_REFRESH_SECONDS = 90.0
service_running = threading.Event()


def _next_tunnel_restart_delay(rate_limited=False):
    global tunnel_restart_failures
    tunnel_restart_failures = min(tunnel_restart_failures + 1, 8)
    base_delay = (
        DEFAULT_TUNNEL_RATE_LIMIT_DELAY_SECONDS
        if rate_limited
        else DEFAULT_TUNNEL_RESTART_DELAY_SECONDS
    )
    delay = base_delay * (2 ** max(0, tunnel_restart_failures - 1))
    jitter = random.uniform(0.0, min(6.0, max(1.0, delay * 0.15)))
    return min(delay + jitter, MAX_TUNNEL_RESTART_DELAY_SECONDS)


def stop_cloudflared_tunnel():
    """Stop the cloudflared subprocess if it is running."""
    global tunnel_process, tunnel_last_error, tunnel_url, tunnel_desired, tunnel_restart_failures
    tunnel_desired = False
    tunnel_restart_failures = 0
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
            global tunnel_url, tunnel_process, tunnel_last_error, tunnel_restart_failures
            found_url = False
            captured_url = ""
            rate_limited = False
            if process.stdout is None:
                return

            for raw_line in iter(process.stdout.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue
                lowered = line.lower()
                if any(token in lowered for token in ("error", "failed", "unable", "panic")):
                    log(f"[CLOUDFLARED] {line}")
                if "429 too many requests" in lowered or "error code: 1015" in lowered:
                    rate_limited = True

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
                                tunnel_restart_failures = 0
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
                    tunnel_restart_failures = 0
                    tunnel_last_error = f"cloudflared exited (code {return_code}); tunnel URL expired"
                    log(f"[WARN] {tunnel_last_error}")
                else:
                    if rate_limited:
                        tunnel_last_error = (
                            f"cloudflared rate-limited (429/1015) before URL (code {return_code})"
                        )
                    else:
                        tunnel_last_error = f"cloudflared exited before URL (code {return_code})"
                    log(f"[ERROR] {tunnel_last_error}")

                if tunnel_desired and service_running.is_set():
                    delay = _next_tunnel_restart_delay(rate_limited=rate_limited and not found_url)
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


def _normalize_device_key(value):
    key = re.sub(r"[^A-Za-z0-9_]+", "", str(value or "").strip().upper())
    return key


def _ordered_serial_candidates(preferred_device=None, candidate_devices=None):
    ordered = []
    seen = set()

    def add(device):
        port = str(device or "").strip()
        if not port or port in seen:
            return
        seen.add(port)
        ordered.append(port)

    add(preferred_device)

    if candidate_devices:
        for item in candidate_devices:
            add(item)
        return ordered

    env_ports = str(os.environ.get("ADAPTER_SERIAL_SCAN_PORTS", "")).strip()
    if env_ports:
        for token in env_ports.split(","):
            add(token)

    try:
        from serial.tools import list_ports

        discovered = []
        for port_info in list_ports.comports():
            port_name = str(getattr(port_info, "device", "") or "").strip()
            if port_name:
                discovered.append(port_name)
        for port_name in sorted(discovered):
            add(port_name)
    except Exception:
        pass

    if os.name == "nt":
        for idx in range(1, 33):
            add(f"COM{idx}")
    else:
        for idx in range(AUTO_SERIAL_USB_INDEX_MIN, AUTO_SERIAL_USB_INDEX_MAX + 1):
            add(f"/dev/ttyUSB{idx}")
        for idx in range(AUTO_SERIAL_USB_INDEX_MIN, AUTO_SERIAL_USB_INDEX_MAX + 1):
            add(f"/dev/ttyACM{idx}")

    return ordered


def _parse_serial_health_line(line):
    raw = str(line or "").strip()
    if not raw:
        return None

    # Optional JSON fallback for future controller variants.
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            parsed = {}
            for key, value in payload.items():
                clean_key = re.sub(r"[^A-Za-z0-9_]+", "", str(key).strip().upper())
                if not clean_key:
                    continue
                parsed[clean_key] = str(value).strip()
            device_key = _normalize_device_key(
                parsed.get("DEVICE")
                or parsed.get("DEVICE_KEY")
                or parsed.get("TYPE")
                or parsed.get("ROLE")
            )
            if device_key:
                parsed["DEVICE"] = device_key
            parsed["RAW"] = raw
            return parsed if parsed else None

    upper = raw.upper()
    tail = ""
    separators = None
    if upper == "HEALTH":
        return {"RAW": raw}
    if upper.startswith("HEALTH|"):
        tail = raw.split("|", 1)[1]
        separators = "|"
    elif upper.startswith("HEALTH:"):
        tail = raw.split(":", 1)[1]
        separators = "mixed"
    elif upper.startswith("HEALTH "):
        tail = raw.split(" ", 1)[1]
        separators = "mixed"
    else:
        return None

    if separators == "|":
        tokens = [part.strip() for part in tail.split("|") if part.strip()]
    else:
        tokens = [part.strip() for part in re.split(r"[|,\s]+", tail) if part.strip()]

    parsed = {}
    for token in tokens:
        key = ""
        value = ""
        if "=" in token:
            key, value = token.split("=", 1)
        elif ":" in token:
            key, value = token.split(":", 1)
        clean_key = re.sub(r"[^A-Za-z0-9_]+", "", str(key).strip().upper())
        if not clean_key:
            continue
        parsed[clean_key] = str(value).strip()

    device_key = _normalize_device_key(
        parsed.get("DEVICE")
        or parsed.get("DEVICE_KEY")
        or parsed.get("TYPE")
        or parsed.get("ROLE")
    )
    if device_key:
        parsed["DEVICE"] = device_key
    parsed["RAW"] = raw
    return parsed


def _probe_serial_candidate(device, baudrate, expected_device_key):
    expected = _normalize_device_key(expected_device_key)
    result = {
        "serial": None,
        "device": str(device or "").strip(),
        "baudrate": int(baudrate),
        "matched": False,
        "mismatch": False,
        "device_key": "",
        "health": {},
        "error": "",
    }
    serial_conn = None
    matched = False

    try:
        serial_conn = serial.Serial(
            result["device"],
            int(baudrate),
            timeout=AUTO_SERIAL_PROBE_READ_TIMEOUT_SECONDS,
            write_timeout=AUTO_SERIAL_PROBE_WRITE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        result["error"] = f"open failed: {exc}"
        return result

    try:
        time.sleep(AUTO_SERIAL_PROBE_WARMUP_SECONDS)
        try:
            serial_conn.reset_input_buffer()
        except Exception:
            pass
        try:
            serial_conn.reset_output_buffer()
        except Exception:
            pass

        parsed_any = False

        for _ in range(AUTO_SERIAL_PROBE_ATTEMPTS):
            try:
                serial_conn.write((AUTO_SERIAL_PROBE_COMMAND + "\n").encode("utf-8"))
                serial_conn.flush()
            except Exception as exc:
                result["error"] = f"probe write failed: {exc}"
                return result

            deadline = time.time() + AUTO_SERIAL_PROBE_RESPONSE_WINDOW_SECONDS
            while time.time() < deadline:
                try:
                    raw_bytes = serial_conn.readline()
                except Exception as exc:
                    result["error"] = f"probe read failed: {exc}"
                    return result
                if not raw_bytes:
                    continue
                line = raw_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                parsed = _parse_serial_health_line(line)
                if not parsed:
                    continue

                parsed_any = True
                result["health"] = dict(parsed)
                device_key = _normalize_device_key(parsed.get("DEVICE") or parsed.get("DEVICE_KEY"))
                result["device_key"] = device_key
                if device_key:
                    result["health"]["DEVICE"] = device_key

                if expected and device_key and device_key != expected:
                    result["mismatch"] = True
                    result["error"] = f"reported DEVICE={device_key}"
                    return result
                if expected and not device_key:
                    result["error"] = "HEALTH response missing DEVICE key"
                    continue

                matched = True
                result["matched"] = True
                result["serial"] = serial_conn
                return result

        if parsed_any and not result["error"]:
            result["error"] = "HEALTH response did not match expected device key"
        if not parsed_any and not result["error"]:
            result["error"] = "No HEALTH response"
        return result
    finally:
        if not matched and serial_conn is not None:
            try:
                serial_conn.close()
            except Exception:
                pass


def discover_serial_connection(
    expected_device_key,
    preferred_device=None,
    preferred_baudrate=DEFAULT_BAUDRATE,
    candidate_devices=None,
):
    expected = _normalize_device_key(expected_device_key)
    resolved_baudrate = _as_int(
        preferred_baudrate,
        DEFAULT_BAUDRATE,
        minimum=300,
        maximum=2_000_000,
    )
    baudrates = [int(resolved_baudrate)]
    if int(DEFAULT_BAUDRATE) not in baudrates:
        baudrates.append(int(DEFAULT_BAUDRATE))

    candidates = _ordered_serial_candidates(
        preferred_device=preferred_device,
        candidate_devices=candidate_devices,
    )
    if not candidates:
        return None, "", int(resolved_baudrate), {}, "No serial candidates available"

    mismatch_reports = []
    probe_errors = []

    for baud in baudrates:
        for device in candidates:
            probe = _probe_serial_candidate(device, baud, expected)
            if probe["matched"] and probe["serial"] is not None:
                health = dict(probe.get("health") or {})
                if expected and "DEVICE" not in health:
                    health["DEVICE"] = expected
                return probe["serial"], probe["device"], int(baud), health, ""

            detail = str(probe.get("error") or "probe failed").strip() or "probe failed"
            if probe.get("mismatch"):
                found_key = probe.get("device_key") or "UNKNOWN"
                mismatch_reports.append(f"{probe['device']} -> {found_key}")
            else:
                probe_errors.append(f"{probe['device']}@{baud}: {detail}")

    expected_desc = expected or "ANY"
    if mismatch_reports:
        return (
            None,
            "",
            int(resolved_baudrate),
            {},
            f"No controller with DEVICE={expected_desc} matched HEALTH probe. Found: {', '.join(mismatch_reports)}",
        )

    if probe_errors:
        return (
            None,
            "",
            int(resolved_baudrate),
            {},
            f"No controller with DEVICE={expected_desc} responded to HEALTH. Last probe error: {probe_errors[-1]}",
        )

    return None, "", int(resolved_baudrate), {}, f"No serial candidates available for DEVICE={expected_desc}"


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

    expected_device_key = _normalize_device_key(
        _read_config_value(
            config,
            "adapter.serial.expected_device_key",
            DEFAULT_SERIAL_EXPECTED_DEVICE_KEY,
            legacy_keys=("expected_device_key", "device_key"),
        )
    ) or DEFAULT_SERIAL_EXPECTED_DEVICE_KEY
    promote("adapter.serial.expected_device_key", expected_device_key)

    listen_host = str(
        _read_config_value(
            config, "adapter.network.listen_host", DEFAULT_LISTEN_HOST, legacy_keys=("listen_host",)
        )
    ).strip() or DEFAULT_LISTEN_HOST
    lowered_listen_host = listen_host.lower()
    if lowered_listen_host in ("localhost", "::1") or lowered_listen_host.startswith("127."):
        listen_host = DEFAULT_LISTEN_HOST
    promote("adapter.network.listen_host", listen_host)

    listen_port = _as_int(
        _read_config_value(
            config, "adapter.network.listen_port", DEFAULT_LISTEN_PORT, legacy_keys=("listen_port",)
        ),
        DEFAULT_LISTEN_PORT,
        minimum=1,
        maximum=65535,
    )
    if listen_port in LEGACY_DEFAULT_LISTEN_PORTS:
        listen_port = DEFAULT_LISTEN_PORT
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
        "expected_device_key": expected_device_key,
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
                        restart_required=False,
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
                        restart_required=False,
                    ),
                    SettingSpec(
                        id="expected_device_key",
                        label="Expected Device Key",
                        path="adapter.serial.expected_device_key",
                        value_type="str",
                        default=DEFAULT_SERIAL_EXPECTED_DEVICE_KEY,
                        description="Expected DEVICE value returned by HEALTH probe (e.g. NECK, LLEG).",
                        restart_required=False,
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

# --- Configurator helpers (mirror camera_route.py pattern) ---
def _config_spec_available():
    return UI_AVAILABLE and ConfigSpec is not None and SettingSpec is not None and CategorySpec is not None


def _serialize_setting_spec(spec):
    return {
        "id": str(spec.id),
        "label": str(spec.label),
        "path": str(spec.path),
        "value_type": str(spec.value_type),
        "description": str(spec.description or ""),
        "default": spec.default,
        "choices": list(getattr(spec, "choices", None) or ()),
        "sensitive": bool(getattr(spec, "sensitive", False)),
        "restart_required": bool(getattr(spec, "restart_required", False)),
        "min_value": getattr(spec, "min_value", None),
        "max_value": getattr(spec, "max_value", None),
    }


def _adapter_config_schema_payload(config_data=None):
    if not _config_spec_available():
        return {
            "status": "error",
            "message": "Configurator unavailable (terminal_ui support is not loaded).",
        }, 503

    spec = _build_adapter_config_spec()
    if spec is None:
        return {"status": "error", "message": "Configurator spec unavailable."}, 503

    config_data = load_config() if config_data is None else config_data
    categories_payload = []
    for category in spec.categories:
        settings_payload = []
        for setting in category.settings:
            raw = _get_nested(config_data, setting.path, _MISSING)
            if raw is _MISSING:
                current_value = setting.default
                current_source = "default"
            else:
                current_value = raw
                current_source = "config"
            settings_payload.append(
                {
                    **_serialize_setting_spec(setting),
                    "current_value": current_value,
                    "current_source": current_source,
                }
            )
        categories_payload.append(
            {
                "id": str(category.id),
                "label": str(category.label),
                "settings": settings_payload,
            }
        )

    return {
        "status": "success",
        "config": {
            "label": str(spec.label),
            "categories": categories_payload,
        },
    }, 200


def _coerce_config_value(raw_value, setting_spec):
    value_type = str(setting_spec.value_type or "str").strip().lower()

    if value_type == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        if isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                return True
            if normalized in ("0", "false", "no", "off"):
                return False
        raise ValueError("Expected boolean value")

    if value_type == "int":
        try:
            parsed = int(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError("Expected integer value")
        if setting_spec.min_value is not None and parsed < setting_spec.min_value:
            raise ValueError(f"Minimum is {setting_spec.min_value}")
        if setting_spec.max_value is not None and parsed > setting_spec.max_value:
            raise ValueError(f"Maximum is {setting_spec.max_value}")
        return parsed

    if value_type == "float":
        try:
            parsed = float(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError("Expected numeric value")
        if setting_spec.min_value is not None and parsed < setting_spec.min_value:
            raise ValueError(f"Minimum is {setting_spec.min_value}")
        if setting_spec.max_value is not None and parsed > setting_spec.max_value:
            raise ValueError(f"Maximum is {setting_spec.max_value}")
        return parsed

    if value_type == "secret":
        return str(raw_value)

    return str(raw_value).strip()


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


def _interactive_prompts_allowed():
    raw = str(os.environ.get("ADAPTER_DISABLE_INTERACTIVE_PROMPTS", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False

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
        "require_auth": True,
    }
    ser = None
    serial_device = adapter_settings["serial_device"]
    baudrate = adapter_settings["baudrate"]
    serial_expected_device_key = (
        _normalize_device_key(adapter_settings.get("expected_device_key"))
        or DEFAULT_SERIAL_EXPECTED_DEVICE_KEY
    )
    serial_health_report = {}

    def _persist(path, value):
        nonlocal config_changed
        current = _get_nested(config, path, _MISSING)
        if current is _MISSING or current != value:
            _set_nested(config, path, value)
            config_changed = True
            return True
        return False

    def _close_serial_locked():
        nonlocal ser
        if ser is None:
            return
        try:
            if getattr(ser, "is_open", False):
                ser.close()
                log(f"Serial disconnected: {serial_device or 'N/A'}@{baudrate}")
        except Exception as close_exc:
            log(f"Serial close warning: {close_exc}")
        finally:
            ser = None

    def _connect_serial_controller(
        preferred_device=None,
        preferred_baudrate=None,
        candidate_devices=None,
        context_label="[SERIAL]",
        save_if_changed=False,
    ):
        nonlocal ser, serial_device, baudrate, serial_health_report

        target_baudrate = _as_int(
            preferred_baudrate if preferred_baudrate is not None else baudrate,
            DEFAULT_BAUDRATE,
            minimum=300,
            maximum=2_000_000,
        )
        expected_key = _normalize_device_key(serial_expected_device_key) or DEFAULT_SERIAL_EXPECTED_DEVICE_KEY

        with serial_io_lock:
            _close_serial_locked()
            found_ser, found_device, found_baud, found_health, failure_reason = discover_serial_connection(
                expected_device_key=expected_key,
                preferred_device=preferred_device,
                preferred_baudrate=target_baudrate,
                candidate_devices=candidate_devices,
            )
            if found_ser is None:
                ser = None
                serial_health_report = {}
                return False, failure_reason

            ser = found_ser
            serial_device = str(found_device or "").strip()
            baudrate = int(found_baud)
            serial_health_report = dict(found_health or {})
            if expected_key and "DEVICE" not in serial_health_report:
                serial_health_report["DEVICE"] = expected_key

        changed_any = False
        changed_any = _persist("adapter.serial.device", serial_device) or changed_any
        changed_any = _persist("adapter.serial.baudrate", int(baudrate)) or changed_any
        changed_any = _persist("adapter.serial.expected_device_key", expected_key) or changed_any
        if save_if_changed and changed_any:
            save_config(config)

        discovered_key = _normalize_device_key(
            serial_health_report.get("DEVICE") or serial_health_report.get("device_key")
        )
        key_suffix = f", DEVICE={discovered_key}" if discovered_key else ""
        log(f"{context_label} Serial connected: {serial_device}@{baudrate}{key_suffix}")
        return True, "Serial connected"

    def _apply_runtime_config(saved_config):
        global SESSION_TIMEOUT
        nonlocal ser, serial_device, baudrate, serial_expected_device_key, serial_health_report
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
        configured_serial_device = _read_config_value(
            saved_config,
            "adapter.serial.device",
            serial_device or "",
            legacy_keys=("serial_device",),
        )
        configured_serial_device = str(configured_serial_device or "").strip() or None
        configured_baudrate = _as_int(
            _read_config_value(
                saved_config,
                "adapter.serial.baudrate",
                baudrate,
                legacy_keys=("baudrate",),
            ),
            baudrate,
            minimum=300,
            maximum=2_000_000,
        )
        configured_expected_device_key = _normalize_device_key(
            _read_config_value(
                saved_config,
                "adapter.serial.expected_device_key",
                serial_expected_device_key,
                legacy_keys=("expected_device_key", "device_key"),
            )
        ) or DEFAULT_SERIAL_EXPECTED_DEVICE_KEY

        runtime_security["password"] = password
        SESSION_TIMEOUT = timeout
        current_detected_key = _normalize_device_key(
            serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
        )
        serial_apply = {
            "changed": False,
            "attempted": False,
            "configured_device": configured_serial_device or "",
            "baudrate": int(configured_baudrate),
            "expected_device_key": configured_expected_device_key,
            "connected": bool(ser is not None and getattr(ser, "is_open", False)),
            "detected_device_key": current_detected_key,
            "error": "",
        }

        if (
            configured_serial_device != serial_device
            or int(configured_baudrate) != int(baudrate)
            or configured_expected_device_key != serial_expected_device_key
        ):
            serial_apply["changed"] = True
            serial_apply["attempted"] = True
            serial_expected_device_key = configured_expected_device_key

            connected, connect_message = _connect_serial_controller(
                preferred_device=configured_serial_device,
                preferred_baudrate=int(configured_baudrate),
                context_label="[CONFIG]",
                save_if_changed=True,
            )
            serial_apply["connected"] = connected
            serial_apply["error"] = "" if connected else connect_message
            serial_apply["configured_device"] = configured_serial_device or ""
            serial_apply["baudrate"] = int(configured_baudrate)
            serial_apply["detected_device_key"] = _normalize_device_key(
                serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
            )
            if not connected:
                log(
                    f"[WARN] Configured serial connect failed for "
                    f"{configured_serial_device or '(auto)'}@{configured_baudrate} "
                    f"(DEVICE={serial_expected_device_key}): {connect_message}"
                )

        if ui:
            ui.update_metric("Session Timeout (s)", str(SESSION_TIMEOUT))
            ui.update_metric("Serial Port", serial_device or "N/A")
            ui.update_metric("Baudrate", str(baudrate))
            ui.update_metric("Expected Device", serial_expected_device_key or "N/A")
            ui.update_metric(
                "Detected Device",
                _normalize_device_key(
                    serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
                )
                or "N/A",
            )
            ui.update_metric(
                "Serial",
                "Connected" if (ser is not None and getattr(ser, "is_open", False)) else "Disconnected",
            )
            ui.log("Applied live security updates from config save")
            if serial_apply["changed"]:
                if serial_apply["connected"]:
                    ui.log(f"Serial config applied: {serial_device}@{baudrate}")
                else:
                    ui.log(
                        f"Serial config apply failed for {serial_device or '(none)'}@{baudrate}: "
                        f"{serial_apply['error'] or 'not connected'}"
                    )
        return serial_apply

    if ui:
        ui.on_save(_apply_runtime_config)

    interactive_prompts = _interactive_prompts_allowed()
    if not interactive_prompts:
        log("[BOOT] Interactive prompts disabled; adapter will continue without blocking for manual input")
    log(f"[BOOT] Serial HEALTH probe target DEVICE={serial_expected_device_key}")

    boot_connected, boot_message = _connect_serial_controller(
        preferred_device=serial_device,
        preferred_baudrate=baudrate,
        context_label="[BOOT]",
    )
    if boot_connected:
        print("Serial connection OK via HEALTH discovery")
    else:
        log(f"[WARN] {boot_message}")

    # Optional interactive fallback for manual override in supervised mode.
    if ser is None and interactive_prompts:
        while ser is None:
            device = input("Serial device (e.g. /dev/ttyUSB2 or COM3): ").strip()
            baud_in = input(f"Baudrate (default {DEFAULT_BAUDRATE}): ").strip() or str(DEFAULT_BAUDRATE)
            try:
                baud = int(baud_in)
            except ValueError:
                print("Invalid baudrate.\n")
                continue

            manual_device = str(device or "").strip() or None
            connected, connect_message = _connect_serial_controller(
                preferred_device=manual_device,
                preferred_baudrate=baud,
                candidate_devices=[manual_device] if manual_device else None,
                context_label="[MANUAL]",
                save_if_changed=True,
            )
            if connected:
                print("Serial connection successful!")
            else:
                print(f"Serial connect error: {connect_message}\n")
    elif ser is None:
        log("[WARN] Serial not connected; starting HTTP/WS endpoints in degraded mode")

    def reset_serial_connection(trigger_home=True, home_command="HOME_BRUTE"):
        """Reconnect serial port using HEALTH discovery, optionally issuing a home command."""
        nonlocal ser, serial_device, baudrate, serial_health_report

        normalized_home = _normalized_home_command(str(home_command)) or "home_brute"
        outbound_home = normalized_home.upper()
        preferred_device = serial_device
        preferred_baudrate = baudrate

        if preferred_device:
            connected, reconnect_message = _connect_serial_controller(
                preferred_device=preferred_device,
                preferred_baudrate=preferred_baudrate,
                candidate_devices=[preferred_device],
                context_label="[RESET]",
                save_if_changed=True,
            )
            if not connected:
                log(
                    f"[WARN] Reconnect on configured serial device failed ({preferred_device}@{preferred_baudrate}); "
                    "running full HEALTH discovery scan"
                )
                log(f"[WARN] {reconnect_message}")
        else:
            connected = False
            reconnect_message = "No configured serial device"

        if not connected:
            connected, reconnect_message = _connect_serial_controller(
                preferred_device=preferred_device,
                preferred_baudrate=preferred_baudrate,
                context_label="[RESET]",
                save_if_changed=True,
            )
            if not connected:
                return False, f"Reconnect failed: {reconnect_message}", None

        # Give firmware a moment after reconnect before optional home command.
        time.sleep(0.2)

        if trigger_home:
            try:
                with serial_io_lock:
                    if ser is None or not getattr(ser, "is_open", False):
                        return False, "Reconnect succeeded but serial is not open", None
                    ser.write((outbound_home + "\n").encode("utf-8"))
                _reset_state_to_home_defaults()
                log(f"Sent command after serial reset: {outbound_home}")
            except Exception as home_exc:
                return False, f"Reconnect succeeded but home send failed: {home_exc}", outbound_home

        detected_key = _normalize_device_key(
            serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
        )
        message = f"Serial port reset complete ({serial_device}@{baudrate}"
        if detected_key:
            message += f", DEVICE={detected_key}"
        message += ")"
        return True, message, outbound_home if trigger_home else None

    def retest_serial_ports():
        """Force a HEALTH-based serial rediscovery scan without issuing movement commands."""
        nonlocal serial_device, baudrate, serial_health_report

        preferred_device = serial_device
        preferred_baudrate = baudrate
        connected, reconnect_message = _connect_serial_controller(
            preferred_device=preferred_device,
            preferred_baudrate=preferred_baudrate,
            context_label="[RETEST]",
            save_if_changed=True,
        )
        if not connected:
            return False, f"Retest failed: {reconnect_message}"

        detected_key = _normalize_device_key(
            serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
        )
        message = f"Serial retest complete ({serial_device}@{baudrate}"
        if detected_key:
            message += f", DEVICE={detected_key}"
        message += ")"
        return True, message

    # --- Network Host/Port/Route ---
    listen_host = adapter_settings["listen_host"]
    listen_route = _normalize_route(adapter_settings["listen_route"])
    configured_port = _as_int(
        adapter_settings.get("listen_port"),
        DEFAULT_LISTEN_PORT,
        minimum=1,
        maximum=65535,
    )

    if is_port_available(configured_port, listen_host):
        listen_port = configured_port
        log(f"[BOOT] Using configured listen port: {configured_port}")
    elif configured_port != DEFAULT_LISTEN_PORT and is_port_available(DEFAULT_LISTEN_PORT, listen_host):
        listen_port = DEFAULT_LISTEN_PORT
        log(
            f"[WARN] Configured listen port {configured_port} unavailable on {listen_host}; "
            f"falling back to default {DEFAULT_LISTEN_PORT}"
        )
    else:
        raise RuntimeError(
            f"No available adapter port on {listen_host}. "
            f"Tried configured={configured_port} and default={DEFAULT_LISTEN_PORT}."
        )

    _persist("adapter.network.listen_host", listen_host)
    _persist("adapter.network.listen_port", listen_port)
    _persist("adapter.network.listen_route", listen_route)
    _persist("adapter.serial.expected_device_key", serial_expected_device_key)
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
    started_at = time.time()
    command_count = {"value": 0}
    request_count = {"value": 0}

    def _resolve_lan_host():
        bind_host = str(listen_host or "").strip()
        if bind_host and bind_host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
            return bind_host
        try:
            # Best-effort host detection for LAN-reachable address.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                candidate = str(sock.getsockname()[0] or "").strip()
            if candidate and not candidate.startswith("127."):
                return candidate
        except Exception:
            pass
        try:
            candidate = str(socket.gethostbyname(socket.gethostname()) or "").strip()
            if candidate and not candidate.startswith("127."):
                return candidate
        except Exception:
            pass
        return ""

    def _network_endpoints():
        local_base = f"http://127.0.0.1:{listen_port}"
        bind_host = str(listen_host or "").strip() or "127.0.0.1"
        bind_base = f"http://{bind_host}:{listen_port}"
        lan_host = _resolve_lan_host()
        lan_base = f"http://{lan_host}:{listen_port}" if lan_host else ""
        local_http = f"{local_base}{listen_route}"
        local_ws = f"ws://127.0.0.1:{listen_port}/ws"
        lan_http = f"{lan_base}{listen_route}" if lan_base else ""
        lan_ws = f"ws://{lan_host}:{listen_port}/ws" if lan_host else ""
        return {
            "local_base": local_base,
            "bind_base": bind_base,
            "lan_base": lan_base,
            "local_http": local_http,
            "local_ws": local_ws,
            "lan_http": lan_http,
            "lan_ws": lan_ws,
        }

    fallback_lock = Lock()
    nats_subject_token = secrets.token_hex(8)
    nkn_topic_token = secrets.token_hex(8)
    fallback_state = {
        "upnp": {
            "state": "inactive",
            "enabled": DEFAULT_ENABLE_UPNP_FALLBACK,
            "public_ip": "",
            "external_port": 0,
            "public_base_url": "",
            "dashboard_url": "",
            "http_endpoint": "",
            "ws_endpoint": "",
            "error": "",
            "last_attempt_ms": 0,
        },
        "nats": {
            "state": "inactive",
            "broker_url": "wss://demo.nats.io:443",
            "subject": f"dropbear.adapter.{nats_subject_token}",
            "error": "NATS fallback is advertised but no relay is configured in this build",
        },
        "nkn": {
            "state": "inactive",
            "topic": f"dropbear.adapter.{nkn_topic_token}",
            "nkn_address": "",
            "address": "",
            "configured": False,
            "error": "Set DROPBEAR_ADAPTER_NKN_ADDRESS to enable per-service NKN fallback",
        },
    }

    def _normalize_nkn_address(raw_value):
        text = str(raw_value or "").strip()
        if not text:
            return ""
        if text.lower().startswith("nkn://"):
            text = text[6:]
        text = text.strip().strip("/")
        parts = [part for part in text.split(".") if part]
        if not parts:
            return ""
        pubkey = str(parts[-1] or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", pubkey):
            return ""
        prefix = ".".join(parts[:-1]).strip()
        return f"{prefix}.{pubkey}" if prefix else pubkey

    def _refresh_nkn_fallback():
        configured_address = _normalize_nkn_address(
            os.environ.get("DROPBEAR_ADAPTER_NKN_ADDRESS")
            or os.environ.get("DROPBEAR_NKN_ADDRESS")
            or ""
        )
        with fallback_lock:
            payload = dict(fallback_state.get("nkn", {}))
            payload["nkn_address"] = configured_address
            payload["address"] = configured_address
            payload["configured"] = bool(configured_address)
            if configured_address:
                payload["state"] = "active"
                payload["error"] = ""
            else:
                payload["state"] = "inactive"
                payload["error"] = "Set DROPBEAR_ADAPTER_NKN_ADDRESS to enable per-service NKN fallback"
            fallback_state["nkn"] = payload
            return dict(payload)

    def _snapshot_upnp_fallback():
        with fallback_lock:
            return dict(fallback_state["upnp"])

    def _refresh_upnp_fallback(force=False):
        now_ms = int(time.time() * 1000)
        with fallback_lock:
            upnp_payload = dict(fallback_state.get("upnp", {}))
            enabled = bool(upnp_payload.get("enabled", DEFAULT_ENABLE_UPNP_FALLBACK))
            last_attempt_ms = int(upnp_payload.get("last_attempt_ms", 0) or 0)
            if not enabled:
                fallback_state["upnp"].update(
                    {
                        "state": "disabled",
                        "error": "UPnP fallback disabled",
                        "last_attempt_ms": now_ms,
                    }
                )
                return dict(fallback_state["upnp"])
            if not force and last_attempt_ms and (now_ms - last_attempt_ms) < int(UPNP_FALLBACK_REFRESH_SECONDS * 1000):
                return dict(fallback_state["upnp"])
            fallback_state["upnp"]["last_attempt_ms"] = now_ms

        result = {
            "state": "inactive",
            "enabled": enabled,
            "public_ip": "",
            "external_port": 0,
            "public_base_url": "",
            "dashboard_url": "",
            "http_endpoint": "",
            "ws_endpoint": "",
            "error": "",
            "last_attempt_ms": now_ms,
        }
        try:
            import miniupnpc  # type: ignore
        except Exception as exc:
            result["state"] = "unavailable"
            result["error"] = f"miniupnpc unavailable: {exc}"
            with fallback_lock:
                fallback_state["upnp"].update(result)
                return dict(fallback_state["upnp"])

        try:
            upnp = miniupnpc.UPnP()
            upnp.discoverdelay = 2000
            discovered = int(upnp.discover() or 0)
            if discovered <= 0:
                raise RuntimeError("no UPnP IGD discovered")
            upnp.selectigd()
            lan_addr = str(upnp.lanaddr or "").strip()
            public_ip = str(upnp.externalipaddress() or "").strip()
            if not lan_addr or not public_ip:
                raise RuntimeError("missing LAN or public IP from IGD")

            internal_port = int(listen_port)
            preferred = internal_port if internal_port > 0 else 0
            candidates = []
            if preferred:
                candidates.append(preferred)
            for _ in range(18):
                candidates.append(random.randint(20000, 61000))

            mapped_port = 0
            for external_port in candidates:
                if external_port <= 0:
                    continue
                try:
                    existing = upnp.getspecificportmapping(external_port, "TCP")
                except Exception:
                    existing = None
                if existing:
                    try:
                        existing_host = str(existing[0] or "").strip()
                        existing_port = int(existing[1] or 0)
                    except Exception:
                        existing_host = ""
                        existing_port = 0
                    if existing_host != lan_addr or existing_port != internal_port:
                        continue
                try:
                    added = bool(
                        upnp.addportmapping(
                            external_port,
                            "TCP",
                            lan_addr,
                            internal_port,
                            "dropbear-neck-adapter",
                            "",
                        )
                    )
                except Exception:
                    added = False
                if added:
                    mapped_port = int(external_port)
                    break

            if mapped_port <= 0:
                raise RuntimeError("unable to reserve a public UPnP TCP port mapping")

            public_base = f"http://{public_ip}:{mapped_port}"
            ws_base = public_base.replace("http://", "ws://").replace("https://", "wss://")
            result.update(
                {
                    "state": "active",
                    "public_ip": public_ip,
                    "external_port": mapped_port,
                    "public_base_url": public_base,
                    "dashboard_url": f"{public_base}/",
                    "http_endpoint": f"{public_base}{listen_route}",
                    "ws_endpoint": f"{ws_base}/ws",
                    "error": "",
                }
            )
        except Exception as exc:
            result["state"] = "error"
            result["error"] = str(exc)

        with fallback_lock:
            fallback_state["upnp"].update(result)
            return dict(fallback_state["upnp"])

    def _adapter_fallback_payload(current_tunnel, process_running):
        tunnel_active = bool(process_running and str(current_tunnel or "").strip())
        if tunnel_active:
            upnp_payload = _snapshot_upnp_fallback()
        else:
            upnp_payload = _refresh_upnp_fallback(force=False)
        nats_payload = dict(fallback_state.get("nats", {}))
        nkn_payload = _refresh_nkn_fallback()

        selected = "local"
        if tunnel_active:
            selected = "cloudflare"
        elif str(upnp_payload.get("state") or "").strip().lower() == "active":
            selected = "upnp"
        elif str(nats_payload.get("state") or "").strip().lower() == "active":
            selected = "nats"
        elif str(nkn_payload.get("state") or "").strip().lower() == "active":
            selected = "nkn"

        return {
            "selected_transport": selected,
            "order": ["cloudflare", "upnp", "nats", "nkn", "local"],
            "upnp": upnp_payload,
            "nats": nats_payload,
            "nkn": nkn_payload,
        }

    ADAPTER_INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Neck Adapter Dashboard</title>
    <style>
      * { box-sizing: border-box; }
      body { margin: 0; background: #111; color: #fff; font-family: monospace; }
      .wrap { max-width: 1280px; margin: 0 auto; padding: 1rem; }
      .panel { background: #1b1b1b; border: 1px solid #333; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; }
      .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
      .title { margin: 0; }
      .muted { opacity: 0.85; }
      .ok { color: #00d08a; }
      .warn { color: #ffcc66; }
      .bad { color: #ff5c5c; }
      code { color: #ffcc66; }
      a, a:visited { color: #d8e3ff; }
      input, button, select { background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 0.5rem; }
      button { cursor: pointer; }
      .tabs { display: flex; gap: 0.5rem; flex-wrap: wrap; }
      .tab-btn { background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 0.5rem 0.8rem; cursor: pointer; }
      .tab-btn.active { border-color: #8aa0ff; color: #d7e1ff; }
      .tab-panel { display: none; margin-top: 0.8rem; }
      .tab-panel.active { display: block; }
      .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.75rem; }
      .card { background: #1b1b1b; border: 1px solid #333; border-radius: 10px; padding: 0.8rem; }
      .card .label { opacity: 0.85; font-size: 0.78rem; }
      .card .value { margin-top: 4px; font-size: 1.05rem; }
      .motor-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 0.75rem; }
      .motor-card { background: #161616; border: 1px solid #333; border-radius: 10px; padding: 0.8rem; }
      .motor-card .motor-label { font-size: 0.85rem; opacity: 0.85; margin-bottom: 4px; }
      .motor-card .motor-value { font-size: 1.4rem; font-weight: 700; margin-bottom: 6px; }
      .motor-bar-track { width: 100%; height: 10px; background: #222; border-radius: 5px; overflow: hidden; position: relative; }
      .motor-bar-fill { height: 100%; border-radius: 5px; transition: width 0.3s, left 0.3s; position: absolute; }
      .motor-bar-center { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: #555; }
      .motor-range { font-size: 0.72rem; opacity: 0.6; margin-top: 3px; display: flex; justify-content: space-between; }
      table { width: 100%; border-collapse: collapse; }
      th, td { text-align: left; padding: 0.45rem; border-bottom: 1px solid #333; vertical-align: top; }
      th { opacity: 0.85; }
      .cfg-wrap { overflow: auto; max-height: 460px; border: 1px solid #333; border-radius: 8px; }
      .cfg-input, .cfg-select { width: 100%; background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 0.35rem 0.45rem; }
      .cfg-check { transform: scale(1.1); }
      .cfg-row.pending { background: rgba(0, 208, 138, 0.08); }
      .cfg-source { font-size: 0.8rem; opacity: 0.85; }
      .cfg-path { font-size: 0.78rem; opacity: 0.72; }
      .badge { display: inline-block; margin-left: 0.4rem; padding: 0.1rem 0.4rem; border-radius: 999px; border: 1px solid #666; font-size: 0.7rem; }
      .config-status.ok { color: #00d08a; }
      .config-status.bad { color: #ff5c5c; }
      .meta { opacity: 0.85; font-size: 0.85rem; margin: 0.3rem 0; }
      .links-grid { display: grid; grid-template-columns: auto 1fr; gap: 0.2rem 0.8rem; align-items: baseline; }
      .links-grid .lbl { opacity: 0.75; text-align: right; white-space: nowrap; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="panel">
        <div class="row">
          <h2 class="title">Neck Adapter Dashboard</h2>
          <span id="serialState" class="muted">loading...</span>
          <span class="muted" id="refreshState"></span>
        </div>
        <div class="row" style="margin-top:8px">
          <label for="password">Password</label>
          <input id="password" type="password" placeholder="Enter password">
          <button id="connectBtn" type="button">Authenticate</button>
          <button id="refreshNowBtn" type="button">Refresh Now</button>
          <button id="serialResetBtn" type="button">Serial Reset</button>
        </div>
        <div id="statusLine" class="meta">Not authenticated.</div>
      </div>

      <div class="panel">
        <h3 style="margin-top:0">Motor States</h3>
        <div id="motorGrid" class="motor-grid"></div>
      </div>

      <div class="panel">
        <div class="cards">
          <div class="card"><div class="label">Serial Port</div><div id="serialPort" class="value">--</div></div>
          <div class="card"><div class="label">Baudrate</div><div id="baudrate" class="value">--</div></div>
          <div class="card"><div class="label">Serial Status</div><div id="serialStatus" class="value">--</div></div>
          <div class="card"><div class="label">Sessions Active</div><div id="sessionsActive" class="value">0</div></div>
          <div class="card"><div class="label">Commands Served</div><div id="commandsServed" class="value">0</div></div>
          <div class="card"><div class="label">Requests Served</div><div id="requestsServed" class="value">0</div></div>
          <div class="card"><div class="label">Uptime</div><div id="uptime" class="value">--</div></div>
          <div class="card"><div class="label">Tunnel</div><div id="tunnelStatus" class="value">--</div></div>
        </div>
      </div>

      <div class="panel">
        <div class="tabs">
          <button id="tabHealthBtn" class="tab-btn active" type="button">Health &amp; Links</button>
          <button id="tabConfigBtn" class="tab-btn" type="button">Configurator</button>
        </div>
        <div id="healthPanel" class="tab-panel active">
          <h3 style="margin-top:0">/health</h3>
          <pre id="healthOut" class="meta">loading...</pre>
          <h3>Endpoints</h3>
          <div class="links-grid">
            <span class="lbl">Dashboard:</span><span><a href="/" id="linkDashboard">/</a></span>
            <span class="lbl">Health:</span><span><a href="/health" id="linkHealth">/health</a></span>
            <span class="lbl">Command:</span><span><code id="linkCommand">{{ listen_route }}</code></span>
            <span class="lbl">WebSocket:</span><span><code>ws://127.0.0.1:{{ listen_port }}/ws</code></span>
            <span class="lbl">Router Info:</span><span><a href="/router_info">/router_info</a></span>
            <span class="lbl">Tunnel Info:</span><span><a href="/tunnel_info">/tunnel_info</a></span>
            <span class="lbl">Config Schema:</span><span><a href="/config/schema">/config/schema</a></span>
          </div>
        </div>
        <div id="configPanel" class="tab-panel">
          <div class="row">
            <button id="configReloadBtn" type="button">Reload Config</button>
            <button id="configSaveBtn" type="button">Save Changes</button>
            <button id="configDiscardBtn" type="button">Discard Changes</button>
          </div>
          <div id="configStatus" class="meta config-status">Load config schema to begin.</div>
          <div id="configCategoryTabs" class="tabs" style="margin:0.6rem 0;"></div>
          <div class="cfg-wrap">
            <table>
              <thead>
                <tr>
                  <th style="min-width:180px;">Setting</th>
                  <th style="min-width:220px;">Value</th>
                  <th style="min-width:70px;">Type</th>
                  <th style="min-width:90px;">Source</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody id="configRows"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <script>
      var sessionKey = "";
      var polling = false;
      var MOTOR_LABELS = {
        X: "Pan (X)",
        Y: "Tilt (Y)",
        Z: "Roll (Z)",
        H: "Height (H)",
        S: "Speed (S)",
        A: "Accel (A)",
        R: "Roll Motor (R)",
        P: "Pan Motor (P)"
      };
      var MOTOR_COLORS = {
        X: "#5ca8ff", Y: "#00d08a", Z: "#ffcc66", H: "#ff8c5c",
        S: "#d08aff", A: "#ff5ca8", R: "#8affe0", P: "#ffd45c"
      };
      var configState = { schema: null, selectedCategoryId: "", pending: {} };

      function esc(v) {
        if (v === null || v === undefined) v = "";
        return String(v).replace(/[&<>"']/g, function(m) {
          return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m];
        });
      }

      function normalizeScalar(v) {
        if (v === null || v === undefined) return "";
        if (typeof v === "object") { try { return JSON.stringify(v); } catch(_) { return String(v); } }
        return String(v);
      }

      function asBool(v) {
        if (typeof v === "boolean") return v;
        return ["1","true","yes","on"].includes(String(v||"").trim().toLowerCase());
      }

      function fmtUptime(s) {
        var h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
        return (h>0 ? h+"h " : "") + m+"m " + sec+"s";
      }

      function renderMotors(motorState, motorRanges) {
        var root = document.getElementById("motorGrid");
        if (!root) return;
        var keys = ["X","Y","Z","H","S","A","R","P"];
        var html = "";
        for (var i = 0; i < keys.length; i++) {
          var k = keys[i];
          var val = Number(motorState[k] || 0);
          var range = motorRanges[k] || {min:0,max:1};
          var lo = Number(range.min), hi = Number(range.max);
          var span = hi - lo || 1;
          var hasCenterZero = lo < 0;
          var pct, leftPct, barColor;
          barColor = MOTOR_COLORS[k] || "#5ca8ff";
          if (hasCenterZero) {
            var centerPct = Math.abs(lo) / span * 100;
            if (val >= 0) {
              leftPct = centerPct;
              pct = (val / hi) * (100 - centerPct);
            } else {
              pct = (Math.abs(val) / Math.abs(lo)) * centerPct;
              leftPct = centerPct - pct;
            }
          } else {
            leftPct = 0;
            pct = ((val - lo) / span) * 100;
          }
          pct = Math.max(0.5, Math.min(100, pct));
          html += '<div class="motor-card">';
          html += '<div class="motor-label">' + esc(MOTOR_LABELS[k] || k) + '</div>';
          html += '<div class="motor-value" style="color:' + barColor + '">' + val + '</div>';
          html += '<div class="motor-bar-track">';
          if (hasCenterZero) html += '<div class="motor-bar-center"></div>';
          html += '<div class="motor-bar-fill" style="background:' + barColor + ';left:' + leftPct.toFixed(1) + '%;width:' + pct.toFixed(1) + '%"></div>';
          html += '</div>';
          html += '<div class="motor-range"><span>' + lo + '</span><span>' + hi + '</span></div>';
          html += '</div>';
        }
        root.innerHTML = html;
      }

      function showTab(name) {
        var hb = document.getElementById("tabHealthBtn");
        var cb = document.getElementById("tabConfigBtn");
        var hp = document.getElementById("healthPanel");
        var cp = document.getElementById("configPanel");
        hb.classList.toggle("active", name==="health");
        cb.classList.toggle("active", name==="config");
        hp.classList.toggle("active", name==="health");
        cp.classList.toggle("active", name==="config");
      }

      function setConfigStatus(msg, isError) {
        var n = document.getElementById("configStatus");
        n.textContent = msg;
        n.classList.toggle("ok", !isError);
        n.classList.toggle("bad", !!isError);
      }

      function getSpecByPath(path) {
        if (!configState.schema || !configState.schema.categories) return null;
        for (var i=0; i<configState.schema.categories.length; i++) {
          var cat = configState.schema.categories[i];
          if (!cat.settings) continue;
          for (var j=0; j<cat.settings.length; j++) {
            if (cat.settings[j].path === path) return cat.settings[j];
          }
        }
        return null;
      }

      function comparableValue(v, spec) {
        if (!spec) return normalizeScalar(v);
        if (spec.value_type === "bool") return asBool(v) ? "true" : "false";
        return normalizeScalar(v);
      }

      function setPendingValue(path, value) {
        var spec = getSpecByPath(path);
        if (!spec) return;
        if (comparableValue(value, spec) === comparableValue(spec.current_value, spec)) {
          delete configState.pending[path];
        } else {
          configState.pending[path] = value;
        }
      }

      function renderConfigCategoryTabs() {
        var root = document.getElementById("configCategoryTabs");
        if (!configState.schema || !configState.schema.categories || !configState.schema.categories.length) { root.innerHTML = ""; return; }
        root.innerHTML = configState.schema.categories.map(function(cat) {
          var active = cat.id === configState.selectedCategoryId;
          return '<button type="button" class="tab-btn ' + (active?"active":"") + '" data-category-id="' + esc(cat.id) + '">' + esc(cat.label) + '</button>';
        }).join("");
      }

      function renderConfigRows() {
        var root = document.getElementById("configRows");
        if (!configState.schema || !configState.schema.categories || !configState.schema.categories.length) {
          root.innerHTML = "<tr><td colspan='5' class='meta'>No configurator schema.</td></tr>"; return;
        }
        var cat = null;
        for (var i=0; i<configState.schema.categories.length; i++) {
          if (configState.schema.categories[i].id === configState.selectedCategoryId) { cat = configState.schema.categories[i]; break; }
        }
        if (!cat) cat = configState.schema.categories[0];
        if (!cat || !cat.settings || !cat.settings.length) {
          root.innerHTML = "<tr><td colspan='5' class='meta'>No settings.</td></tr>"; return;
        }
        root.innerHTML = cat.settings.map(function(s) {
          var path = String(s.path || "");
          var pending = configState.pending.hasOwnProperty(path);
          var curVal = pending ? configState.pending[path] : s.current_value;
          var source = pending ? "pending" : (s.current_source || "default");
          var vt = String(s.value_type || "str");
          var editor = "";
          if (vt === "bool") {
            editor = '<input class="cfg-check" type="checkbox" data-config-path="'+esc(path)+'" '+(asBool(curVal)?"checked":"")+'>';
          } else if (vt === "enum") {
            var opts = (s.choices||[]).map(function(c) { var cv=normalizeScalar(c); return '<option value="'+esc(cv)+'" '+(cv===normalizeScalar(curVal)?"selected":"")+'>'+esc(cv)+'</option>'; }).join("");
            editor = '<select class="cfg-select" data-config-path="'+esc(path)+'">'+opts+'</select>';
          } else {
            var it = vt==="secret"?"password":(vt==="int"||vt==="float"?"number":"text");
            var step = vt==="float"?"any":(vt==="int"?"1":"");
            var minA = s.min_value!=null?' min="'+esc(s.min_value)+'"':"";
            var maxA = s.max_value!=null?' max="'+esc(s.max_value)+'"':"";
            var stepA = step?' step="'+step+'"':"";
            editor = '<input class="cfg-input" type="'+it+'" data-config-path="'+esc(path)+'" value="'+esc(normalizeScalar(curVal))+'"'+minA+maxA+stepA+'>';
          }
          var rb = s.restart_required ? "<span class='badge'>restart</span>" : "";
          var rc = pending ? "cfg-row pending" : "cfg-row";
          return '<tr class="'+rc+'"><td><div><strong>'+esc(s.label||s.id||path)+'</strong>'+rb+'</div><div class="cfg-path"><code>'+esc(path)+'</code></div></td><td>'+editor+'</td><td class="cfg-source">'+esc(vt)+'</td><td class="cfg-source">'+esc(source)+'</td><td>'+esc(s.description||"")+'</td></tr>';
        }).join("");
      }

      function renderConfigPanel() {
        renderConfigCategoryTabs();
        renderConfigRows();
        var pc = Object.keys(configState.pending).length;
        if (pc > 0) setConfigStatus(pc + " pending change(s)", false);
      }

      function fetchJson(url, timeout, done) {
        var xhr = new XMLHttpRequest();
        var finished = false;
        function finish(err, data) { if (finished) return; finished = true; done(err, data); }
        try { xhr.open("GET", url, true); } catch(e) { finish(e, null); return; }
        xhr.timeout = timeout || 5000;
        xhr.onreadystatechange = function() {
          if (xhr.readyState !== 4) return;
          try { finish(null, JSON.parse(xhr.responseText)); } catch(e) { finish(e, null); }
        };
        xhr.onerror = function() { finish(new Error("network error"), null); };
        xhr.ontimeout = function() { finish(new Error("timeout"), null); };
        try { xhr.send(); } catch(e) { finish(e, null); }
      }

      function refreshDashboard() {
        if (polling) return;
        polling = true;
        var marker = document.getElementById("refreshState");
        if (marker) marker.textContent = "updating...";
        var t0 = Date.now();
        fetchJson("/dashboard/data?t="+Date.now(), 5000, function(err, data) {
          polling = false;
          if (err || !data || data.status !== "success") {
            if (marker) marker.textContent = "update error";
            return;
          }
          renderMotors(data.motor_state || {}, data.motor_ranges || {});
          var setText = function(id, v) { var n = document.getElementById(id); if (n) n.textContent = String(v); };
          var serial = data.serial || {};
          var serialNode = document.getElementById("serialState");
          if (serialNode) {
            serialNode.textContent = serial.connected ? "Serial connected" : "Serial disconnected";
            serialNode.className = serial.connected ? "ok" : "bad";
          }
          setText("serialPort", serial.device || "N/A");
          setText("baudrate", serial.baudrate || "--");
          setText("serialStatus", serial.connected ? "Connected" : "Disconnected");
          setText("sessionsActive", data.sessions_active || 0);
          setText("commandsServed", data.commands_served || 0);
          setText("requestsServed", data.requests_served || 0);
          setText("uptime", fmtUptime(data.uptime_seconds || 0));
          var tun = data.tunnel || {};
          setText("tunnelStatus", tun.url ? "Active" : (tun.running ? "Starting..." : "Inactive"));
          if (marker) marker.textContent = "updated " + new Date().toLocaleTimeString() + " (" + (Date.now()-t0) + "ms)";
        });
      }

      function refreshHealth() {
        fetchJson("/health?t="+Date.now(), 5000, function(err, data) {
          var out = document.getElementById("healthOut");
          if (out) out.textContent = err ? String(err) : JSON.stringify(data, null, 2);
        });
      }

      function authenticate() {
        var pw = document.getElementById("password").value.trim();
        if (!pw) { alert("Enter password first."); return; }
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/auth", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function() {
          if (xhr.readyState !== 4) return;
          try {
            var d = JSON.parse(xhr.responseText);
            if (d.status === "success") {
              sessionKey = d.session_key;
              document.getElementById("statusLine").textContent = "Authenticated. Timeout " + d.timeout + "s";
              loadConfigSchema();
            } else {
              document.getElementById("statusLine").textContent = "Auth failed: " + (d.message || xhr.status);
            }
          } catch(e) { document.getElementById("statusLine").textContent = "Auth error: " + e; }
        };
        xhr.send(JSON.stringify({password: pw}));
      }

      function serialReset() {
        if (!sessionKey) { document.getElementById("statusLine").textContent = "Authenticate first."; return; }
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/serial_reset", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function() {
          if (xhr.readyState !== 4) return;
          try {
            var d = JSON.parse(xhr.responseText);
            document.getElementById("statusLine").textContent = d.message || d.status;
          } catch(e) { document.getElementById("statusLine").textContent = "Reset error: " + e; }
        };
        xhr.send(JSON.stringify({session_key: sessionKey, trigger_home: true}));
      }

      function loadConfigSchema() {
        fetchJson("/config/schema?t="+Date.now(), 5000, function(err, data) {
          if (err || !data || data.status !== "success") {
            setConfigStatus(err ? String(err) : (data && data.message || "Schema load failed"), true);
            return;
          }
          configState.schema = data.config || {categories:[]};
          configState.pending = {};
          var cats = configState.schema.categories || [];
          configState.selectedCategoryId = cats.length ? String(cats[0].id) : "";
          renderConfigPanel();
          setConfigStatus("Configurator loaded.", false);
        });
      }

      function saveConfigChanges() {
        var paths = Object.keys(configState.pending);
        if (!paths.length) { setConfigStatus("No pending changes.", false); return; }
        if (!sessionKey) { setConfigStatus("Authenticate first to save.", true); return; }
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/config/save", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function() {
          if (xhr.readyState !== 4) return;
          try {
            var d = JSON.parse(xhr.responseText);
            if (d.status === "success") {
              loadConfigSchema();
              var note = d.restart_required ? " Restart required." : "";
              setConfigStatus("Saved " + (d.saved_paths ? d.saved_paths.length : paths.length) + " change(s)." + note, false);
            } else {
              setConfigStatus("Save failed: " + (d.errors ? JSON.stringify(d.errors) : (d.message || xhr.status)), true);
            }
          } catch(e) { setConfigStatus("Save error: " + e, true); }
        };
        xhr.send(JSON.stringify({session_key: sessionKey, changes: configState.pending}));
      }

      /* event delegation for config inputs */
      document.addEventListener("input", function(e) {
        var path = e.target.getAttribute("data-config-path");
        if (!path) return;
        if (e.target.type === "checkbox") { setPendingValue(path, e.target.checked); }
        else { setPendingValue(path, e.target.value); }
        renderConfigPanel();
      });
      document.addEventListener("click", function(e) {
        var catId = e.target.getAttribute("data-category-id");
        if (catId) { configState.selectedCategoryId = catId; renderConfigPanel(); }
      });

      document.getElementById("connectBtn").addEventListener("click", authenticate);
      document.getElementById("refreshNowBtn").addEventListener("click", function() { refreshDashboard(); refreshHealth(); });
      document.getElementById("serialResetBtn").addEventListener("click", serialReset);
      document.getElementById("tabHealthBtn").addEventListener("click", function() { showTab("health"); });
      document.getElementById("tabConfigBtn").addEventListener("click", function() { showTab("config"); });
      document.getElementById("configReloadBtn").addEventListener("click", loadConfigSchema);
      document.getElementById("configSaveBtn").addEventListener("click", saveConfigChanges);
      document.getElementById("configDiscardBtn").addEventListener("click", function() { configState.pending = {}; renderConfigPanel(); setConfigStatus("Discarded.", false); });

      showTab("health");
      refreshDashboard();
      refreshHealth();
      loadConfigSchema();
      setInterval(refreshDashboard, 1000);
      setInterval(refreshHealth, 3000);
    </script>
  </body>
</html>
"""

    @app.before_request
    def _count_requests():
        request_count["value"] += 1

    @app.route("/", methods=["GET"])
    def index():
        return render_template_string(
            ADAPTER_INDEX_HTML,
            listen_route=listen_route,
            listen_port=listen_port,
        )

    @app.route("/health", methods=["GET"])
    def health():
        payload = {}
        with sessions_lock:
            session_count = len(sessions)
        serial_connected = bool(ser is not None and getattr(ser, "is_open", False))
        serial_detected_device_key = _normalize_device_key(
            serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
        )
        process_running = tunnel_process is not None and tunnel_process.poll() is None
        current_tunnel = None
        current_error = ""
        try:
            payload = _build_adapter_discovery_payload()
            tunnel_payload = payload.get("tunnel", {}) if isinstance(payload, dict) else {}
            current_tunnel_raw = tunnel_payload.get("tunnel_url")
            current_tunnel = str(current_tunnel_raw or "").strip() if current_tunnel_raw is not None else None
            current_error = str(tunnel_payload.get("error") or "").strip()
        except Exception as exc:
            current_error = str(exc)
            log(f"[HEALTH] discovery payload error: {current_error}")
        return jsonify(
            {
                "status": "ok",
                "service": "adapter",
                "uptime_seconds": round(time.time() - started_at, 2),
                "require_auth": bool(runtime_security.get("require_auth", True)),
                "tunnel_running": process_running,
                "tunnel_error": current_error,
                "tunnel_url": current_tunnel if process_running else None,
                "serial_connected": serial_connected,
                "serial_device": serial_device or "",
                "baudrate": int(baudrate),
                "serial_expected_device_key": serial_expected_device_key,
                "serial_detected_device_key": serial_detected_device_key,
                "serial_health": dict(serial_health_report) if isinstance(serial_health_report, dict) else {},
                "sessions_active": session_count,
                "commands_served": int(command_count["value"]),
                "requests_served": int(request_count["value"]),
            }
        )

    @app.route("/dashboard", methods=["GET"])
    def dashboard_page():
        return index()

    @app.route("/dashboard/data", methods=["GET"])
    def dashboard_data():
        """Live dashboard data: motor states, serial status, metrics."""
        serial_connected = bool(ser is not None and getattr(ser, "is_open", False))
        with sessions_lock:
            session_count = len(sessions)
        process_running = tunnel_process is not None and tunnel_process.poll() is None
        with tunnel_url_lock:
            current_tunnel = tunnel_url if process_running else None

        return jsonify({
            "status": "success",
            "timestamp_ms": int(time.time() * 1000),
            "uptime_seconds": round(time.time() - started_at, 2),
            "motor_state": dict(current_state),
            "motor_ranges": {k: {"min": v[0], "max": v[1], "type": v[2].__name__} for k, v in allowed_ranges.items()},
            "serial": {
                "connected": serial_connected,
                "device": serial_device or "",
                "baudrate": int(baudrate),
                "expected_device_key": serial_expected_device_key,
                "detected_device_key": _normalize_device_key(
                    serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
                ),
                "health": dict(serial_health_report) if isinstance(serial_health_report, dict) else {},
            },
            "network": {
                "listen_host": listen_host,
                "listen_port": int(listen_port),
                "listen_route": listen_route,
            },
            "tunnel": {
                "running": process_running,
                "url": current_tunnel or "",
            },
            "sessions_active": session_count,
            "commands_served": int(command_count["value"]),
            "requests_served": int(request_count["value"]),
        })

    @app.route("/config/schema", methods=["GET"])
    def config_schema():
        """Return the adapter configurator schema with current values."""
        payload, status_code = _adapter_config_schema_payload(load_config())
        return jsonify(payload), status_code

    @app.route("/config/save", methods=["POST"])
    def config_save():
        """Save adapter configuration changes."""
        if not _config_spec_available():
            return jsonify({"status": "error", "message": "Configurator unavailable"}), 503

        data = request.get_json() or {}
        session_key = data.get("session_key", "")
        if not validate_session(session_key):
            return jsonify({"status": "error", "message": "Invalid or expired session"}), 401

        changes = data.get("changes", {})
        if not isinstance(changes, dict) or not changes:
            return jsonify({"status": "error", "message": "No config changes provided"}), 400

        spec = _build_adapter_config_spec()
        if spec is None:
            return jsonify({"status": "error", "message": "Configurator spec unavailable"}), 503

        path_to_spec = {}
        for category in spec.categories:
            for setting in category.settings:
                path_to_spec[setting.path] = setting

        coerced_changes = {}
        errors = {}
        restart_required = False
        for path, raw_value in changes.items():
            path = str(path or "").strip()
            if not path:
                continue
            setting_spec = path_to_spec.get(path)
            if setting_spec is None:
                errors[path] = "Unknown setting path"
                continue
            try:
                coerced_value = _coerce_config_value(raw_value, setting_spec)
                coerced_changes[path] = coerced_value
                if setting_spec.restart_required:
                    restart_required = True
            except ValueError as exc:
                errors[path] = str(exc)

        if errors:
            return jsonify({"status": "error", "message": "Validation failed", "errors": errors}), 400

        if not coerced_changes:
            return jsonify({"status": "error", "message": "No valid config changes provided"}), 400

        config_data = load_config()
        for path, value in coerced_changes.items():
            _set_nested(config_data, path, value)
        _load_adapter_settings(config_data)
        save_config(config_data)
        runtime_apply = _apply_runtime_config(config_data)

        return jsonify({
            "status": "success",
            "message": "Config saved",
            "saved_paths": sorted(coerced_changes.keys()),
            "restart_required": restart_required,
            "runtime_apply": runtime_apply,
        })

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

        endpoints = _network_endpoints()
        local_base = endpoints["local_base"]
        bind_base = endpoints["bind_base"]
        lan_base = endpoints["lan_base"]
        local_http = endpoints["local_http"]
        local_ws = endpoints["local_ws"]
        lan_http = endpoints["lan_http"]
        lan_ws = endpoints["lan_ws"]
        fallback_payload = _adapter_fallback_payload(current_tunnel, process_running)
        selected_transport = str(fallback_payload.get("selected_transport") or "local").strip().lower()
        upnp_payload = fallback_payload.get("upnp", {}) if isinstance(fallback_payload, dict) else {}
        upnp_base = str((upnp_payload or {}).get("public_base_url") or "").strip()
        upnp_http = str((upnp_payload or {}).get("http_endpoint") or "").strip()
        upnp_ws = str((upnp_payload or {}).get("ws_endpoint") or "").strip()
        tunnel_http = f"{current_tunnel}{listen_route}" if current_tunnel else ""
        tunnel_ws = f"{current_tunnel.replace('https://', 'wss://')}/ws" if current_tunnel else ""
        effective_base = current_tunnel or (upnp_base if selected_transport == "upnp" else "") or lan_base or local_base

        tunnel_state = "active" if (process_running and current_tunnel) else ("starting" if process_running else "inactive")
        if stale_tunnel and not process_running:
            tunnel_state = "stale"
        if current_error and not process_running and not current_tunnel and not stale_tunnel:
            tunnel_state = "error"

        local_payload = {
            "base_url": local_base,
            "listen_host": listen_host,
            "listen_port": int(listen_port),
            "command_route": listen_route,
            "auth_route": "/auth",
            "ws_path": "/ws",
            "health_url": f"{local_base}/health",
            "dashboard_url": f"{local_base}/",
            "http_endpoint": local_http,
            "ws_endpoint": local_ws,
        }
        if bind_base and bind_base != local_base:
            local_payload["bind_base_url"] = bind_base
        if lan_base:
            local_payload["lan_base_url"] = lan_base
            local_payload["lan_dashboard_url"] = f"{lan_base}/"
            local_payload["lan_http_endpoint"] = lan_http
            local_payload["lan_ws_endpoint"] = lan_ws

        return {
            "service": "adapter",
            "transport": selected_transport,
            "base_url": effective_base,
            "http_endpoint": tunnel_http or upnp_http or lan_http or local_http,
            "ws_endpoint": tunnel_ws or upnp_ws or lan_ws or local_ws,
            "serial": {
                "connected": bool(ser is not None and getattr(ser, "is_open", False)),
                "device": serial_device or "",
                "baudrate": int(baudrate),
                "expected_device_key": serial_expected_device_key,
                "detected_device_key": _normalize_device_key(
                    serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
                ),
                "health": dict(serial_health_report) if isinstance(serial_health_report, dict) else {},
            },
            "local": local_payload,
            "tunnel": {
                "state": tunnel_state,
                "tunnel_url": current_tunnel,
                "http_endpoint": tunnel_http,
                "ws_endpoint": tunnel_ws,
                "stale_tunnel_url": stale_tunnel,
                "error": current_error,
            },
            "fallback": fallback_payload,
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
        tunnel_payload = payload.get("tunnel", {}) if isinstance(payload, dict) else {}
        current_tunnel = str(tunnel_payload.get("tunnel_url") or "").strip()
        stale_tunnel = str(tunnel_payload.get("stale_tunnel_url") or "").strip()
        current_error = str(tunnel_payload.get("error") or "").strip()

        if current_tunnel:
            payload["status"] = "success"
            payload["message"] = "Tunnel URL available"
        elif stale_tunnel:
            payload["status"] = "error"
            tunnel_payload["error"] = current_error or "Tunnel URL expired"
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
            "serial_expected_device_key": serial_expected_device_key,
            "serial_detected_device_key": _normalize_device_key(
                serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
            ),
            "serial_health": dict(serial_health_report) if isinstance(serial_health_report, dict) else {},
            "home_sent": home_sent,
        }
        return jsonify(response)

    @app.route("/serial_retest", methods=["POST"])
    def http_serial_retest():
        """Retest serial ports using HEALTH discovery and bind the first matching controller."""
        data = request.get_json() or {}
        session_key = data.get("session_key", "")

        if not validate_session(session_key):
            return jsonify({"status": "error", "message": "Invalid or expired session"}), 401

        ok, message = retest_serial_ports()
        if not ok:
            return jsonify({"status": "error", "message": message}), 500

        response = {
            "status": "success",
            "message": message,
            "serial_device": serial_device,
            "baudrate": int(baudrate),
            "serial_expected_device_key": serial_expected_device_key,
            "serial_detected_device_key": _normalize_device_key(
                serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
            ),
            "serial_health": dict(serial_health_report) if isinstance(serial_health_report, dict) else {},
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

    def fallback_refresh_loop():
        last_upnp_url = ""
        while service_running.is_set():
            process_running = tunnel_process is not None and tunnel_process.poll() is None
            with tunnel_url_lock:
                current_tunnel = tunnel_url if process_running else ""
            if not current_tunnel:
                upnp_state = _refresh_upnp_fallback(force=False)
                if str(upnp_state.get("state") or "").strip().lower() == "active":
                    upnp_url = str(upnp_state.get("public_base_url") or "").strip()
                    if upnp_url and upnp_url != last_upnp_url:
                        log(f"[FALLBACK] UPnP adapter endpoint ready: {upnp_url}")
                        last_upnp_url = upnp_url
            time.sleep(max(15.0, float(UPNP_FALLBACK_REFRESH_SECONDS)))

    _refresh_upnp_fallback(force=True)
    threading.Thread(target=fallback_refresh_loop, daemon=True).start()

    # --- Startup Log & Run ---
    startup_endpoints = _network_endpoints()
    local_base = startup_endpoints["local_base"]
    lan_base = startup_endpoints["lan_base"]
    local_http = startup_endpoints["local_http"]
    local_ws = startup_endpoints["local_ws"]
    lan_http = startup_endpoints["lan_http"]

    log(f"Adapter dashboard: {local_base}/")
    log(f"Local health URL: {local_base}/health")
    log(f"Local command URL: {local_http}")
    if lan_base:
        log(f"LAN dashboard: {lan_base}/")
        log(f"LAN command URL: {lan_http}")
    log(f"Bind host: {listen_host}:{listen_port}")
    if enable_tunnel:
        log("Cloudflare Tunnel will be available shortly...")
        log("Remote URL will be displayed once tunnel is established.")

    # Update initial metrics
    if ui:
        ui.update_metric("Serial Port", serial_device or "N/A")
        ui.update_metric("Baudrate", str(baudrate))
        ui.update_metric("Expected Device", serial_expected_device_key or "N/A")
        ui.update_metric(
            "Detected Device",
            _normalize_device_key(
                serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
            )
            or "N/A",
        )
        ui.update_metric("Local URL", local_base)
        ui.update_metric("LAN URL", lan_base or "N/A")
        ui.update_metric("HTTP Endpoint", local_http)
        ui.update_metric("WebSocket", local_ws)
        ui.update_metric("Session Timeout (s)", str(SESSION_TIMEOUT))
        ui.update_metric("Sessions", "0")
        ui.update_metric("Commands", "0")
        ui.update_metric("Tunnel Status", "Starting..." if enable_tunnel else "Disabled")

    # Metrics update thread
    restart_state = {"requested": False, "reason": ""}
    restart_lock = Lock()

    def update_metrics_loop():
        while ui and ui.running:
            with sessions_lock:
                session_count = len(sessions)
            ui.update_metric("Sessions", str(session_count))
            ui.update_metric("Commands", str(command_count["value"]))
            ui.update_metric("Serial Port", serial_device or "N/A")
            ui.update_metric("Baudrate", str(baudrate))
            ui.update_metric("Expected Device", serial_expected_device_key or "N/A")
            ui.update_metric(
                "Detected Device",
                _normalize_device_key(
                    serial_health_report.get("DEVICE") if isinstance(serial_health_report, dict) else ""
                )
                or "N/A",
            )
            ui.update_metric(
                "Serial",
                "Connected" if (ser is not None and getattr(ser, "is_open", False)) else "Disconnected",
            )

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
                allow_unsafe_werkzeug=True,
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
            socketio.run(app, host=listen_host, port=listen_port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
        finally:
            service_running.clear()
            stop_cloudflared_tunnel()
if __name__ == "__main__":
    main()
