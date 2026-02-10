#!/usr/bin/env python
"""
This is a self-contained Flask web application for controlling a Stewart platform (neck)
via serial commands. It supports multiple control modes:

  1. Direct Motor Control: Individual motor commands (e.g., "1:30,2:45,...").
  2. Euler Control: Control yaw (X), pitch (Y), roll (Z) and height (H) (e.g., "X30,Y15,Z-10,H50").
  3. Full Head Control: Control yaw (X), lateral translation (Y), front/back (Z),
     height (H), speed multiplier (S), acceleration multiplier (A), roll (R) and pitch (P)
     (e.g., "X30,Y0,Z10,H-40,S1,A1,R0,P0").
  4. Quaternion Control: Control orientation via quaternion (w, x, y, z) plus optional
     speed (S) and acceleration (A) multipliers, and a height value (e.g., "Q:1,0,0,0,H50,S1,A1").

Additionally, a "HOME" command is supported to re-home the platform.

Before any control pages are available the user is presented with a Connect to Neck
page where a serial port is selected.

All pages use a darkmode interface with background #111 and text #FFFAFA. All content is
centered in a 1024pxwide container, using flexbox with a 0.5rem gap. Buttons are outlined
with #FFFAFA, have 0.5rem padding and 0.25rem borderradius. A footer console displays every
serial command sent.

The fonts are imported from Google Fonts.
 
Before any pipimported modules are loaded, the script checks for (and if needed creates) a virtual
environment so that Flask and pyserial are installed automatically.
"""

import os
import sys
import subprocess
import threading
import re
import platform
import time
import json
import socket
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

# ---------- VENV SETUP ----------
APP_VENV_DIR_NAME = "app_venv"
APP_CLOUDFLARED_BASENAME = "app_cloudflared"


def in_virtualenv(target_prefix=None):
    in_venv = sys.prefix != sys.base_prefix
    if not target_prefix:
        return in_venv
    return in_venv and os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(
        os.path.abspath(target_prefix)
    )

script_dir = os.path.dirname(os.path.abspath(__file__))
venv_dir = os.path.join(script_dir, APP_VENV_DIR_NAME)

if not in_virtualenv(venv_dir):
    if os.name == "nt":
        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_exe = os.path.join(venv_dir, "bin", "pip")
        python_exe = os.path.join(venv_dir, "bin", "python")

    # Create venv if it doesn't exist
    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment at '{APP_VENV_DIR_NAME}'...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        print("Installing required packages (Flask, pyserial)...")
        subprocess.check_call([pip_exe, "install", "Flask", "pyserial"])
    else:
        # Venv exists - check if packages are installed
        try:
            result = subprocess.run(
                [python_exe, "-c", "import flask"],
                capture_output=True,
                timeout=5
            )
            if result.returncode != 0:
                print("Installing missing packages...")
                subprocess.check_call([pip_exe, "install", "Flask", "pyserial"])
        except:
            print("Installing required packages (Flask, pyserial)...")
            subprocess.check_call([pip_exe, "install", "Flask", "pyserial"])

    print("Restarting script inside virtual environment...")
    os.execv(python_exe, [python_exe] + sys.argv)
# ---------- End VENV SETUP ----------

from flask import Flask, render_template_string, redirect, url_for, jsonify

# ---------- Configuration ----------
CONFIG_PATH = "config.json"
DEFAULT_ADAPTER_WS_URL = os.environ.get("ADAPTER_WS_URL", "ws://127.0.0.1:5001/ws")
DEFAULT_ADAPTER_HTTP_URL = os.environ.get("ADAPTER_HTTP_URL", "http://127.0.0.1:5001/send_command")
DEFAULT_APP_HOST = "0.0.0.0"
DEFAULT_APP_PORT = 5000
DEFAULT_APP_ENABLE_TUNNEL = True
DEFAULT_APP_AUTO_INSTALL_CLOUDFLARED = True
WEBSOCKET_URL = DEFAULT_ADAPTER_WS_URL
ADAPTER_HTTP_URL = DEFAULT_ADAPTER_HTTP_URL

# --- Cloudflare Tunnel ---
tunnel_url = None
tunnel_url_lock = Lock()
tunnel_process = None

# ---------- Flask Application Setup ----------
app = Flask(__name__)

# ---------- Config Helpers ----------
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
    except OSError as exc:
        log(f"Failed to save config: {exc}")


def _load_app_settings(config):
    """Resolve app settings and promote them into app.* nested paths."""
    changed = False

    def promote(path, value):
        nonlocal changed
        current = _get_nested(config, path, _MISSING)
        if current is _MISSING or current != value:
            _set_nested(config, path, value)
            changed = True

    websocket_url = str(
        _read_config_value(
            config,
            "app.adapter.websocket_url",
            DEFAULT_ADAPTER_WS_URL,
            legacy_keys=("websocket_url", "ADAPTER_WS_URL"),
        )
    ).strip() or DEFAULT_ADAPTER_WS_URL
    promote("app.adapter.websocket_url", websocket_url)

    http_url = str(
        _read_config_value(
            config,
            "app.adapter.http_url",
            DEFAULT_ADAPTER_HTTP_URL,
            legacy_keys=("http_url", "ADAPTER_HTTP_URL"),
        )
    ).strip() or DEFAULT_ADAPTER_HTTP_URL
    promote("app.adapter.http_url", http_url)

    listen_host = str(
        _read_config_value(
            config,
            "app.server.host",
            DEFAULT_APP_HOST,
            legacy_keys=("frontend_host", "host"),
        )
    ).strip() or DEFAULT_APP_HOST
    promote("app.server.host", listen_host)

    listen_port = _as_int(
        _read_config_value(
            config,
            "app.server.port",
            DEFAULT_APP_PORT,
            legacy_keys=("frontend_port", "port"),
        ),
        DEFAULT_APP_PORT,
        minimum=1,
        maximum=65535,
    )
    promote("app.server.port", listen_port)

    enable_tunnel = _as_bool(
        _read_config_value(
            config,
            "app.tunnel.enable",
            DEFAULT_APP_ENABLE_TUNNEL,
            legacy_keys=("enable_tunnel",),
        ),
        default=DEFAULT_APP_ENABLE_TUNNEL,
    )
    promote("app.tunnel.enable", enable_tunnel)

    auto_install_cloudflared = _as_bool(
        _read_config_value(
            config,
            "app.tunnel.auto_install_cloudflared",
            DEFAULT_APP_AUTO_INSTALL_CLOUDFLARED,
            legacy_keys=("auto_install_cloudflared",),
        ),
        default=DEFAULT_APP_AUTO_INSTALL_CLOUDFLARED,
    )
    promote("app.tunnel.auto_install_cloudflared", auto_install_cloudflared)

    return {
        "websocket_url": websocket_url,
        "http_url": http_url,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "enable_tunnel": enable_tunnel,
        "auto_install_cloudflared": auto_install_cloudflared,
    }, changed


def _build_app_config_spec():
    if not UI_AVAILABLE:
        return None
    return ConfigSpec(
        label="Neck Frontend",
        categories=(
            CategorySpec(
                id="adapter",
                label="Adapter",
                settings=(
                    SettingSpec(
                        id="websocket_url",
                        label="Adapter WS URL",
                        path="app.adapter.websocket_url",
                        value_type="str",
                        default=DEFAULT_ADAPTER_WS_URL,
                        description="Default adapter WebSocket endpoint used by all pages.",
                    ),
                    SettingSpec(
                        id="http_url",
                        label="Adapter HTTP URL",
                        path="app.adapter.http_url",
                        value_type="str",
                        default=DEFAULT_ADAPTER_HTTP_URL,
                        description="Default adapter HTTP command endpoint.",
                    ),
                ),
            ),
            CategorySpec(
                id="server",
                label="Server",
                settings=(
                    SettingSpec(
                        id="listen_host",
                        label="Listen Host",
                        path="app.server.host",
                        value_type="str",
                        default=DEFAULT_APP_HOST,
                        description="Bind host for frontend Flask app.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="listen_port",
                        label="Listen Port",
                        path="app.server.port",
                        value_type="int",
                        default=DEFAULT_APP_PORT,
                        min_value=1,
                        max_value=65535,
                        description="Bind port for frontend Flask app.",
                        restart_required=True,
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
                        path="app.tunnel.enable",
                        value_type="bool",
                        default=DEFAULT_APP_ENABLE_TUNNEL,
                        description="Enable Cloudflare Tunnel for remote frontend access.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="auto_install_cloudflared",
                        label="Auto-install Cloudflared",
                        path="app.tunnel.auto_install_cloudflared",
                        value_type="bool",
                        default=DEFAULT_APP_AUTO_INSTALL_CLOUDFLARED,
                        description="Install cloudflared automatically when missing.",
                        restart_required=True,
                    ),
                ),
            ),
        ),
    )

# ---------- Cloudflared Installation ----------
def get_cloudflared_path():
    """Get the path to cloudflared binary."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.name == 'nt':
        return os.path.join(script_dir, f"{APP_CLOUDFLARED_BASENAME}.exe")
    else:
        return os.path.join(script_dir, APP_CLOUDFLARED_BASENAME)

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
    if ui:
        log("Installing cloudflared...")
    else:
        print("Installing cloudflared...")
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
        msg = f" Unsupported platform: {system} {machine}"
        if ui:
            log(msg)
        else:
            print(msg)
        return False

    try:
        import urllib.request
        msg = f"Downloading cloudflared..."
        if ui:
            log(msg)
        else:
            print(msg)
        urllib.request.urlretrieve(url, cloudflared_path)

        # Make executable on Unix-like systems
        if os.name != 'nt':
            os.chmod(cloudflared_path, 0o755)

        msg = "[OK] Cloudflared installed successfully"
        if ui:
            log(msg)
        else:
            print(msg)
        return True
    except Exception as e:
        msg = f"[ERROR] Failed to install cloudflared: {e}"
        if ui:
            log(msg)
        else:
            print(msg)
        return False

def start_cloudflared_tunnel(local_port):
    """Start cloudflared tunnel in background and capture the URL."""
    global tunnel_url, tunnel_process

    cloudflared_path = get_cloudflared_path()
    if not os.path.exists(cloudflared_path):
        # Try using cloudflared from PATH
        cloudflared_path = "cloudflared"

    url = f"http://localhost:{local_port}"

    try:
        log("[START] Starting Cloudflare Tunnel for frontend...")
        process = subprocess.Popen(
            [cloudflared_path, "tunnel", "--url", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        tunnel_process = process

        # Start a thread to monitor output and capture the URL
        def monitor_tunnel():
            global tunnel_url
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if line:
                    # Look for the tunnel URL in the output
                    if "trycloudflare.com" in line or "https://" in line:
                        # Extract URL using regex
                        url_match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
                        if url_match:
                            with tunnel_url_lock:
                                if tunnel_url is None:
                                    tunnel_url = url_match.group(0)
                                    log("")
                                    log("=" * 60)
                                    log(f"[TUNNEL] Frontend Cloudflare Tunnel URL: {tunnel_url}")
                                    log("=" * 60)
                                    log("")
                                    log(f"Access your frontend remotely at:")
                                    log(f"  {tunnel_url}")
                                    log("")

        thread = threading.Thread(target=monitor_tunnel, daemon=True)
        thread.start()

        return True
    except Exception as e:
        log(f"[ERROR] Failed to start cloudflared tunnel: {e}")
        return False

# ---------- Base CSS and JavaScript (Dark Mode, Flexbox Layout) ----------
base_css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Exo:ital,wght@0,100..900;1,100..900&family=Monomaniac+One&family=Oxanium:wght@200..800&family=Roboto+Mono:ital,wght@0,100..700;1,100..700&display=swap');

:root {
    --bg-primary: #222222;
    --bg-secondary: #2a2a2a;
    --bg-tertiary: #1a1a1a;
    --text-primary: #ffffff;
    --accent: #ffae00;
    --border-light: #333333;
    --border-dark: #111111;
}

body {
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: 'Roboto Mono', monospace;
    margin: 0;
    padding: 0;
}

.container {
    width: 1024px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 1rem;
}

/* Modal Styles */
.modal {
    display: none;
    position: fixed;
    z-index: 1000;
    left: 0;
    top: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.8);
    align-items: center;
    justify-content: center;
}

.modal.active {
    display: flex;
}

.modal-content {
    background: var(--bg-primary);
    border: 2px solid var(--border-light);
    border-radius: 0.5rem;
    padding: 2rem;
    max-width: 500px;
    width: 90%;
}

.modal-header {
    font-size: 1.5rem;
    margin-bottom: 1rem;
    color: var(--accent);
}

.modal-section {
    background: var(--bg-tertiary);
    border: 2px solid var(--border-dark);
    border-radius: 0.5rem;
    padding: 1rem;
    margin-bottom: 1rem;
}

/* Metrics Display */
.metrics-bar {
    display: flex;
    gap: 1rem;
    padding: 0.75rem;
    background: var(--bg-tertiary);
    border: 2px solid var(--border-dark);
    border-radius: 0.5rem;
    margin-bottom: 0.5rem;
}

.metric {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
}

.metric-label {
    font-size: 0.75rem;
    opacity: 0.7;
}

.metric-value {
    font-weight: bold;
    color: var(--accent);
}

.metric-value.good {
    color: #00ff88;
}

.metric-value.warning {
    color: #ffae00;
}

.metric-value.error {
    color: #ff4444;
}

/* Navigation styling */
nav {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0;
    margin: 0;
}

.nav-container {
    display: flex;
    flex-direction: row;
    gap: 0.5rem;
    align-items: center;
    width: 100%;
    flex-wrap: wrap;
}

.nav-link {
    color: var(--text-primary);
    text-decoration: none;
    border: 2px solid var(--border-light);
    padding: 0.5rem 1rem;
    border-radius: 0.5rem;
    transition: all 0.2s;
}

.nav-link:hover {
    background: var(--accent);
    color: var(--bg-primary);
    border-color: var(--accent);
}

.nav-button {
    background: var(--accent);
    border: 2px solid var(--accent);
    color: var(--bg-primary);
    padding: 0.5rem 1rem;
    border-radius: 0.5rem;
    cursor: pointer;
    font-weight: bold;
    transition: all 0.2s;
}

.nav-button:hover {
    background: #ffcc00;
    border-color: #ffcc00;
}

.row {
    display: flex;
    flex-direction: row;
    gap: 0.5rem;
    align-items: center;
}

.column {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.control-section {
    background: var(--bg-tertiary);
    border: 2px solid var(--border-dark);
    border-radius: 0.5rem;
    padding: 1rem;
    margin-bottom: 0.5rem;
}

button {
    background: var(--bg-secondary);
    border: 2px solid var(--border-light);
    color: var(--text-primary);
    padding: 0.5rem 1rem;
    border-radius: 0.5rem;
    cursor: pointer;
    transition: all 0.2s;
}

button:hover {
    background: var(--accent);
    color: var(--bg-primary);
    border-color: var(--accent);
}

button.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--bg-primary);
    font-weight: bold;
}

button.primary:hover {
    background: #ffcc00;
    border-color: #ffcc00;
}

input[type="number"],
input[type="text"],
input[type="password"],
input[type="range"],
select {
    background: var(--bg-secondary);
    border: 2px solid var(--border-light);
    color: var(--text-primary);
    padding: 0.5rem;
    border-radius: 0.5rem;
}

input[type="number"]:focus,
input[type="text"]:focus,
input[type="password"]:focus,
select:focus {
    outline: none;
    border-color: var(--accent);
}

input[type="range"] {
    flex: 1;
}

label {
    min-width: 120px;
}

footer {
    background: var(--bg-tertiary);
    border: 2px solid var(--border-dark);
    padding: 0.5rem;
    font-size: 0.8rem;
    overflow-y: auto;
    max-height: 150px;
    border-radius: 0.5rem;
    margin-top: 1rem;
}

h1, h2 {
    color: var(--accent);
}
</style>
"""

base_js = r"""
<script>
// Define PI if you need quaternion math.
const PI = Math.PI;

// Defaults injected from backend config
const SERVER_DEFAULT_WS_URL = {{ ws_url | tojson }};
const SERVER_DEFAULT_HTTP_URL = {{ http_url | tojson }};

// Connection state - no auto-fill, only use saved or query params
let WS_URL = localStorage.getItem('wsUrl') || "";
let HTTP_URL = localStorage.getItem('httpUrl') || "";
if (WS_URL === SERVER_DEFAULT_WS_URL) {
    WS_URL = "";
}
if (HTTP_URL === SERVER_DEFAULT_HTTP_URL) {
    HTTP_URL = "";
}
let SESSION_KEY = localStorage.getItem('sessionKey') || "";
let PASSWORD = localStorage.getItem('password') || "";
let socket = null;
let useWS = false;
let authenticated = false;
let suppressCommandDispatch = false;

// Metrics tracking
let metrics = {
    connected: false,
    lastPing: 0,
    latency: 0,
    commandsSent: 0,
    dataRate: 0,
    lastCommandTime: 0
};

// Common logger for the footer console.
function logToConsole(msg) {
    const consoleEl = document.getElementById('console');
    if (consoleEl) {
        const line = document.createElement('div');
        line.textContent = msg;
        consoleEl.appendChild(line);
        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
}

// Update metrics display
function updateMetrics() {
    const statusEl = document.getElementById('metricStatus');
    const latencyEl = document.getElementById('metricLatency');
    const rateEl = document.getElementById('metricRate');

    if (statusEl) {
        if (metrics.connected) {
            statusEl.textContent = useWS ? 'WebSocket' : 'HTTP';
            statusEl.className = 'metric-value good';
        } else {
            statusEl.textContent = 'Disconnected';
            statusEl.className = 'metric-value error';
        }
    }

    if (latencyEl) {
        latencyEl.textContent = metrics.latency + 'ms';
        latencyEl.className = 'metric-value ' + (metrics.latency < 100 ? 'good' : metrics.latency < 300 ? 'warning' : 'error');
    }

    if (rateEl) {
        rateEl.textContent = metrics.dataRate.toFixed(1) + ' cmd/s';
        rateEl.className = 'metric-value';
    }
}

// Calculate data rate
setInterval(() => {
    const now = Date.now();
    const elapsed = (now - metrics.lastCommandTime) / 1000;
    if (elapsed > 2) {
        metrics.dataRate = 0;
    }
    updateMetrics();
}, 1000);

// All your original defaults:
const DEFAULTS = {
    'motor': 0,'yaw': 0,'pitch': 0,'roll': 0,'height': 0,
    'X': 0,'Y': 0,'Z': 0,'H': 0,'S': 1,'A': 1,'R': 0,'P': 0,
    'w': 1,'x': 0,'y': 0,'z': 0,'qH': 0,'qS': 1,'qA': 1
};

// Reset sliders/inputs back to defaults and clear the command display.
function resetSliders(options = {}) {
    const silent = !!options.silent;
    const previousSuppress = suppressCommandDispatch;
    if (silent) {
        suppressCommandDispatch = true;
    }
    try {
        document.querySelectorAll("input[type='number'], input[type='range']").forEach(input => {
            for (let k in DEFAULTS) {
                if (input.id.startsWith(k)) {
                    input.value = DEFAULTS[k];
                    input.dispatchEvent(new Event('change'));
                    break;
                }
            }
        });
        const cur = document.getElementById('currentCmd');
        if (cur) cur.textContent = "";
    } finally {
        suppressCommandDispatch = previousSuppress;
    }
}

// Send HOME command and then reset UI.
function sendHomeCommand() {
    sendCommand("HOME_BRUTE");
    resetSliders({silent: true});
    logToConsole("Sent HOME_BRUTE command");
}

// Send soft HOME command and then reset UI.
function sendHomeSoftCommand() {
    sendCommand("HOME_SOFT");
    resetSliders({silent: true});
    logToConsole("Sent HOME_SOFT command");
}

// Authenticate with adapter
async function authenticate(password, wsUrl, httpUrl) {
    try {
        let authUrl;
        try {
            const parsedHttpUrl = new URL(httpUrl.includes("://") ? httpUrl : `https://${httpUrl}`);
            authUrl = `${parsedHttpUrl.origin}/auth`;
        } catch (urlErr) {
            logToConsole("[ERROR] Invalid HTTP URL: " + httpUrl);
            return false;
        }
        const startTime = Date.now();
        const response = await fetch(authUrl, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: password})
        });
        const data = await response.json();

        if (data.status === 'success') {
            SESSION_KEY = data.session_key;
            PASSWORD = password;
            localStorage.setItem('sessionKey', SESSION_KEY);
            localStorage.setItem('password', password);
            localStorage.setItem('wsUrl', wsUrl);
            localStorage.setItem('httpUrl', httpUrl);

            metrics.latency = Date.now() - startTime;
            metrics.connected = true;
            authenticated = true;

            logToConsole("[OK] Authenticated successfully");
            updateMetrics();
            return true;
        } else {
            logToConsole("[ERROR] Authentication failed: " + data.message);
            return false;
        }
    } catch (err) {
        logToConsole("[ERROR] Authentication error: " + err);
        return false;
    }
}

// Centralized sendCommand: whichever path is currently active.
function sendCommand(command) {
    if (suppressCommandDispatch) {
        return;
    }

    const startTime = Date.now();
    metrics.commandsSent++;

    if (!SESSION_KEY) {
        logToConsole("[ERROR] No session key - please authenticate first");
        showConnectionModal();
        return;
    }

    if (useWS && socket && socket.connected) {
        socket.emit('message', {command: command, session_key: SESSION_KEY});
        logToConsole("WS -> " + command);

        const elapsed = (Date.now() - metrics.lastCommandTime) / 1000;
        if (elapsed > 0) {
            metrics.dataRate = 1 / elapsed;
        }
        metrics.lastCommandTime = Date.now();
        metrics.latency = Date.now() - startTime;
        updateMetrics();
    } else {
        // If WS not yet open (or closed), do HTTP POST
        if (!HTTP_URL) {
            logToConsole("[ERROR] No HTTP URL configured");
            showConnectionModal();
            return;
        }

        fetch(HTTP_URL, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({command: command, session_key: SESSION_KEY})
        })
        .then(r => {
            metrics.latency = Date.now() - startTime;
            return r.json();
        })
        .then(data => {
            logToConsole("HTTP -> " + command);
            if (data.status !== 'success') {
                logToConsole("[ERROR] " + (data.message || JSON.stringify(data)));
                if (data.message && data.message.includes('session')) {
                    SESSION_KEY = "";
                    localStorage.removeItem('sessionKey');
                    showConnectionModal();
                }
            }
            const elapsed = (Date.now() - metrics.lastCommandTime) / 1000;
            if (elapsed > 0) {
                metrics.dataRate = 1 / elapsed;
            }
            metrics.lastCommandTime = Date.now();
            updateMetrics();
        })
        .catch(err => {
            logToConsole("[ERROR] Fetch error: " + err);
            metrics.connected = false;
            updateMetrics();
        });
    }
}



// Initialize the Socket.IO connection.
function initWebSocket() {
  if (!SESSION_KEY) {
    logToConsole("[WARN] Cannot connect to WebSocket without session key");
    return;
  }

  try {
    // Extract base URL from WS_URL (remove /ws path)
    const wsBase = WS_URL.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:').replace(/\/ws$/, '');

    // Connect using Socket.IO client
    socket = io(wsBase, {
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      reconnectionAttempts: 5
    });

    socket.on('connect', () => {
      logToConsole("[OK] Socket.IO connected - authenticating...");
      socket.emit('authenticate', {session_key: SESSION_KEY});
    });

    socket.on('message', (data) => {
      try {
        const parsed = typeof data === 'string' ? JSON.parse(data) : data;
        if (parsed.status === 'authenticated') {
          useWS = true;
          metrics.connected = true;
          logToConsole("[OK] WS authenticated - now using WebSocket");
          updateMetrics();
          hideConnectionModal();
        } else if (parsed.status === 'error') {
          logToConsole("[ERROR] WS error: " + parsed.message);
          if (parsed.message && parsed.message.includes('session')) {
            SESSION_KEY = "";
            localStorage.removeItem('sessionKey');
            showConnectionModal();
          }
        } else {
          logToConsole("WS <- " + JSON.stringify(parsed));
        }
      } catch (err) {
        logToConsole("WS <- " + data);
      }
    });

    socket.on('disconnect', () => {
      useWS = false;
      metrics.connected = false;
      logToConsole("[WARN] Socket.IO disconnected - falling back to HTTP");
      updateMetrics();
    });

    socket.on('connect_error', (err) => {
      useWS = false;
      metrics.connected = false;
      logToConsole("[ERROR] Socket.IO connection error - falling back to HTTP");
      updateMetrics();
    });

  } catch (err) {
    console.warn("Socket.IO init failed:", err);
    logToConsole("[ERROR] Socket.IO init failed: " + err);
  }
}

// Show/hide connection modal
function showConnectionModal() {
  const modal = document.getElementById('connectionModal');
  if (modal) {
    modal.classList.add('active');
    ensureEndpointInputBindings();
    // Pre-fill only saved values, no defaults
    const passInput = document.getElementById('passwordInput');
    const wsInput = document.getElementById('wsUrlInput');
    const httpInput = document.getElementById('httpUrlInput');

    if (passInput) passInput.value = PASSWORD || '';
    if (wsInput) wsInput.value = WS_URL || '';
    if (httpInput) httpInput.value = HTTP_URL || '';
    hydrateEndpointInputs("http");
  }
}

function hideConnectionModal() {
  const modal = document.getElementById('connectionModal');
  if (modal) {
    modal.classList.remove('active');
  }
}

let endpointInputBindingsInstalled = false;
let endpointHydrateTimer = null;

function buildAdapterEndpoints(baseInput) {
  if (!baseInput) {
    return null;
  }

  const raw = baseInput.trim();
  if (!raw) {
    return null;
  }

  const candidate = raw.includes("://") ? raw : `https://${raw}`;
  let adapterUrl;
  try {
    adapterUrl = new URL(candidate);
  } catch (err) {
    return null;
  }

  let defaultHttpPath = "/send_command";
  let defaultWsPath = "/ws";
  try {
    defaultHttpPath = new URL(SERVER_DEFAULT_HTTP_URL).pathname || "/send_command";
  } catch (err) {}
  try {
    defaultWsPath = new URL(SERVER_DEFAULT_WS_URL).pathname || "/ws";
  } catch (err) {}

  const baseProtocol = adapterUrl.protocol === "wss:"
    ? "https:"
    : adapterUrl.protocol === "ws:"
      ? "http:"
      : adapterUrl.protocol;
  const baseOrigin = `${baseProtocol}//${adapterUrl.host}`;
  const httpUrl = `${baseOrigin}${defaultHttpPath}`;
  const wsProtocol = baseProtocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProtocol}//${adapterUrl.host}${defaultWsPath}`;
  return { httpUrl, wsUrl, origin: baseOrigin };
}

function hydrateEndpointInputs(prefer = "http") {
  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");
  if (!httpInput || !wsInput) {
    return null;
  }

  const httpRaw = httpInput.value.trim();
  const wsRaw = wsInput.value.trim();
  const source = prefer === "ws" ? (wsRaw || httpRaw) : (httpRaw || wsRaw);
  const endpoints = buildAdapterEndpoints(source);
  if (!endpoints) {
    return null;
  }

  httpInput.value = endpoints.httpUrl;
  wsInput.value = endpoints.wsUrl;
  return endpoints;
}

function ensureEndpointInputBindings() {
  if (endpointInputBindingsInstalled) {
    return;
  }

  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");
  if (!httpInput || !wsInput) {
    return;
  }

  endpointInputBindingsInstalled = true;

  const hydrateFromHttp = () => {
    if (httpInput.value.trim()) {
      hydrateEndpointInputs("http");
    }
  };
  const hydrateFromWs = () => {
    if (wsInput.value.trim()) {
      hydrateEndpointInputs("ws");
    }
  };
  const scheduleHydrate = (prefer) => {
    if (endpointHydrateTimer) {
      clearTimeout(endpointHydrateTimer);
    }
    endpointHydrateTimer = setTimeout(() => hydrateEndpointInputs(prefer), 120);
  };

  httpInput.addEventListener("input", () => scheduleHydrate("http"));
  httpInput.addEventListener("blur", hydrateFromHttp);
  httpInput.addEventListener("change", hydrateFromHttp);
  httpInput.addEventListener("paste", () => setTimeout(hydrateFromHttp, 0));

  wsInput.addEventListener("input", () => scheduleHydrate("ws"));
  wsInput.addEventListener("blur", hydrateFromWs);
  wsInput.addEventListener("change", hydrateFromWs);
  wsInput.addEventListener("paste", () => setTimeout(hydrateFromWs, 0));
}

// Fill HTTP/WS inputs from a provided adapter/tunnel URL.
function fetchTunnelUrl() {
  const endpoints = hydrateEndpointInputs("http");
  if (!endpoints) {
    alert("Enter a valid adapter URL first (for example https://example.trycloudflare.com).");
    return;
  }

  logToConsole("Adapter endpoints filled from: " + endpoints.origin);
}

// Handle connection form submission
async function connectToAdapter() {
  const password = document.getElementById('passwordInput').value.trim();
  const httpInputEl = document.getElementById('httpUrlInput');
  const wsInputEl = document.getElementById('wsUrlInput');
  const httpInputRaw = httpInputEl ? httpInputEl.value.trim() : "";
  const wsInputRaw = wsInputEl ? wsInputEl.value.trim() : "";

  if (!password || (!httpInputRaw && !wsInputRaw)) {
    alert("Please enter password and adapter URL");
    return;
  }

  const normalized = hydrateEndpointInputs("http");
  if (!normalized) {
    alert("Please enter a valid adapter URL");
    return;
  }

  const httpUrl = normalized.httpUrl;
  const wsUrl = normalized.wsUrl;

  logToConsole("[CONNECT] Connecting to adapter...");

  // Authenticate first
  const success = await authenticate(password, wsUrl, httpUrl);
  if (success) {
    WS_URL = wsUrl;
    HTTP_URL = httpUrl;

    // Try WebSocket if URL provided
    if (wsUrl) {
      initWebSocket();
    } else {
      hideConnectionModal();
    }
  }
}

// Parse query parameters for adapter URL
function parseConnectionFromQuery() {
  const urlParams = new URLSearchParams(window.location.search);
  const adapterParam = (urlParams.get('adapter') || "").trim();
  const passwordParam = (urlParams.get('password') || "").trim();

  let adapterConfigured = false;
  let passwordProvided = false;

  if (adapterParam) {
    const endpoints = buildAdapterEndpoints(adapterParam);
    if (endpoints) {
      localStorage.setItem('httpUrl', endpoints.httpUrl);
      localStorage.setItem('wsUrl', endpoints.wsUrl);
      HTTP_URL = endpoints.httpUrl;
      WS_URL = endpoints.wsUrl;
      adapterConfigured = true;
      logToConsole(`[OK] Adapter configured from URL: ${endpoints.origin}`);
    } else {
      console.error('Invalid adapter URL in query parameter:', adapterParam);
      logToConsole('[WARN] Invalid adapter URL in query parameter');
    }
  }

  if (passwordParam) {
    PASSWORD = passwordParam;
    localStorage.setItem('password', PASSWORD);
    passwordProvided = true;
  }

  return { adapterConfigured, passwordProvided };
}

window.addEventListener('load', async () => {
  ensureEndpointInputBindings();
  const queryConnection = parseConnectionFromQuery();

  if (queryConnection.adapterConfigured && queryConnection.passwordProvided) {
    logToConsole("[CONNECT] Adapter and password found in query; attempting auto-connect...");
    const autoConnected = await authenticate(PASSWORD, WS_URL, HTTP_URL);
    if (autoConnected) {
      if (WS_URL) {
        initWebSocket();
      } else {
        hideConnectionModal();
      }
      return;
    }
    showConnectionModal();
    return;
  }

  // Check if we have a valid session
  if (SESSION_KEY && HTTP_URL) {
    logToConsole("[SESSION] Found saved session, attempting to reconnect...");
    metrics.connected = true;
    authenticated = true;
    updateMetrics();

    // Try WebSocket if configured
    if (WS_URL) {
      initWebSocket();
    }
  } else if (queryConnection.adapterConfigured) {
    // We have adapter URL but no session - show connection modal
    logToConsole("[CONNECT] Adapter URL configured, please authenticate...");
    showConnectionModal();
  } else {
    // Show connection modal on first load
    showConnectionModal();
  }
});
</script>
"""


# Navigation block using only divs and flexbox.
nav_html = """
<nav>
  <div class="nav-container">
    <a href="/connect" class="nav-link">Connect</a>
    <a href="/home" class="nav-link">Home</a>
    <a href="/direct" class="nav-link">Direct Motor</a>
    <a href="/euler" class="nav-link">Euler</a>
    <a href="/head" class="nav-link">Full Head</a>
    <a href="/quaternion" class="nav-link">Quaternion</a>
    <a href="/headstream" class="nav-link">Morphtarget</a>
  </div>
  <div class="row">
    <button onclick="sendHomeCommand()" class="nav-button">HOME Brute</button>
    <button onclick="sendHomeSoftCommand()" class="nav-button">HOME Soft</button>
  </div>

</nav>
"""

# ---------- Page Templates ----------
# 1. Connect Page
connect_page = """
<html>
<head>
  <title>Connect to Neck Adapter</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  %%CSS%%
  %%JS%%
</head>
<body>
  <!-- Connection Modal -->
  <div id="connectionModal" class="modal active">
    <div class="modal-content">
      <div class="modal-header">Connect to Neck Adapter</div>

      <div class="modal-section">
        <h3 style="margin-top:0;">Authentication</h3>
        <div class="column">
          <label for="passwordInput">Password:</label>
          <input type="password" id="passwordInput" placeholder="Enter adapter password">
        </div>
      </div>

      <div class="modal-section">
        <h3 style="margin-top:0;">Adapter Endpoints</h3>
        <div class="column">
          <label for="httpUrlInput">HTTP URL:</label>
          <input type="text" id="httpUrlInput">

          <label for="wsUrlInput">WebSocket URL (optional):</label>
          <input type="text" id="wsUrlInput">
        </div>
        <button onclick="fetchTunnelUrl()" style="width:100%;margin-top:0.5rem;">
           Fill Endpoints From Tunnel URL
        </button>
        <p style="font-size:0.85rem;opacity:0.7;margin:0.5rem 0 0 0;">
          Paste a tunnel/base URL, then click above to derive /send_command and /ws endpoints
        </p>
      </div>

      <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;font-size:1.1rem;">
        Connect
      </button>
    </div>
  </div>

  <div class="container">
    <!-- Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric">
        <div class="metric-label">Connection</div>
        <div id="metricStatus" class="metric-value">Checking...</div>
      </div>
      <div class="metric">
        <div class="metric-label">Latency</div>
        <div id="metricLatency" class="metric-value">--</div>
      </div>
      <div class="metric">
        <div class="metric-label">Data Rate</div>
        <div id="metricRate" class="metric-value">0 cmd/s</div>
      </div>
      <div style="margin-left:auto;">
        <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">
          Settings
        </button>
      </div>
    </div>

    <h1>Robotic Neck Control</h1>
    <p>Please configure your connection to the adapter in the modal above.</p>
    <p style="opacity:0.7;font-size:0.9rem;">
      The adapter must be running and accessible at the specified URL.
      Default password is "neck2025" unless changed in adapter config.
    </p>

    <div style="margin-top:2rem;">
      <a href="/home"><button class="primary">Proceed to Controls -></button></a>
    </div>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
connect_page = connect_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js);

# 2. Home Page
home_page = """
<html>
<head>
  <title>Stewart Platform Control Interface</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  %%CSS%%
  %%JS%%
</head>
<body>
  <!-- Connection Modal (hidden by default) -->
  <div id="connectionModal" class="modal">
    <div class="modal-content">
      <div class="modal-header">Reconnect to Adapter</div>

      <div class="modal-section">
        <h3 style="margin-top:0;">Authentication</h3>
        <div class="column">
          <label for="passwordInput">Password:</label>
          <input type="password" id="passwordInput" placeholder="Enter adapter password">
        </div>
      </div>

      <div class="modal-section">
        <h3 style="margin-top:0;">Adapter Endpoints</h3>
        <div class="column">
          <label for="httpUrlInput">HTTP URL:</label>
          <input type="text" id="httpUrlInput">

          <label for="wsUrlInput">WebSocket URL (optional):</label>
          <input type="text" id="wsUrlInput">
        </div>
      </div>

      <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;">
        Connect
      </button>
    </div>
  </div>

  <div class="container">
    <!-- Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric">
        <div class="metric-label">Connection</div>
        <div id="metricStatus" class="metric-value">Checking...</div>
      </div>
      <div class="metric">
        <div class="metric-label">Latency</div>
        <div id="metricLatency" class="metric-value">--</div>
      </div>
      <div class="metric">
        <div class="metric-label">Data Rate</div>
        <div id="metricRate" class="metric-value">0 cmd/s</div>
      </div>
      <div style="margin-left:auto;">
        <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">
          Settings
        </button>
      </div>
    </div>

    %%NAV%%
    <h1>Stewart Platform Control Interface</h1>
    <div class="control-section">
      <p>Select a control mode from the navigation menu above.</p>
      <ul style="line-height:1.8;">
        <li><strong>Direct Motor:</strong> Control each of the 6 actuators individually</li>
        <li><strong>Euler:</strong> Control yaw, pitch, roll, and height</li>
        <li><strong>Full Head:</strong> Complete head control with speed/acceleration</li>
        <li><strong>Quaternion:</strong> Quaternion-based orientation control</li>
        <li><strong>Morphtarget:</strong> Real-time face tracking control</li>
      </ul>
    </div>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
home_page = home_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

# 3. Direct Motor Control Page
direct_page = """
<html>
<head>
  <title>Direct Motor Control</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  %%CSS%%
  %%JS%%
  <script>
    function updateDirect() {
      let cmdParts = [];
      for (let i = 1; i <= 6; i++) {
        let val = document.getElementById('motor' + i).value;
        cmdParts.push(i + ":" + val);
      }
      let cmd = cmdParts.join(",");
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    function incMotor(id) {
      let el = document.getElementById('motor' + id);
      el.value = parseInt(el.value) + 1;
      updateDirect();
    }
    function decMotor(id) {
      let el = document.getElementById('motor' + id);
      el.value = parseInt(el.value) - 1;
      updateDirect();
    }
  </script>
</head>
<body>
  <!-- Connection Modal -->
  <div id="connectionModal" class="modal">
    <div class="modal-content">
      <div class="modal-header">Reconnect to Adapter</div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Authentication</h3>
        <div class="column">
          <label for="passwordInput">Password:</label>
          <input type="password" id="passwordInput">
        </div>
      </div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Adapter Endpoints</h3>
        <div class="column">
          <label for="httpUrlInput">HTTP URL:</label>
          <input type="text" id="httpUrlInput">
          <label for="wsUrlInput">WebSocket URL:</label>
          <input type="text" id="wsUrlInput">
        </div>
        <button onclick="fetchTunnelUrl()" style="width:100%;margin-top:0.5rem;">
           Fill Endpoints From Tunnel URL
        </button>
      </div>
      <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;">Connect</button>
    </div>
  </div>

  <div class="container">
    <!-- Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric">
        <div class="metric-label">Connection</div>
        <div id="metricStatus" class="metric-value">Checking...</div>
      </div>
      <div class="metric">
        <div class="metric-label">Latency</div>
        <div id="metricLatency" class="metric-value">--</div>
      </div>
      <div class="metric">
        <div class="metric-label">Data Rate</div>
        <div id="metricRate" class="metric-value">0 cmd/s</div>
      </div>
      <div style="margin-left:auto;">
        <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">Settings</button>
      </div>
    </div>

    %%NAV%%
    <h2>Direct Motor Control</h2>

    <div class="control-section">
      {% for i in range(1,7) %}
        <div class="row">
          <label>Motor {{ i }}:</label>
          <button onclick="decMotor({{ i }})">-</button>
          <input type="number" id="motor{{ i }}" value="0" onchange="updateDirect()" style="width:80px;">
          <button onclick="incMotor({{ i }})">+</button>
          <input type="range" id="slider{{ i }}" min="0" max="80" value="0"
                 oninput="document.getElementById('motor{{ i }}').value=this.value; updateDirect();">
        </div>
      {% endfor %}
    </div>

    <div class="control-section">
      <h3 style="margin-top:0;">Manual Command</h3>
      <div class="row">
        <input type="text" id="directCmdInput" placeholder="e.g., 1:30,2:45" style="flex:1;">
        <button onclick="sendCommand(document.getElementById('directCmdInput').value)" class="primary">Send</button>
      </div>
      <p style="margin:0.5rem 0 0 0;opacity:0.7;">Current: <span id="currentCmd" style="color:var(--accent);"></span></p>
    </div>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
direct_page = direct_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

# 4. Euler Control Page
euler_page = """
<html>
<head>
  <title>Euler Control</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  %%CSS%%
  %%JS%%
  <script>
    function updateEuler() {
      let yaw = document.getElementById('yaw').value;
      let pitch = document.getElementById('roll').value;
      let roll = document.getElementById('pitch').value;
      let height = document.getElementById('height').value;
      let cmd = "X" + yaw + ",Y" + pitch + ",Z" + roll + ",H" + height;
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    function incField(field) {
      let el = document.getElementById(field);
      el.value = parseInt(el.value) + 1;
      updateEuler();
    }
    function decField(field) {
      let el = document.getElementById(field);
      el.value = parseInt(el.value) - 1;
      updateEuler();
    }
  </script>
</head>
<body>
  <!-- Connection Modal -->
  <div id="connectionModal" class="modal">
    <div class="modal-content">
      <div class="modal-header">Reconnect to Adapter</div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Authentication</h3>
        <div class="column">
          <label for="passwordInput">Password:</label>
          <input type="password" id="passwordInput">
        </div>
      </div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Adapter Endpoints</h3>
        <div class="column">
          <label for="httpUrlInput">HTTP URL:</label>
          <input type="text" id="httpUrlInput">
          <label for="wsUrlInput">WebSocket URL:</label>
          <input type="text" id="wsUrlInput">
        </div>
        <button onclick="fetchTunnelUrl()" style="width:100%;margin-top:0.5rem;">
           Fill Endpoints From Tunnel URL
        </button>
      </div>
      <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;">Connect</button>
    </div>
  </div>

  <div class="container">
    <!-- Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric">
        <div class="metric-label">Connection</div>
        <div id="metricStatus" class="metric-value">Checking...</div>
      </div>
      <div class="metric">
        <div class="metric-label">Latency</div>
        <div id="metricLatency" class="metric-value">--</div>
      </div>
      <div class="metric">
        <div class="metric-label">Data Rate</div>
        <div id="metricRate" class="metric-value">0 cmd/s</div>
      </div>
      <div style="margin-left:auto;">
        <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">Settings</button>
      </div>
    </div>

    %%NAV%%
    <h2>Euler Control</h2>

    <div class="control-section">
      <div class="row">
        <label>Yaw (X):</label>
        <button onclick="decField('yaw')">-</button>
        <input type="number" id="yaw" value="0" onchange="updateEuler()" style="width:80px;">
        <button onclick="incField('yaw')">+</button>
        <input type="range" id="yawSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('yaw').value=this.value; updateEuler();">
      </div>
      <div class="row">
        <label>Roll (Y):</label>
        <button onclick="decField('roll')">-</button>
        <input type="number" id="roll" value="0" onchange="updateEuler()" style="width:80px;">
        <button onclick="incField('roll')">+</button>
        <input type="range" id="rollSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('roll').value=this.value; updateEuler();">
      </div>
      <div class="row">
        <label>Pitch (Z):</label>
        <button onclick="decField('pitch')">-</button>
        <input type="number" id="pitch" value="0" onchange="updateEuler()" style="width:80px;">
        <button onclick="incField('pitch')">+</button>
        <input type="range" id="pitchSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('pitch').value=this.value; updateEuler();">
      </div>
      <div class="row">
        <label>Height (H):</label>
        <button onclick="decField('height')">-</button>
        <input type="number" id="height" value="0" onchange="updateEuler()" style="width:80px;">
        <button onclick="incField('height')">+</button>
        <input type="range" id="heightSlider" min="0" max="70" value="0"
               oninput="document.getElementById('height').value=this.value; updateEuler();">
      </div>
    </div>

    <div class="control-section">
      <h3 style="margin-top:0;">Manual Command</h3>
      <div class="row">
        <input type="text" id="eulerCmdInput" placeholder="e.g., X30,Y15,Z-10,H50" style="flex:1;">
        <button onclick="sendCommand(document.getElementById('eulerCmdInput').value)" class="primary">Send</button>
      </div>
      <p style="margin:0.5rem 0 0 0;opacity:0.7;">Current: <span id="currentCmd" style="color:var(--accent);"></span></p>
    </div>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
euler_page = euler_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);


# 5. Full Head Control Page
head_page = """
<html>
<head>
  <title>Full Head Control</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  %%CSS%%
  %%JS%%
  <script>
    function updateHead() {
      let X = document.getElementById('X').value;
      let Y = document.getElementById('Y').value;
      let Z = document.getElementById('Z').value;
      let H = document.getElementById('H').value;
      let S = document.getElementById('S').value;
      let A = document.getElementById('A').value;
      let R = document.getElementById('R').value;
      let P = document.getElementById('P').value;
      let cmd = "X" + X + ",Y" + Y + ",Z" + Z + ",H" + H + ",S" + S + ",A" + A + ",R" + R + ",P" + P;
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    function incField(field, step) {
      let el = document.getElementById(field);
      if(field === 'S' || field === 'A'){
        el.value = parseFloat(el.value) + step;
      } else {
        el.value = parseInt(el.value) + step;
      }
      updateHead();
    }
    function decField(field, step) {
      let el = document.getElementById(field);
      if(field === 'S' || field === 'A'){
        el.value = parseFloat(el.value) - step;
      } else {
        el.value = parseInt(el.value) - step;
      }
      updateHead();
    }
  </script>
</head>
<body>
  <!-- Connection Modal -->
  <div id="connectionModal" class="modal">
    <div class="modal-content">
      <div class="modal-header">Reconnect to Adapter</div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Authentication</h3>
        <div class="column">
          <label for="passwordInput">Password:</label>
          <input type="password" id="passwordInput">
        </div>
      </div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Adapter Endpoints</h3>
        <div class="column">
          <label for="httpUrlInput">HTTP URL:</label>
          <input type="text" id="httpUrlInput">
          <label for="wsUrlInput">WebSocket URL:</label>
          <input type="text" id="wsUrlInput">
        </div>
        <button onclick="fetchTunnelUrl()" style="width:100%;margin-top:0.5rem;">
           Fill Endpoints From Tunnel URL
        </button>
      </div>
      <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;">Connect</button>
    </div>
  </div>

  <div class="container">
    <!-- Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric">
        <div class="metric-label">Connection</div>
        <div id="metricStatus" class="metric-value">Checking...</div>
      </div>
      <div class="metric">
        <div class="metric-label">Latency</div>
        <div id="metricLatency" class="metric-value">--</div>
      </div>
      <div class="metric">
        <div class="metric-label">Data Rate</div>
        <div id="metricRate" class="metric-value">0 cmd/s</div>
      </div>
      <div style="margin-left:auto;">
        <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">Settings</button>
      </div>
    </div>

    %%NAV%%
    <h2>Full Head Control</h2>

    <div class="control-section">
      <h3 style="margin-top:0;">Position & Orientation</h3>
      <div class="row">
        <label>Yaw (X):</label>
        <button onclick="decField('X', 1)">-</button>
        <input type="number" id="X" value="0" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('X', 1)">+</button>
        <input type="range" id="XSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('X').value=this.value; updateHead();">
      </div>
      <div class="row">
        <label>Lateral (Y):</label>
        <button onclick="decField('Y', 1)">-</button>
        <input type="number" id="Y" value="0" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('Y', 1)">+</button>
        <input type="range" id="YSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('Y').value=this.value; updateHead();">
      </div>
      <div class="row">
        <label>Front/Back (Z):</label>
        <button onclick="decField('Z', 1)">-</button>
        <input type="number" id="Z" value="0" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('Z', 1)">+</button>
        <input type="range" id="ZSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('Z').value=this.value; updateHead();">
      </div>
      <div class="row">
        <label>Height (H):</label>
        <button onclick="decField('H', 1)">-</button>
        <input type="number" id="H" value="0" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('H', 1)">+</button>
        <input type="range" id="HSlider" min="0" max="70" value="0"
               oninput="document.getElementById('H').value=this.value; updateHead();">
      </div>
      <div class="row">
        <label>Roll (R):</label>
        <button onclick="decField('R', 1)">-</button>
        <input type="number" id="R" value="0" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('R', 1)">+</button>
        <input type="range" id="RSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('R').value=this.value; updateHead();">
      </div>
      <div class="row">
        <label>Pitch (P):</label>
        <button onclick="decField('P', 1)">-</button>
        <input type="number" id="P" value="0" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('P', 1)">+</button>
        <input type="range" id="PSlider" min="-800" max="800" value="0"
               oninput="document.getElementById('P').value=this.value; updateHead();">
      </div>
    </div>

    <div class="control-section">
      <h3 style="margin-top:0;">Motion Parameters</h3>
      <div class="row">
        <label>Speed (S):</label>
        <button onclick="decField('S', 0.1)">-</button>
        <input type="number" id="S" value="1" step="0.1" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('S', 0.1)">+</button>
        <input type="range" id="SSlider" min="0" max="10" step="0.1" value="1"
               oninput="document.getElementById('S').value=this.value; updateHead();">
      </div>
      <div class="row">
        <label>Acceleration (A):</label>
        <button onclick="decField('A', 0.1)">-</button>
        <input type="number" id="A" value="1" step="0.1" onchange="updateHead()" style="width:80px;">
        <button onclick="incField('A', 0.1)">+</button>
        <input type="range" id="ASlider" min="0" max="10" step="0.1" value="1"
               oninput="document.getElementById('A').value=this.value; updateHead();">
      </div>
    </div>

    <div class="control-section">
      <h3 style="margin-top:0;">Manual Command</h3>
      <div class="row">
        <input type="text" id="headCmdInput" placeholder="e.g., X30,Y0,Z10,H-40,S1,A1,R0,P0" style="flex:1;">
        <button onclick="sendCommand(document.getElementById('headCmdInput').value)" class="primary">Send</button>
      </div>
      <p style="margin:0.5rem 0 0 0;opacity:0.7;">Current: <span id="currentCmd" style="color:var(--accent);"></span></p>
    </div>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
head_page = head_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

# 6. Quaternion Control Page
quat_page = """
<html>
<head>
  <title>Quaternion Control</title>
  <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
  %%CSS%%
  %%JS%%
  <script>
    function updateQuat() {
      let w = document.getElementById('w').value;
      let x = document.getElementById('x').value;
      let y = document.getElementById('y').value;
      let z = document.getElementById('z').value;
      let h = document.getElementById('qH').value;
      let S = document.getElementById('qS').value;
      let A = document.getElementById('qA').value;
      let cmd = "Q:" + w + "," + x + "," + y + "," + z + ",H" + h;
      if (S !== "") { cmd += ",S" + S; }
      if (A !== "") { cmd += ",A" + A; }
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    function incField(field, step) {
      let el = document.getElementById(field);
      el.value = parseFloat(el.value) + step;
      updateQuat();
    }
    function decField(field, step) {
      let el = document.getElementById(field);
      el.value = parseFloat(el.value) - step;
      updateQuat();
    }
  </script>
</head>
<body>
  <!-- Connection Modal -->
  <div id="connectionModal" class="modal">
    <div class="modal-content">
      <div class="modal-header">Reconnect to Adapter</div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Authentication</h3>
        <div class="column">
          <label for="passwordInput">Password:</label>
          <input type="password" id="passwordInput">
        </div>
      </div>
      <div class="modal-section">
        <h3 style="margin-top:0;">Adapter Endpoints</h3>
        <div class="column">
          <label for="httpUrlInput">HTTP URL:</label>
          <input type="text" id="httpUrlInput">
          <label for="wsUrlInput">WebSocket URL:</label>
          <input type="text" id="wsUrlInput">
        </div>
        <button onclick="fetchTunnelUrl()" style="width:100%;margin-top:0.5rem;">
           Fill Endpoints From Tunnel URL
        </button>
      </div>
      <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;">Connect</button>
    </div>
  </div>

  <div class="container">
    <!-- Metrics Bar -->
    <div class="metrics-bar">
      <div class="metric">
        <div class="metric-label">Connection</div>
        <div id="metricStatus" class="metric-value">Checking...</div>
      </div>
      <div class="metric">
        <div class="metric-label">Latency</div>
        <div id="metricLatency" class="metric-value">--</div>
      </div>
      <div class="metric">
        <div class="metric-label">Data Rate</div>
        <div id="metricRate" class="metric-value">0 cmd/s</div>
      </div>
      <div style="margin-left:auto;">
        <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">Settings</button>
      </div>
    </div>

    %%NAV%%
    <h2>Quaternion Control</h2>

    <div class="control-section">
      <h3 style="margin-top:0;">Quaternion Components</h3>
      <div class="row">
        <label>W:</label>
        <button onclick="decField('w', 0.1)">-</button>
        <input type="number" id="w" value="1" step="0.1" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('w', 0.1)">+</button>
        <input type="range" id="wSlider" min="0" max="1" step="0.01" value="1"
               oninput="document.getElementById('w').value=this.value; updateQuat();">
      </div>
      <div class="row">
        <label>X:</label>
        <button onclick="decField('x', 0.1)">-</button>
        <input type="number" id="x" value="0" step="0.1" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('x', 0.1)">+</button>
        <input type="range" id="xSlider" min="-1" max="1" step="0.01" value="0"
               oninput="document.getElementById('x').value=this.value; updateQuat();">
      </div>
      <div class="row">
        <label>Y:</label>
        <button onclick="decField('y', 0.1)">-</button>
        <input type="number" id="y" value="0" step="0.1" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('y', 0.1)">+</button>
        <input type="range" id="ySlider" min="-1" max="1" step="0.01" value="0"
               oninput="document.getElementById('y').value=this.value; updateQuat();">
      </div>
      <div class="row">
        <label>Z:</label>
        <button onclick="decField('z', 0.1)">-</button>
        <input type="number" id="z" value="0" step="0.1" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('z', 0.1)">+</button>
        <input type="range" id="zSlider" min="-1" max="1" step="0.01" value="0"
               oninput="document.getElementById('z').value=this.value; updateQuat();">
      </div>
    </div>

    <div class="control-section">
      <h3 style="margin-top:0;">Position & Motion</h3>
      <div class="row">
        <label>Height (H):</label>
        <button onclick="decField('qH', 1)">-</button>
        <input type="number" id="qH" value="0" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('qH', 1)">+</button>
        <input type="range" id="qHSlider" min="0" max="70" value="0"
               oninput="document.getElementById('qH').value=this.value; updateQuat();">
      </div>
      <div class="row">
        <label>Speed (S):</label>
        <button onclick="decField('qS', 0.1)">-</button>
        <input type="number" id="qS" value="1" step="0.1" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('qS', 0.1)">+</button>
        <input type="range" id="qSSlider" min="0" max="10" step="0.1" value="1"
               oninput="document.getElementById('qS').value=this.value; updateQuat();">
      </div>
      <div class="row">
        <label>Acceleration (A):</label>
        <button onclick="decField('qA', 0.1)">-</button>
        <input type="number" id="qA" value="1" step="0.1" onchange="updateQuat()" style="width:80px;">
        <button onclick="incField('qA', 0.1)">+</button>
        <input type="range" id="qASlider" min="0" max="10" step="0.1" value="1"
               oninput="document.getElementById('qA').value=this.value; updateQuat();">
      </div>
    </div>

    <div class="control-section">
      <h3 style="margin-top:0;">Manual Command</h3>
      <div class="row">
        <input type="text" id="quatCmdInput" placeholder="e.g., Q:1,0,0,0,H50,S1,A1" style="flex:1;">
        <button onclick="sendCommand(document.getElementById('quatCmdInput').value)" class="primary">Send</button>
      </div>
      <p style="margin:0.5rem 0 0 0;opacity:0.7;">Current: <span id="currentCmd" style="color:var(--accent);"></span></p>
    </div>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
quat_page = quat_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);
headstream_page = r"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <title>Morphtarget Stream</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    %%CSS%%
    %%JS%%
    <style>
      #morphCanvasWrap {
        width: 100%;
        height: 420px;
        border: 2px solid var(--border-light);
        border-radius: 0.5rem;
        overflow: hidden;
        background: #101010;
      }

      #morphCanvasWrap canvas {
        width: 100%;
        height: 100%;
        display: block;
      }

      .stream-grid {
        display: grid;
        grid-template-columns: 220px 1fr;
        gap: 0.75rem;
        margin-top: 0.75rem;
      }

      .stream-card {
        background: var(--bg-secondary);
        border: 2px solid var(--border-light);
        border-radius: 0.5rem;
        padding: 0.65rem 0.75rem;
      }

      .stream-label {
        font-size: 0.75rem;
        opacity: 0.75;
        margin-bottom: 0.35rem;
      }

      .stream-value {
        font-family: 'Roboto Mono', monospace;
        color: var(--accent);
        word-break: break-word;
      }

      .tuneables-note {
        opacity: 0.75;
        margin-top: 0;
      }

      @media (max-width: 900px) {
        #morphCanvasWrap {
          height: 300px;
        }

        .stream-grid {
          grid-template-columns: 1fr;
        }
      }
    </style>
    <script type="importmap">
    {
      "imports": {
        "three": "https://unpkg.com/three@0.152.2/build/three.module.js",
        "three/addons/": "https://unpkg.com/three@0.152.2/examples/jsm/"
      }
    }
    </script>
  </head>
  <body>
    <div id="connectionModal" class="modal">
      <div class="modal-content">
        <div class="modal-header">Reconnect to Adapter</div>
        <div class="modal-section">
          <h3 style="margin-top:0;">Authentication</h3>
          <div class="column">
            <label for="passwordInput">Password:</label>
            <input type="password" id="passwordInput" placeholder="Enter adapter password">
          </div>
        </div>
        <div class="modal-section">
          <h3 style="margin-top:0;">Adapter Endpoints</h3>
          <div class="column">
            <label for="httpUrlInput">HTTP URL:</label>
            <input type="text" id="httpUrlInput">

            <label for="wsUrlInput">WebSocket URL (optional):</label>
            <input type="text" id="wsUrlInput">
          </div>
          <button onclick="fetchTunnelUrl()" style="width:100%;margin-top:0.5rem;">
            Fill Endpoints From Tunnel URL
          </button>
        </div>
        <button onclick="connectToAdapter()" class="primary" style="width:100%;padding:1rem;">
          Connect
        </button>
      </div>
    </div>

    <div class="container">
      <div class="metrics-bar">
        <div class="metric">
          <div class="metric-label">Connection</div>
          <div id="metricStatus" class="metric-value">Checking...</div>
        </div>
        <div class="metric">
          <div class="metric-label">Latency</div>
          <div id="metricLatency" class="metric-value">--</div>
        </div>
        <div class="metric">
          <div class="metric-label">Data Rate</div>
          <div id="metricRate" class="metric-value">0 cmd/s</div>
        </div>
        <div style="margin-left:auto;">
          <button onclick="showConnectionModal()" style="padding:0.25rem 0.75rem;font-size:0.85rem;">
            Settings
          </button>
        </div>
      </div>

      %%NAV%%
      <h2>Morphtarget</h2>

      <div class="control-section">
        <h3 style="margin-top:0;">Head Pose Command Stream</h3>
        <p style="opacity:0.75;margin-top:0;">
          Track your face and stream X/Y/Z/H/S/A/R/P commands to the adapter.
        </p>

        <div id="morphCanvasWrap"></div>

        <div class="stream-grid">
          <div class="stream-card">
            <div class="stream-label">Stream Status</div>
            <div id="streamStatus" class="stream-value">Initializing...</div>
          </div>
          <div class="stream-card">
            <div class="stream-label">Last Command</div>
            <div id="commandStream" class="stream-value">Waiting for head pose...</div>
          </div>
        </div>
      </div>

      <div class="control-section">
        <h3 style="margin-top:0;">Morphtarget Tuneables</h3>
        <p class="tuneables-note">These settings apply live while tracking.</p>

        <div class="row" style="margin-bottom:0.5rem;">
          <button id="streamToggleBtn" class="primary">Play Stream</button>
          <button id="recenterPoseBtn">Recenter</button>
          <button id="resetTuneablesBtn">Reset Tuneables</button>
        </div>

        <div class="row">
          <label for="tuneLateralGain">Lateral Gain (Y):</label>
          <input type="number" id="tuneLateralGain" min="0" max="20" step="0.1" value="1.8" style="width:90px;">
          <input type="range" id="tuneLateralGainRange" min="0" max="20" step="0.1" value="1.8">
        </div>
        <div class="row">
          <label for="tuneFrontBackGain">Front/Back Gain (Z):</label>
          <input type="number" id="tuneFrontBackGain" min="0" max="20" step="0.1" value="1.6" style="width:90px;">
          <input type="range" id="tuneFrontBackGainRange" min="0" max="20" step="0.1" value="1.6">
        </div>
        <div class="row">
          <label for="tuneHeightGain">Height Gain (H):</label>
          <input type="number" id="tuneHeightGain" min="0" max="30" step="0.1" value="2.5" style="width:90px;">
          <input type="range" id="tuneHeightGainRange" min="0" max="30" step="0.1" value="2.5">
        </div>
        <div class="row">
          <label for="tuneYawGain">Yaw Gain (X):</label>
          <input type="number" id="tuneYawGain" min="0" max="40" step="0.1" value="4.8" style="width:90px;">
          <input type="range" id="tuneYawGainRange" min="0" max="40" step="0.1" value="4.8">
        </div>
        <div class="row">
          <label for="tunePitchGain">Pitch Gain (P):</label>
          <input type="number" id="tunePitchGain" min="-60" max="0" step="0.1" value="-7" style="width:90px;">
          <input type="range" id="tunePitchGainRange" min="-60" max="0" step="0.1" value="-7">
        </div>
        <div class="row">
          <label for="tuneRollGain">Roll Gain (R):</label>
          <input type="number" id="tuneRollGain" min="-60" max="0" step="0.1" value="-6" style="width:90px;">
          <input type="range" id="tuneRollGainRange" min="-60" max="0" step="0.1" value="-6">
        </div>
        <div class="row">
          <label for="tuneSmoothAlpha">Smoothing (alpha):</label>
          <input type="number" id="tuneSmoothAlpha" min="0.1" max="0.95" step="0.01" value="0.45" style="width:90px;">
          <input type="range" id="tuneSmoothAlphaRange" min="0.1" max="0.95" step="0.01" value="0.45">
        </div>
        <div class="row">
          <label for="tuneIntervalMs">Send Interval (ms):</label>
          <input type="number" id="tuneIntervalMs" min="20" max="200" step="1" value="90" style="width:90px;">
          <input type="range" id="tuneIntervalMsRange" min="20" max="200" step="1" value="90">
        </div>
      </div>
    </div>

    <footer id="console"></footer>

    <script type="module">
      import * as THREE from 'three';
      import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
      import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
      import { KTX2Loader } from 'three/addons/loaders/KTX2Loader.js';
      import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
      import vision from 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.0';

      const { FaceLandmarker, FilesetResolver } = vision;
      const streamStatusEl = document.getElementById('streamStatus');
      const commandStreamEl = document.getElementById('commandStream');
      const viewport = document.getElementById('morphCanvasWrap');

      const COMMAND_INTERVAL_MS = 90;
      let lastCommandSentAt = 0;
      let lastCommandSent = "";
      let faceLandmarker = null;
      let videoReady = false;
      let smoothed = null;
      let poseBaseline = null;
      let lastTrackedPose = null;
      let streamPlaybackEnabled = false;
      let baselinePendingOnPlay = false;

      const defaultTuneables = {
        yawGain: 4.8,
        pitchGain: -7,
        rollGain: -6,
        lateralGain: 1.8,
        frontBackGain: 1.6,
        heightGain: 2.5,
        smoothAlpha: 0.45,
        commandIntervalMs: COMMAND_INTERVAL_MS,
      };
      const tuneables = { ...defaultTuneables };

      function setStreamStatus(message, error = false) {
        streamStatusEl.textContent = message;
        streamStatusEl.style.color = error ? '#ff4444' : 'var(--accent)';
      }

      function clamp(value, min, max) {
        return Math.max(min, Math.min(max, value));
      }

      function normalizeAngleDelta(rad) {
        let wrapped = rad;
        while (wrapped > Math.PI) {
          wrapped -= Math.PI * 2;
        }
        while (wrapped < -Math.PI) {
          wrapped += Math.PI * 2;
        }
        return wrapped;
      }

      function setPoseBaselineFromPose(pose) {
        poseBaseline = { ...pose };
        smoothed = null;
        setStreamStatus('Tracking active (centered)');
        logToConsole('[CAL] Morphtarget centered at current pose');
      }

      function setPoseBaseline(transformObj, euler) {
        setPoseBaselineFromPose({
          x: transformObj.position.x,
          y: transformObj.position.y,
          z: transformObj.position.z,
          yaw: euler.y,
          pitch: euler.x,
          roll: euler.z,
        });
      }

      function updateStreamToggleUi() {
        const toggleBtn = document.getElementById('streamToggleBtn');
        if (!toggleBtn) {
          return;
        }
        toggleBtn.textContent = streamPlaybackEnabled ? 'Pause Stream' : 'Play Stream';
        toggleBtn.className = streamPlaybackEnabled ? '' : 'primary';
      }

      function setStreamPlaybackEnabled(enabled) {
        streamPlaybackEnabled = !!enabled;

        if (streamPlaybackEnabled) {
          baselinePendingOnPlay = true;
          poseBaseline = null;
          smoothed = null;
          lastCommandSent = "";
          lastCommandSentAt = 0;

          if (lastTrackedPose) {
            setPoseBaselineFromPose(lastTrackedPose);
            baselinePendingOnPlay = false;
            commandStreamEl.textContent = "Centered at Play position";
          } else {
            setStreamStatus('Play requested - waiting for face...');
            commandStreamEl.textContent = "Waiting for face to set Play baseline...";
          }
          logToConsole('[STREAM] Morphtarget play');
        } else {
          baselinePendingOnPlay = false;
          poseBaseline = null;
          smoothed = null;
          commandStreamEl.textContent = "Paused - press Play Stream";
          setStreamStatus('Tracking paused');
          logToConsole('[STREAM] Morphtarget paused');
        }

        updateStreamToggleUi();
      }

      function bindTuneablePair(numberId, rangeId, key, integerValue = false) {
        const numberEl = document.getElementById(numberId);
        const rangeEl = document.getElementById(rangeId);
        if (!numberEl || !rangeEl) {
          return;
        }

        const applyRawValue = (rawValue) => {
          const parsed = integerValue ? parseInt(rawValue, 10) : parseFloat(rawValue);
          if (!Number.isFinite(parsed)) {
            return;
          }
          tuneables[key] = parsed;
          numberEl.value = String(parsed);
          rangeEl.value = String(parsed);
        };

        numberEl.addEventListener('input', () => applyRawValue(numberEl.value));
        rangeEl.addEventListener('input', () => applyRawValue(rangeEl.value));
        applyRawValue(numberEl.value || rangeEl.value || String(defaultTuneables[key]));
      }

      function setTuneablesToDefaults() {
        Object.assign(tuneables, defaultTuneables);

        const tuneableInputs = [
          ['tuneLateralGain', 'lateralGain'],
          ['tuneLateralGainRange', 'lateralGain'],
          ['tuneFrontBackGain', 'frontBackGain'],
          ['tuneFrontBackGainRange', 'frontBackGain'],
          ['tuneHeightGain', 'heightGain'],
          ['tuneHeightGainRange', 'heightGain'],
          ['tuneYawGain', 'yawGain'],
          ['tuneYawGainRange', 'yawGain'],
          ['tunePitchGain', 'pitchGain'],
          ['tunePitchGainRange', 'pitchGain'],
          ['tuneRollGain', 'rollGain'],
          ['tuneRollGainRange', 'rollGain'],
          ['tuneSmoothAlpha', 'smoothAlpha'],
          ['tuneSmoothAlphaRange', 'smoothAlpha'],
          ['tuneIntervalMs', 'commandIntervalMs'],
          ['tuneIntervalMsRange', 'commandIntervalMs'],
        ];

        tuneableInputs.forEach(([id, key]) => {
          const inputEl = document.getElementById(id);
          if (inputEl) {
            inputEl.value = String(tuneables[key]);
          }
        });
      }

      function setupTuneablesUi() {
        bindTuneablePair('tuneLateralGain', 'tuneLateralGainRange', 'lateralGain');
        bindTuneablePair('tuneFrontBackGain', 'tuneFrontBackGainRange', 'frontBackGain');
        bindTuneablePair('tuneHeightGain', 'tuneHeightGainRange', 'heightGain');
        bindTuneablePair('tuneYawGain', 'tuneYawGainRange', 'yawGain');
        bindTuneablePair('tunePitchGain', 'tunePitchGainRange', 'pitchGain');
        bindTuneablePair('tuneRollGain', 'tuneRollGainRange', 'rollGain');
        bindTuneablePair('tuneSmoothAlpha', 'tuneSmoothAlphaRange', 'smoothAlpha');
        bindTuneablePair('tuneIntervalMs', 'tuneIntervalMsRange', 'commandIntervalMs', true);

        const streamToggleBtn = document.getElementById('streamToggleBtn');
        if (streamToggleBtn) {
          streamToggleBtn.addEventListener('click', () => {
            setStreamPlaybackEnabled(!streamPlaybackEnabled);
          });
        }

        const recenterBtn = document.getElementById('recenterPoseBtn');
        if (recenterBtn) {
          recenterBtn.addEventListener('click', () => {
            if (!lastTrackedPose) {
              setStreamStatus('Cannot recenter until face is detected', true);
              return;
            }
            setPoseBaselineFromPose(lastTrackedPose);
          });
        }

        const resetBtn = document.getElementById('resetTuneablesBtn');
        if (resetBtn) {
          resetBtn.addEventListener('click', () => {
            setTuneablesToDefaults();
            logToConsole('[CAL] Morphtarget tuneables reset to defaults');
          });
        }

        // Morphtarget starts disconnected from command streaming.
        setStreamPlaybackEnabled(false);
      }

      function sendCommandToNeck(commandStr) {
        if (typeof window.sendCommand === 'function') {
          window.sendCommand(commandStr);
          return;
        }
        console.warn('sendCommand not available, command not sent:', commandStr);
      }

      function buildPoseCommand(transformObj, euler) {
        if (!poseBaseline) {
          return null;
        }

        const deltaX = transformObj.position.x - poseBaseline.x;
        const deltaY = transformObj.position.y - poseBaseline.y;
        const deltaZ = transformObj.position.z - poseBaseline.z;

        const deltaYaw = normalizeAngleDelta(euler.y - poseBaseline.yaw);
        const deltaPitch = normalizeAngleDelta(euler.x - poseBaseline.pitch);
        const deltaRoll = normalizeAngleDelta(euler.z - poseBaseline.roll);

        const rawLateral = -deltaX;
        const rawHeight = deltaY;
        const rawFrontBack = -deltaZ;
        const rawYaw = THREE.MathUtils.radToDeg(deltaYaw);
        const rawPitch = THREE.MathUtils.radToDeg(deltaPitch);
        const rawRoll = THREE.MathUtils.radToDeg(deltaRoll);

        const yawMRaw = rawYaw * tuneables.yawGain;
        const lateralMRaw = rawLateral * tuneables.lateralGain;
        const frontBackMRaw = rawFrontBack * tuneables.frontBackGain;
        const rollMRaw = rawRoll * tuneables.rollGain;
        const pitchMRaw = rawPitch * tuneables.pitchGain;
        const heightRaw = rawHeight * tuneables.heightGain;

        if (!smoothed) {
          smoothed = {
            yaw: yawMRaw,
            lateral: lateralMRaw,
            frontBack: frontBackMRaw,
            roll: rollMRaw,
            pitch: pitchMRaw,
            height: heightRaw,
          };
        }

        const alpha = clamp(tuneables.smoothAlpha, 0.1, 0.95);
        smoothed.yaw = alpha * yawMRaw + (1 - alpha) * smoothed.yaw;
        smoothed.lateral = alpha * lateralMRaw + (1 - alpha) * smoothed.lateral;
        smoothed.frontBack = alpha * frontBackMRaw + (1 - alpha) * smoothed.frontBack;
        smoothed.roll = alpha * rollMRaw + (1 - alpha) * smoothed.roll;
        smoothed.pitch = alpha * pitchMRaw + (1 - alpha) * smoothed.pitch;
        smoothed.height = alpha * heightRaw + (1 - alpha) * smoothed.height;

        const magnitude = Math.max(
          Math.abs(smoothed.yaw),
          Math.abs(smoothed.pitch),
          Math.abs(smoothed.roll),
          Math.abs(smoothed.lateral),
          Math.abs(smoothed.frontBack)
        );

        const sDynamic = clamp(2 - (magnitude / 600), 1, 2);
        const aDynamic = clamp(1.2 - (0.8 * (magnitude / 600)), 0.5, 1.2);

        const xVal = Math.round(clamp(smoothed.yaw, -700, 700));
        const yVal = Math.round(clamp(smoothed.lateral, -700, 700));
        const zVal = Math.round(clamp(smoothed.frontBack, -700, 700));
        const hVal = Math.round(clamp(smoothed.height, 0, 70));
        const rVal = Math.round(clamp(smoothed.roll, -700, 700));
        const pVal = Math.round(clamp(smoothed.pitch, -700, 700));

        return `X${xVal},Y${yVal},Z${zVal},H${hVal},S${sDynamic.toFixed(1)},A${aDynamic.toFixed(1)},R${rVal},P${pVal}`;
      }

      function resizeViewport(renderer, camera) {
        const width = Math.max(320, viewport.clientWidth || 960);
        const height = Math.max(220, viewport.clientHeight || 420);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
      }

      async function main() {
        setStreamStatus('Starting camera...');
        setupTuneablesUi();

        const renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setPixelRatio(window.devicePixelRatio);
        viewport.appendChild(renderer.domElement);

        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0x111111);

        const camera = new THREE.PerspectiveCamera(60, 1, 1, 100);
        camera.position.z = 3.8;

        const controls = new OrbitControls(camera, renderer.domElement);
        controls.enableZoom = false;
        controls.enableRotate = false;
        controls.enablePan = false;

        resizeViewport(renderer, camera);
        window.addEventListener('resize', () => resizeViewport(renderer, camera));

        const grpTransform = new THREE.Group();
        grpTransform.name = 'grp_transform';
        scene.add(grpTransform);

        const video = document.createElement('video');
        video.autoplay = true;
        video.playsInline = true;

        try {
          const stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: 'user' }
          });
          video.srcObject = stream;
          await video.play();
          videoReady = true;
          setStreamStatus('Camera ready');
        } catch (err) {
          console.error('Camera error:', err);
          setStreamStatus('Camera unavailable', true);
          return;
        }

        const gltfLoader = new GLTFLoader();
        const ktx2Loader = new KTX2Loader()
          .setTranscoderPath('https://unpkg.com/three@0.152.2/examples/jsm/libs/basis/')
          .detectSupport(renderer);
        gltfLoader.setKTX2Loader(ktx2Loader);
        gltfLoader.setMeshoptDecoder(MeshoptDecoder);
        gltfLoader.load(
          'https://threejs.org/examples/models/gltf/facecap.glb',
          (gltf) => {
            const mesh = gltf.scene.children[0];
            grpTransform.add(mesh);
            const headMesh = mesh.getObjectByName('mesh_2');
            if (headMesh) {
              headMesh.material = new THREE.MeshNormalMaterial();
            }
          },
          undefined,
          (error) => {
            console.error('Error loading facecap model:', error);
          }
        );

        try {
          const filesetResolver = await FilesetResolver.forVisionTasks(
            'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.0/wasm'
          );
          faceLandmarker = await FaceLandmarker.createFromOptions(filesetResolver, {
            baseOptions: {
              modelAssetPath: 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
              delegate: 'GPU'
            },
            runningMode: 'VIDEO',
            numFaces: 1,
            outputFaceBlendshapes: true,
            outputFacialTransformationMatrixes: true
          });
          setStreamStatus('Tracking active');
        } catch (err) {
          console.error('MediaPipe init error:', err);
          setStreamStatus('Tracking init failed', true);
          return;
        }

        const transformObj = new THREE.Object3D();

        function animate() {
          requestAnimationFrame(animate);

          if (videoReady && faceLandmarker && video.readyState >= video.HAVE_ENOUGH_DATA) {
            const now = Date.now();
            const results = faceLandmarker.detectForVideo(video, now);

            if (results.facialTransformationMatrixes.length > 0) {
              const matrixArray = results.facialTransformationMatrixes[0].data;
              transformObj.matrix.fromArray(matrixArray);
              transformObj.matrix.decompose(
                transformObj.position,
                transformObj.quaternion,
                transformObj.scale
              );

              const euler = new THREE.Euler().setFromQuaternion(transformObj.quaternion, 'YXZ');
              lastTrackedPose = {
                x: transformObj.position.x,
                y: transformObj.position.y,
                z: transformObj.position.z,
                yaw: euler.y,
                pitch: euler.x,
                roll: euler.z,
              };

              // Keep the rendered model in camera-space using absolute pose
              // while command generation remains baseline-relative.
              grpTransform.position.x = transformObj.position.x / 10;
              grpTransform.position.y = transformObj.position.y / 10;
              grpTransform.position.z = -transformObj.position.z / -10 + 4;
              grpTransform.rotation.x = euler.x;
              grpTransform.rotation.y = euler.y;
              grpTransform.rotation.z = euler.z;

              if (streamPlaybackEnabled && baselinePendingOnPlay) {
                setPoseBaselineFromPose(lastTrackedPose);
                baselinePendingOnPlay = false;
                commandStreamEl.textContent = "Centered at Play position";
              }

              if (streamPlaybackEnabled) {
                const commandStr = buildPoseCommand(transformObj, euler);
                if (commandStr) {
                  commandStreamEl.textContent = commandStr;
                  setStreamStatus('Tracking active');

                  if (commandStr !== lastCommandSent && now - lastCommandSentAt >= tuneables.commandIntervalMs) {
                    sendCommandToNeck(commandStr);
                    lastCommandSent = commandStr;
                    lastCommandSentAt = now;
                  }
                }
              }
            } else {
              if (streamPlaybackEnabled) {
                setStreamStatus('Face not detected');
              }
            }
          }

          controls.update();
          renderer.render(scene, camera);
        }

        animate();
      }

      main();
    </script>
  </body>
</html>
"""
headstream_page = headstream_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);




# ---------- Additional Flask Endpoints ----------
# Note: Direct serial connection removed - now using adapter.py exclusively

# ---------- Main Page Routes ----------

@app.route("/")
def index():
    return redirect(url_for("connect"))

@app.route("/connect")
def connect():
    return render_template_string(connect_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/headstream")
def headstream():
    return render_template_string(headstream_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/home")
def home():
    return render_template_string(home_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/direct")
def direct():
    return render_template_string(direct_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/euler")
def euler():
    return render_template_string(euler_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/head")
def head():
    return render_template_string(head_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/quaternion")
def quaternion():
    return render_template_string(quat_page, ws_url=WEBSOCKET_URL, http_url=ADAPTER_HTTP_URL)

@app.route("/tunnel_info")
def get_tunnel_info():
    """Get the Cloudflare Tunnel URL for the frontend if available."""
    with tunnel_url_lock:
        if tunnel_url:
            return jsonify({
                "status": "success",
                "tunnel_url": tunnel_url,
                "message": "Frontend tunnel URL available"
            })
        else:
            return jsonify({
                "status": "pending",
                "message": "Frontend tunnel URL not yet available"
            })


# ---------- Logging wrapper ----------
def log(message):
    """Log a message to UI or console."""
    if ui and UI_AVAILABLE:
        ui.log(message)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] {message}")


# ---------- Run Flask App ----------
if __name__ == "__main__":
    config = load_config()
    app_settings, config_changed = _load_app_settings(config)

    WEBSOCKET_URL = app_settings["websocket_url"]
    ADAPTER_HTTP_URL = app_settings["http_url"]
    frontend_host = app_settings["listen_host"]
    frontend_port = app_settings["listen_port"]
    enable_tunnel = app_settings["enable_tunnel"]
    auto_install_cloudflared = app_settings["auto_install_cloudflared"]

    if config_changed:
        save_config(config)

    # Initialize UI if available
    if UI_AVAILABLE:
        ui = TerminalUI(
            "Neck Control Frontend",
            config_spec=_build_app_config_spec(),
            config_path=CONFIG_PATH,
        )
        log("Starting Neck Control Frontend...")

        def apply_runtime_settings(saved_config):
            global WEBSOCKET_URL, ADAPTER_HTTP_URL
            WEBSOCKET_URL = str(
                _read_config_value(
                    saved_config,
                    "app.adapter.websocket_url",
                    WEBSOCKET_URL,
                    legacy_keys=("websocket_url", "ADAPTER_WS_URL"),
                )
            ).strip() or DEFAULT_ADAPTER_WS_URL
            ADAPTER_HTTP_URL = str(
                _read_config_value(
                    saved_config,
                    "app.adapter.http_url",
                    ADAPTER_HTTP_URL,
                    legacy_keys=("http_url", "ADAPTER_HTTP_URL"),
                )
            ).strip() or DEFAULT_ADAPTER_HTTP_URL
            ui.update_metric("Adapter WS", WEBSOCKET_URL)
            ui.update_metric("Adapter HTTP", ADAPTER_HTTP_URL)
            ui.log("Applied adapter endpoint updates")

        ui.on_save(apply_runtime_settings)

    # Check if cloudflared is available and start tunnel
    if enable_tunnel:
        if not is_cloudflared_installed():
            if auto_install_cloudflared:
                log("Cloudflared not found, attempting to install...")
                if not install_cloudflared():
                    log("Failed to install cloudflared. Remote access will not be available.")
                    log("You can still use the frontend locally.")
                    enable_tunnel = False
            else:
                log("Cloudflared not found and auto-install is disabled. Tunnel is disabled.")
                enable_tunnel = False

    if enable_tunnel:
        # Start tunnel in background thread (it will capture and display the URL)
        def start_tunnel_delayed():
            time.sleep(2)  # Wait for server to start
            start_cloudflared_tunnel(frontend_port)

        tunnel_thread = threading.Thread(target=start_tunnel_delayed, daemon=True)
        tunnel_thread.start()
        log("Cloudflare Tunnel will be available shortly...")
        log("Remote URL will be displayed once tunnel is established.")

    # Get local URLs for display
    local_url = f"http://{frontend_host}:{frontend_port}"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
        lan_url = f"http://{lan_ip}:{frontend_port}"
    except Exception:
        lan_url = "N/A"

    # Update initial metrics
    if ui:
        ui.update_metric("Local URL", local_url)
        ui.update_metric("LAN URL", lan_url)
        ui.update_metric("Adapter WS", WEBSOCKET_URL)
        ui.update_metric("Adapter HTTP", ADAPTER_HTTP_URL)
        ui.update_metric("Tunnel Status", "Starting..." if enable_tunnel else "Disabled")
        ui.update_metric("Pages Served", "0")

    # Request counter
    request_count = {"value": 0}

    # Add before_request handler to count requests
    @app.before_request
    def count_requests():
        request_count["value"] += 1

    # Metrics update thread
    def update_metrics_loop():
        while ui and ui.running:
            ui.update_metric("Pages Served", str(request_count["value"]))

            with tunnel_url_lock:
                if tunnel_url:
                    ui.update_metric("Tunnel URL", tunnel_url)
                    ui.update_metric("Tunnel Status", "Active")

            time.sleep(1)

    # Override print statements in cloudflared monitor to use log
    original_start_tunnel = start_cloudflared_tunnel

    def logged_start_tunnel(port):
        result = original_start_tunnel(port)
        if result:
            log("Cloudflare Tunnel started successfully")
        return result

    globals()["start_cloudflared_tunnel"] = logged_start_tunnel

    if ui and UI_AVAILABLE:
        # Run Flask in background thread
        flask_thread = threading.Thread(
            target=lambda: app.run(
                host=frontend_host,
                port=frontend_port,
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

        log("Frontend server started successfully")
        log("Terminal UI active - Press Ctrl+C to exit")

        # Run UI (blocking)
        try:
            ui.start()
        except KeyboardInterrupt:
            pass
        finally:
            log("Shutting down...")
    else:
        # Run Flask normally without UI
        app.run(host=frontend_host, port=frontend_port, debug=True)
