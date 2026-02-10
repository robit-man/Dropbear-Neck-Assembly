#!/usr/bin/env python3
"""
NKN router sidecar for teleoperation discovery.

Responsibilities:
- Persist a local NKN seed and run an NKN sidecar client.
- Reply to inbound NKN discovery DMs with current adapter/camera tunnel URLs.
- Provide a local HTTP API for the docs frontend to resolve remote router endpoints.
- Offer optional curses Terminal UI using local terminal_ui.py.
"""

import datetime
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from threading import Lock


# ---------------------------------------------------------------------------
# Virtual environment bootstrap
# ---------------------------------------------------------------------------
ROUTER_VENV_DIR_NAME = "router_venv"


def ensure_venv():
    script_dir = os.path.abspath(os.path.dirname(__file__))
    venv_dir = os.path.join(script_dir, ROUTER_VENV_DIR_NAME)
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(venv_dir)):
        return

    if os.name == "nt":
        pip_path = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_path = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_path = os.path.join(venv_dir, "bin", "pip")
        python_path = os.path.join(venv_dir, "bin", "python")

    required = ["Flask", "Flask-CORS", "requests"]
    import_check = "import flask, flask_cors, requests"

    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment in '{ROUTER_VENV_DIR_NAME}'...")
        import venv

        venv.create(venv_dir, with_pip=True)
        print("Installing required packages (Flask, Flask-CORS, requests)...")
        subprocess.check_call([pip_path, "install", *required])
    else:
        try:
            check = subprocess.run([python_path, "-c", import_check], capture_output=True, timeout=5)
            if check.returncode != 0:
                print("Installing missing packages...")
                subprocess.check_call([pip_path, "install", *required])
        except Exception:
            print("Installing required packages (Flask, Flask-CORS, requests)...")
            subprocess.check_call([pip_path, "install", *required])

    print("Re-launching from venv...")
    os.execv(python_path, [python_path] + sys.argv)


ensure_venv()


# ---------------------------------------------------------------------------
# Imports after venv bootstrap
# ---------------------------------------------------------------------------
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS


# ---------------------------------------------------------------------------
# Optional terminal UI import
# ---------------------------------------------------------------------------
UI_AVAILABLE = False
CategorySpec = None
ConfigSpec = None
SettingSpec = None
TerminalUI = None
ui = None

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
try:
    from terminal_ui import CategorySpec, ConfigSpec, SettingSpec, TerminalUI

    UI_AVAILABLE = True
except ImportError:
    print("Warning: terminal_ui.py not found, running without UI")


# ---------------------------------------------------------------------------
# Defaults and runtime state
# ---------------------------------------------------------------------------
CONFIG_PATH = "config.json"
NODE_SIDECAR_DIR = "nkn_sidecar"
NODE_BRIDGE_FILE = "nkn_router_bridge.js"

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 5070

DEFAULT_ADAPTER_ROUTER_INFO_URL = "http://127.0.0.1:5001/router_info"
DEFAULT_CAMERA_ROUTER_INFO_URL = "http://127.0.0.1:8080/router_info"

DEFAULT_NKN_ENABLE = True
DEFAULT_NKN_IDENTIFIER = "router"
DEFAULT_NKN_SUBCLIENTS = 4
DEFAULT_NKN_DM_RETRIES = 3
DEFAULT_NKN_RESOLVE_TIMEOUT_SECONDS = 10
DEFAULT_AUTO_INSTALL_NKN_SDK = True

PENDING_REQUEST_TTL_SECONDS = 30
SERVICE_REFRESH_INTERVAL_SECONDS = 3.0

_MISSING = object()
request_counter = {"value": 0}
startup_time = time.time()

nkn_process = None
nkn_process_lock = Lock()

service_endpoints = {
    "adapter_router_info_url": DEFAULT_ADAPTER_ROUTER_INFO_URL,
    "camera_router_info_url": DEFAULT_CAMERA_ROUTER_INFO_URL,
}
service_snapshot = {
    "timestamp_ms": 0,
    "services": {},
    "resolved": {},
}
service_snapshot_lock = Lock()
service_refresh_running = threading.Event()

nkn_settings = {
    "enable": DEFAULT_NKN_ENABLE,
    "seed_hex": "",
    "identifier": DEFAULT_NKN_IDENTIFIER,
    "subclients": DEFAULT_NKN_SUBCLIENTS,
    "dm_retries": DEFAULT_NKN_DM_RETRIES,
    "resolve_timeout_seconds": DEFAULT_NKN_RESOLVE_TIMEOUT_SECONDS,
    "auto_install_sdk": DEFAULT_AUTO_INSTALL_NKN_SDK,
}

nkn_runtime = {
    "node_available": False,
    "ready": False,
    "address": "",
    "pubkey_hex": "",
    "last_error": "",
    "last_inbound_from": "",
    "last_outbound_to": "",
    "inbound_count": 0,
    "outbound_count": 0,
}
nkn_runtime_lock = Lock()

pending_resolves = {}
pending_resolves_lock = Lock()


def log(message):
    if ui and UI_AVAILABLE:
        ui.log(message)
    else:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}")


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


def _as_nonempty_str(value, default):
    parsed = str(value).strip()
    return parsed if parsed else default


def _normalize_seed_hex(value):
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


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


def _load_router_settings(config):
    changed = False

    def promote(path, value):
        nonlocal changed
        current = _get_nested(config, path, _MISSING)
        if current is _MISSING or current != value:
            _set_nested(config, path, value)
            changed = True

    listen_host = _as_nonempty_str(
        _read_config_value(
            config,
            "router.network.listen_host",
            DEFAULT_LISTEN_HOST,
            legacy_keys=("router_host", "listen_host"),
        ),
        DEFAULT_LISTEN_HOST,
    )
    promote("router.network.listen_host", listen_host)

    listen_port = _as_int(
        _read_config_value(
            config,
            "router.network.listen_port",
            DEFAULT_LISTEN_PORT,
            legacy_keys=("router_port", "listen_port"),
        ),
        DEFAULT_LISTEN_PORT,
        minimum=1,
        maximum=65535,
    )
    promote("router.network.listen_port", listen_port)

    adapter_router_info_url = _as_nonempty_str(
        _read_config_value(
            config,
            "router.services.adapter_router_info_url",
            DEFAULT_ADAPTER_ROUTER_INFO_URL,
            legacy_keys=("adapter_router_info_url",),
        ),
        DEFAULT_ADAPTER_ROUTER_INFO_URL,
    )
    promote("router.services.adapter_router_info_url", adapter_router_info_url)

    camera_router_info_url = _as_nonempty_str(
        _read_config_value(
            config,
            "router.services.camera_router_info_url",
            DEFAULT_CAMERA_ROUTER_INFO_URL,
            legacy_keys=("camera_router_info_url",),
        ),
        DEFAULT_CAMERA_ROUTER_INFO_URL,
    )
    promote("router.services.camera_router_info_url", camera_router_info_url)

    nkn_enable = _as_bool(
        _read_config_value(
            config,
            "router.nkn.enable",
            DEFAULT_NKN_ENABLE,
            legacy_keys=("router_nkn_enable",),
        ),
        default=DEFAULT_NKN_ENABLE,
    )
    promote("router.nkn.enable", nkn_enable)

    seed_hex = _normalize_seed_hex(
        _read_config_value(
            config,
            "router.nkn.seed_hex",
            "",
            legacy_keys=("router_nkn_seed_hex",),
        )
    )
    if not seed_hex:
        seed_hex = secrets.token_hex(32)
        changed = True
    promote("router.nkn.seed_hex", seed_hex)

    identifier = _as_nonempty_str(
        _read_config_value(
            config,
            "router.nkn.identifier",
            DEFAULT_NKN_IDENTIFIER,
            legacy_keys=("router_nkn_identifier",),
        ),
        DEFAULT_NKN_IDENTIFIER,
    )
    promote("router.nkn.identifier", identifier)

    subclients = _as_int(
        _read_config_value(
            config,
            "router.nkn.subclients",
            DEFAULT_NKN_SUBCLIENTS,
            legacy_keys=("router_nkn_subclients",),
        ),
        DEFAULT_NKN_SUBCLIENTS,
        minimum=1,
        maximum=16,
    )
    promote("router.nkn.subclients", subclients)

    dm_retries = _as_int(
        _read_config_value(
            config,
            "router.nkn.dm_retries",
            DEFAULT_NKN_DM_RETRIES,
            legacy_keys=("router_nkn_dm_retries",),
        ),
        DEFAULT_NKN_DM_RETRIES,
        minimum=1,
        maximum=8,
    )
    promote("router.nkn.dm_retries", dm_retries)

    resolve_timeout_seconds = _as_int(
        _read_config_value(
            config,
            "router.nkn.resolve_timeout_seconds",
            DEFAULT_NKN_RESOLVE_TIMEOUT_SECONDS,
            legacy_keys=("router_nkn_resolve_timeout_seconds",),
        ),
        DEFAULT_NKN_RESOLVE_TIMEOUT_SECONDS,
        minimum=2,
        maximum=60,
    )
    promote("router.nkn.resolve_timeout_seconds", resolve_timeout_seconds)

    auto_install_sdk = _as_bool(
        _read_config_value(
            config,
            "router.nkn.auto_install_sdk",
            DEFAULT_AUTO_INSTALL_NKN_SDK,
            legacy_keys=("router_nkn_auto_install_sdk",),
        ),
        default=DEFAULT_AUTO_INSTALL_NKN_SDK,
    )
    promote("router.nkn.auto_install_sdk", auto_install_sdk)

    settings = {
        "listen_host": listen_host,
        "listen_port": listen_port,
        "adapter_router_info_url": adapter_router_info_url,
        "camera_router_info_url": camera_router_info_url,
        "nkn_enable": nkn_enable,
        "seed_hex": seed_hex,
        "identifier": identifier,
        "subclients": subclients,
        "dm_retries": dm_retries,
        "resolve_timeout_seconds": resolve_timeout_seconds,
        "auto_install_sdk": auto_install_sdk,
    }
    return settings, changed


def _build_router_config_spec():
    if not UI_AVAILABLE:
        return None
    return ConfigSpec(
        label="NKN Router",
        categories=(
            CategorySpec(
                id="network",
                label="Network",
                settings=(
                    SettingSpec(
                        id="listen_host",
                        label="Listen Host",
                        path="router.network.listen_host",
                        value_type="str",
                        default=DEFAULT_LISTEN_HOST,
                        description="Bind host for local router API.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="listen_port",
                        label="Listen Port",
                        path="router.network.listen_port",
                        value_type="int",
                        default=DEFAULT_LISTEN_PORT,
                        min_value=1,
                        max_value=65535,
                        description="Bind port for local router API.",
                        restart_required=True,
                    ),
                ),
            ),
            CategorySpec(
                id="services",
                label="Services",
                settings=(
                    SettingSpec(
                        id="adapter_info",
                        label="Adapter Info URL",
                        path="router.services.adapter_router_info_url",
                        value_type="str",
                        default=DEFAULT_ADAPTER_ROUTER_INFO_URL,
                        description="Adapter /router_info endpoint.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="camera_info",
                        label="Camera Info URL",
                        path="router.services.camera_router_info_url",
                        value_type="str",
                        default=DEFAULT_CAMERA_ROUTER_INFO_URL,
                        description="Camera router /router_info endpoint.",
                        restart_required=True,
                    ),
                ),
            ),
            CategorySpec(
                id="nkn",
                label="NKN",
                settings=(
                    SettingSpec(
                        id="enable",
                        label="Enable NKN",
                        path="router.nkn.enable",
                        value_type="bool",
                        default=DEFAULT_NKN_ENABLE,
                        description="Enable NKN sidecar bridge.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="seed_hex",
                        label="Seed Hex",
                        path="router.nkn.seed_hex",
                        value_type="secret",
                        default="",
                        sensitive=True,
                        description="Persistent 32-byte seed in hex.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="identifier",
                        label="Identifier",
                        path="router.nkn.identifier",
                        value_type="str",
                        default=DEFAULT_NKN_IDENTIFIER,
                        description="NKN address prefix (identifier.<pubhex>).",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="subclients",
                        label="Subclients",
                        path="router.nkn.subclients",
                        value_type="int",
                        default=DEFAULT_NKN_SUBCLIENTS,
                        min_value=1,
                        max_value=16,
                        description="NKN MultiClient subclient count.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="dm_retries",
                        label="DM Retries",
                        path="router.nkn.dm_retries",
                        value_type="int",
                        default=DEFAULT_NKN_DM_RETRIES,
                        min_value=1,
                        max_value=8,
                        description="Retry count for outgoing DMs.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="resolve_timeout_seconds",
                        label="Resolve Timeout",
                        path="router.nkn.resolve_timeout_seconds",
                        value_type="int",
                        default=DEFAULT_NKN_RESOLVE_TIMEOUT_SECONDS,
                        min_value=2,
                        max_value=60,
                        description="Timeout waiting for remote resolve reply.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="auto_install_sdk",
                        label="Auto-install SDK",
                        path="router.nkn.auto_install_sdk",
                        value_type="bool",
                        default=DEFAULT_AUTO_INSTALL_NKN_SDK,
                        description="Install nkn-sdk when missing.",
                        restart_required=True,
                    ),
                ),
            ),
        ),
    )

def parse_nkn_pubkey(address):
    if not address:
        return ""
    maybe = str(address).strip().split(".")[-1]
    return maybe.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", maybe) else ""


def _set_nkn_error(message):
    with nkn_runtime_lock:
        nkn_runtime["last_error"] = str(message or "")


def _set_nkn_ready(address):
    pubkey = parse_nkn_pubkey(address)
    with nkn_runtime_lock:
        nkn_runtime["ready"] = True
        nkn_runtime["address"] = str(address or "")
        nkn_runtime["pubkey_hex"] = pubkey
        nkn_runtime["last_error"] = ""


def _mark_nkn_disconnected(reason):
    with nkn_runtime_lock:
        nkn_runtime["ready"] = False
        nkn_runtime["last_error"] = str(reason or "")


def _increment_nkn_counter(direction, peer):
    with nkn_runtime_lock:
        if direction == "in":
            nkn_runtime["inbound_count"] += 1
            nkn_runtime["last_inbound_from"] = str(peer or "")
        else:
            nkn_runtime["outbound_count"] += 1
            nkn_runtime["last_outbound_to"] = str(peer or "")


def send_nkn_dm(destination, payload, tries=None):
    destination = str(destination or "").strip()
    if not destination:
        return False, "Missing destination address"

    with nkn_process_lock:
        process = nkn_process

    if process is None or process.poll() is not None or process.stdin is None:
        return False, "NKN sidecar is not running"

    cmd = {"type": "dm", "to": destination, "data": payload}
    if tries is None:
        tries = nkn_settings["dm_retries"]
    cmd["tries"] = int(max(1, tries))

    try:
        process.stdin.write(json.dumps(cmd) + "\n")
        process.stdin.flush()
        _increment_nkn_counter("out", destination)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _create_pending_resolve(target):
    request_id = secrets.token_urlsafe(9)
    event = threading.Event()
    entry = {
        "request_id": request_id,
        "target": target,
        "event": event,
        "response": None,
        "created_at": time.time(),
    }
    with pending_resolves_lock:
        pending_resolves[request_id] = entry
    return entry


def _complete_pending_resolve(request_id, source, payload):
    with pending_resolves_lock:
        pending = pending_resolves.get(request_id)
        if not pending:
            return False
        pending["response"] = {"source": source, "payload": payload}
        pending["event"].set()
        return True


def _pop_pending_resolve(request_id):
    with pending_resolves_lock:
        return pending_resolves.pop(request_id, None)


def _cleanup_pending_resolves():
    now = time.time()
    expired = []
    with pending_resolves_lock:
        for request_id, pending in list(pending_resolves.items()):
            if now - pending["created_at"] > PENDING_REQUEST_TTL_SECONDS:
                expired.append(request_id)
                pending["event"].set()
        for request_id in expired:
            pending_resolves.pop(request_id, None)
    return len(expired)


def ensure_node_bridge(auto_install_sdk):
    node_bin = shutil.which("node")
    npm_bin = shutil.which("npm")
    if not node_bin or not npm_bin:
        raise RuntimeError("Node.js and npm are required for NKN routing")

    sidecar_dir = pathlib.Path(SCRIPT_DIR) / NODE_SIDECAR_DIR
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    package_json = sidecar_dir / "package.json"
    if not package_json.exists():
        subprocess.check_call([npm_bin, "init", "-y"], cwd=sidecar_dir, stdout=subprocess.DEVNULL)

    sdk_ok = subprocess.run(
        [node_bin, "-e", "require('nkn-sdk'); process.exit(0)"],
        cwd=sidecar_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0

    if not sdk_ok:
        if not auto_install_sdk:
            raise RuntimeError("nkn-sdk missing and auto-install disabled")
        log("Installing nkn-sdk in router sidecar...")
        subprocess.check_call([npm_bin, "install", "nkn-sdk@^1.3.6"], cwd=sidecar_dir)

    bridge_path = sidecar_dir / NODE_BRIDGE_FILE
    bridge_src = r"""
const nkn = require('nkn-sdk');
const readline = require('readline');

const SEED_HEX = (process.env.NKN_ROUTER_SEED_HEX || '').trim().toLowerCase().replace(/^0x/, '');
const IDENTIFIER = (process.env.NKN_ROUTER_IDENTIFIER || 'router').trim() || 'router';
const SUBCLIENTS = Math.max(1, Math.min(16, Number(process.env.NKN_ROUTER_SUBCLIENTS || '4') || 4));
const DEFAULT_RETRIES = Math.max(1, Math.min(8, Number(process.env.NKN_ROUTER_DM_RETRIES || '3') || 3));

function sendToPy(obj){
  process.stdout.write(JSON.stringify(obj) + '\n');
}
function sleep(ms){ return new Promise((resolve) => setTimeout(resolve, ms)); }

async function sendDM(client, to, data, retries){
  const tries = Math.max(1, Number(retries || DEFAULT_RETRIES) || DEFAULT_RETRIES);
  let lastErr = null;
  for (let i = 0; i < tries; i += 1) {
    try {
      await client.send(String(to || '').trim(), JSON.stringify(data || {}));
      return true;
    } catch (err) {
      lastErr = err;
      const backoff = Math.min(1200, 100 * Math.pow(2, i));
      await sleep(backoff);
    }
  }
  sendToPy({ type: 'dm-error', to: String(to || ''), error: String(lastErr || 'unknown') });
  return false;
}

(async () => {
  if (!/^[0-9a-f]{64}$/i.test(SEED_HEX)) {
    throw new Error('invalid NKN seed hex');
  }

  const client = new nkn.MultiClient({
    seed: SEED_HEX,
    identifier: IDENTIFIER,
    numSubClients: SUBCLIENTS,
  });

  client.on('connect', () => {
    sendToPy({ type: 'ready', address: client.addr });
  });

  client.on('message', (a, b) => {
    try {
      let src = '';
      let payload = '';
      if (a && typeof a === 'object' && a.payload !== undefined) {
        src = String(a.src || '');
        payload = Buffer.isBuffer(a.payload) ? a.payload.toString('utf8') : String(a.payload ?? '');
      } else {
        src = String(a || '');
        payload = Buffer.isBuffer(b) ? b.toString('utf8') : String(b ?? '');
      }
      sendToPy({ type: 'message', src, payload });
    } catch (err) {
      sendToPy({ type: 'error', error: String(err) });
    }
  });

  client.on('wsError', (err) => sendToPy({ type: 'error', error: String(err) }));

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    if (!line) return;
    let cmd = null;
    try { cmd = JSON.parse(line); } catch (_) { return; }
    if (!cmd || typeof cmd !== 'object') return;

    if (cmd.type === 'dm') {
      const ok = await sendDM(client, cmd.to, cmd.data, cmd.tries);
      sendToPy({
        type: 'dm-status',
        ok,
        to: String(cmd.to || ''),
        request_id: cmd.data && cmd.data.request_id ? String(cmd.data.request_id) : '',
      });
      return;
    }

    if (cmd.type === 'close') {
      try { await client.close(); } catch (_) {}
      process.exit(0);
    }
  });
})().catch((err) => {
  sendToPy({ type: 'fatal', error: String(err) });
  process.exit(1);
});
"""
    if not bridge_path.exists() or bridge_path.read_text(encoding="utf-8") != bridge_src:
        bridge_path.write_text(bridge_src, encoding="utf-8")

    return str(node_bin), str(sidecar_dir), str(bridge_path)


def _sidecar_stdout_loop(process):
    for raw_line in process.stdout:
        line = (raw_line or "").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"[NKN] {line}")
            continue

        msg_type = str(msg.get("type") or "").strip().lower()
        if msg_type == "ready":
            address = str(msg.get("address") or "").strip()
            _set_nkn_ready(address)
            log(f"[NKN] ready at {address}")
            continue

        if msg_type == "message":
            _handle_nkn_message(msg.get("src"), msg.get("payload"))
            continue

        if msg_type in ("error", "fatal", "dm-error"):
            error_text = str(msg.get("error") or msg)
            _set_nkn_error(error_text)
            log(f"[NKN] {msg_type}: {error_text}")
            continue

        if msg_type == "dm-status":
            continue

        log(f"[NKN] {msg}")

    _mark_nkn_disconnected("NKN sidecar stdout closed")


def _sidecar_stderr_loop(process):
    for raw_line in process.stderr:
        line = (raw_line or "").rstrip()
        if line:
            log(f"[NKN] {line}")


def start_nkn_sidecar():
    global nkn_process

    if not nkn_settings["enable"]:
        log("NKN sidecar disabled in config")
        return False

    try:
        node_bin, sidecar_dir, bridge_path = ensure_node_bridge(nkn_settings["auto_install_sdk"])
    except Exception as exc:
        _set_nkn_error(exc)
        log(f"[ERROR] NKN sidecar init failed: {exc}")
        return False

    env = os.environ.copy()
    env["NKN_ROUTER_SEED_HEX"] = nkn_settings["seed_hex"]
    env["NKN_ROUTER_IDENTIFIER"] = nkn_settings["identifier"]
    env["NKN_ROUTER_SUBCLIENTS"] = str(nkn_settings["subclients"])
    env["NKN_ROUTER_DM_RETRIES"] = str(nkn_settings["dm_retries"])

    with nkn_runtime_lock:
        nkn_runtime["node_available"] = True

    process = subprocess.Popen(
        [node_bin, bridge_path],
        cwd=sidecar_dir,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    with nkn_process_lock:
        nkn_process = process

    threading.Thread(target=_sidecar_stdout_loop, args=(process,), daemon=True).start()
    threading.Thread(target=_sidecar_stderr_loop, args=(process,), daemon=True).start()
    return True


def stop_nkn_sidecar():
    global nkn_process
    with nkn_process_lock:
        process = nkn_process
        nkn_process = None
    if not process:
        return
    try:
        if process.poll() is None and process.stdin:
            process.stdin.write(json.dumps({"type": "close"}) + "\n")
            process.stdin.flush()
            process.wait(timeout=2)
    except Exception:
        pass
    finally:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    _mark_nkn_disconnected("NKN sidecar stopped")

def _fetch_json(url, timeout=2.0):
    response = requests.get(url, timeout=timeout)
    data = response.json() if response.content else {}
    return response.status_code, data


def _service_record(name, query_url):
    return {
        "service": name,
        "query_url": query_url,
        "ok": False,
        "error": "",
        "http_status": None,
        "data": {},
    }


def _coerce_router_info_shape(name, query_url, data):
    if not isinstance(data, dict):
        return None
    if "local" in data or "tunnel" in data:
        return data
    if "tunnel_url" in data:
        tunnel_url = str(data.get("tunnel_url") or "").strip()
        base_local = query_url.rsplit("/", 1)[0]
        if name == "adapter":
            return {
                "status": data.get("status", "success"),
                "service": "adapter",
                "local": {
                    "base_url": base_local,
                    "http_endpoint": f"{base_local}/send_command",
                    "ws_endpoint": f"{base_local.replace('http://', 'ws://').replace('https://', 'wss://')}/ws",
                    "auth_route": "/auth",
                },
                "tunnel": {
                    "state": "active" if tunnel_url else "inactive",
                    "tunnel_url": tunnel_url,
                    "http_endpoint": str(data.get("http_endpoint") or ""),
                    "ws_endpoint": str(data.get("ws_endpoint") or ""),
                },
            }
        return {
            "status": data.get("status", "success"),
            "service": "camera_router",
            "local": {
                "base_url": base_local,
                "auth_url": f"{base_local}/auth",
                "list_url": f"{base_local}/list",
                "health_url": f"{base_local}/health",
            },
            "tunnel": {
                "state": "active" if tunnel_url else "inactive",
                "tunnel_url": tunnel_url,
                "list_url": f"{tunnel_url}/list" if tunnel_url else "",
                "health_url": f"{tunnel_url}/health" if tunnel_url else "",
            },
        }
    return None


def fetch_service_info(name, query_url):
    query_url = str(query_url or "").strip()
    record = _service_record(name, query_url)
    if not query_url:
        record["error"] = "Missing query URL"
        return record

    tried = []
    candidates = [query_url]
    if query_url.endswith("/router_info"):
        candidates.append(query_url[:-len("/router_info")] + "/tunnel_info")

    for url in candidates:
        tried.append(url)
        try:
            status, data = _fetch_json(url)
        except Exception as exc:
            record["error"] = str(exc)
            continue
        record["http_status"] = status
        if status != 200:
            record["error"] = f"HTTP {status}"
            continue
        shaped = _coerce_router_info_shape(name, url, data)
        if shaped is None:
            record["error"] = "Unexpected response shape"
            continue
        record["ok"] = True
        record["error"] = ""
        record["data"] = shaped
        if url != query_url:
            record["query_url"] = url
        return record

    if len(tried) > 1 and record["error"]:
        record["error"] = f"{record['error']} (tried {', '.join(tried)})"
    return record


def build_resolved_endpoints(services):
    adapter_record = services.get("adapter", {})
    camera_record = services.get("camera", {})

    adapter_data = adapter_record.get("data", {}) if isinstance(adapter_record, dict) else {}
    camera_data = camera_record.get("data", {}) if isinstance(camera_record, dict) else {}

    adapter_local = adapter_data.get("local", {}) if isinstance(adapter_data, dict) else {}
    adapter_tunnel = adapter_data.get("tunnel", {}) if isinstance(adapter_data, dict) else {}

    camera_local = camera_data.get("local", {}) if isinstance(camera_data, dict) else {}
    camera_tunnel = camera_data.get("tunnel", {}) if isinstance(camera_data, dict) else {}

    adapter_tunnel_url = str(adapter_tunnel.get("tunnel_url") or "").strip()
    adapter_http = str(adapter_tunnel.get("http_endpoint") or "").strip()
    adapter_ws = str(adapter_tunnel.get("ws_endpoint") or "").strip()
    if not adapter_http and adapter_tunnel_url:
        adapter_http = f"{adapter_tunnel_url}/send_command"
    if not adapter_ws and adapter_tunnel_url:
        adapter_ws = f"{adapter_tunnel_url.replace('https://', 'wss://')}/ws"

    camera_tunnel_url = str(camera_tunnel.get("tunnel_url") or "").strip()
    camera_base = camera_tunnel_url
    if not camera_base:
        camera_base = str(camera_local.get("base_url") or "").strip()

    return {
        "adapter": {
            "tunnel_url": adapter_tunnel_url,
            "http_endpoint": adapter_http,
            "ws_endpoint": adapter_ws,
            "local_http_endpoint": str(adapter_local.get("http_endpoint") or "").strip(),
            "local_ws_endpoint": str(adapter_local.get("ws_endpoint") or "").strip(),
        },
        "camera": {
            "tunnel_url": camera_tunnel_url,
            "base_url": camera_base,
            "list_url": str(camera_tunnel.get("list_url") or (f"{camera_base}/list" if camera_base else "")).strip(),
            "health_url": str(
                camera_tunnel.get("health_url") or (f"{camera_base}/health" if camera_base else "")
            ).strip(),
            "local_base_url": str(camera_local.get("base_url") or "").strip(),
        },
    }


def collect_service_snapshot():
    services = {
        "adapter": fetch_service_info("adapter", service_endpoints["adapter_router_info_url"]),
        "camera": fetch_service_info("camera", service_endpoints["camera_router_info_url"]),
    }
    resolved = build_resolved_endpoints(services)
    snapshot = {
        "timestamp_ms": int(time.time() * 1000),
        "services": services,
        "resolved": resolved,
    }
    with service_snapshot_lock:
        service_snapshot.update(snapshot)
    return snapshot


def get_service_snapshot(force_refresh=False):
    with service_snapshot_lock:
        existing = dict(service_snapshot)
    if force_refresh or not existing.get("timestamp_ms"):
        return collect_service_snapshot()
    return existing


def _payload_to_dict(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {"event": "raw", "data": parsed}
        except json.JSONDecodeError:
            return {"event": "raw", "data": payload}
    return {"event": "raw", "data": payload}


def _handle_nkn_message(source, payload_text):
    source = str(source or "").strip()
    payload = _payload_to_dict(payload_text)
    _increment_nkn_counter("in", source)

    event_name = str(payload.get("event") or payload.get("type") or "").strip().lower()
    request_id = str(payload.get("request_id") or "").strip()

    if request_id:
        if _complete_pending_resolve(request_id, source, payload):
            return

    if event_name in ("resolve_tunnels", "get_tunnels", "router_info", "router_discover"):
        snapshot = get_service_snapshot(force_refresh=True)
        with nkn_runtime_lock:
            router_address = nkn_runtime["address"]
            router_pubkey_hex = nkn_runtime["pubkey_hex"]
        reply = {
            "event": "resolve_tunnels_result",
            "request_id": request_id,
            "router_address": router_address,
            "router_pubkey_hex": router_pubkey_hex,
            "snapshot": snapshot,
            "timestamp_ms": int(time.time() * 1000),
        }
        ok, err = send_nkn_dm(source, reply, tries=2)
        if not ok:
            log(f"[WARN] Failed to reply to {source}: {err}")
        return

    if event_name == "ping":
        with nkn_runtime_lock:
            router_address = nkn_runtime["address"]
        send_nkn_dm(
            source,
            {
                "event": "pong",
                "router_address": router_address,
                "timestamp_ms": int(time.time() * 1000),
            },
            tries=1,
        )


def service_refresh_loop():
    while service_refresh_running.is_set():
        try:
            collect_service_snapshot()
            cleaned = _cleanup_pending_resolves()
            if cleaned > 0:
                log(f"[NKN] cleaned {cleaned} stale pending resolve(s)")
        except Exception as exc:
            log(f"[WARN] service refresh failed: {exc}")
        time.sleep(SERVICE_REFRESH_INTERVAL_SECONDS)


def metrics_update_loop():
    while ui and ui.running:
        snapshot = get_service_snapshot()
        resolved = snapshot.get("resolved", {})
        adapter = resolved.get("adapter", {})
        camera = resolved.get("camera", {})

        with nkn_runtime_lock:
            ui.update_metric("NKN", "Ready" if nkn_runtime["ready"] else "Waiting")
            ui.update_metric("Address", nkn_runtime["address"] or "N/A")
            ui.update_metric("Pubkey", nkn_runtime["pubkey_hex"] or "N/A")
            ui.update_metric("Inbound", str(nkn_runtime["inbound_count"]))
            ui.update_metric("Outbound", str(nkn_runtime["outbound_count"]))
            if nkn_runtime["last_error"]:
                ui.update_metric("NKN Error", nkn_runtime["last_error"])

        with pending_resolves_lock:
            ui.update_metric("Pending", str(len(pending_resolves)))
        ui.update_metric("Requests", str(request_counter["value"]))
        ui.update_metric("Adapter Tunnel", adapter.get("tunnel_url") or "N/A")
        ui.update_metric("Camera Tunnel", camera.get("tunnel_url") or camera.get("base_url") or "N/A")
        time.sleep(1)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.before_request
def _count_requests():
    request_counter["value"] += 1


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "status": "ok",
            "service": "nkn_router",
            "routes": {
                "health": "/health",
                "nkn_info": "/nkn/info",
                "nkn_resolve": "/nkn/resolve",
                "services": "/services/snapshot",
            },
        }
    )


@app.route("/health", methods=["GET"])
def health():
    snapshot = get_service_snapshot()
    with nkn_runtime_lock:
        nkn_state = dict(nkn_runtime)
    with pending_resolves_lock:
        pending_count = len(pending_resolves)
    process_running = False
    with nkn_process_lock:
        if nkn_process and nkn_process.poll() is None:
            process_running = True
    return jsonify(
        {
            "status": "ok",
            "service": "nkn_router",
            "uptime_seconds": round(time.time() - startup_time, 2),
            "requests_served": request_counter["value"],
            "pending_resolves": pending_count,
            "nkn": {
                "running": process_running,
                "ready": nkn_state["ready"],
                "address": nkn_state["address"],
                "pubkey_hex": nkn_state["pubkey_hex"],
                "last_error": nkn_state["last_error"],
                "inbound_count": nkn_state["inbound_count"],
                "outbound_count": nkn_state["outbound_count"],
            },
            "snapshot": snapshot,
        }
    )


@app.route("/services/snapshot", methods=["GET"])
def services_snapshot_endpoint():
    force = _as_bool(request.args.get("refresh", "false"), default=False)
    snapshot = get_service_snapshot(force_refresh=force)
    return jsonify({"status": "success", "snapshot": snapshot})


@app.route("/nkn/info", methods=["GET"])
def nkn_info():
    with nkn_runtime_lock:
        nkn_state = dict(nkn_runtime)
    snapshot = get_service_snapshot()
    return jsonify(
        {
            "status": "success",
            "nkn": {
                "enabled": bool(nkn_settings["enable"]),
                "ready": bool(nkn_state["ready"]),
                "address": nkn_state["address"],
                "pubkey_hex": nkn_state["pubkey_hex"],
                "identifier": nkn_settings["identifier"],
                "subclients": int(nkn_settings["subclients"]),
                "seed_persisted": bool(nkn_settings["seed_hex"]),
                "last_error": nkn_state["last_error"],
            },
            "snapshot": snapshot,
        }
    )


@app.route("/nkn/resolve", methods=["POST"])
def nkn_resolve():
    data = request.get_json(silent=True) or {}
    target_address = str(data.get("router_address") or data.get("target_address") or "").strip()
    timeout_seconds = _as_int(
        data.get("timeout_seconds", nkn_settings["resolve_timeout_seconds"]),
        nkn_settings["resolve_timeout_seconds"],
        minimum=2,
        maximum=60,
    )
    force_refresh = _as_bool(data.get("refresh_local", True), default=True)

    with nkn_runtime_lock:
        self_address = nkn_runtime["address"]
        ready = nkn_runtime["ready"]

    if not target_address or (self_address and target_address == self_address):
        snapshot = get_service_snapshot(force_refresh=force_refresh)
        return jsonify(
            {
                "status": "success",
                "mode": "local",
                "target_address": self_address,
                "snapshot": snapshot,
                "resolved": snapshot.get("resolved", {}),
            }
        )

    if not ready:
        return jsonify({"status": "error", "message": "NKN sidecar not ready"}), 503

    pending = _create_pending_resolve(target_address)
    payload = {
        "event": "resolve_tunnels",
        "request_id": pending["request_id"],
        "from": self_address,
        "timestamp_ms": int(time.time() * 1000),
    }

    ok, err = send_nkn_dm(target_address, payload, tries=nkn_settings["dm_retries"])
    if not ok:
        _pop_pending_resolve(pending["request_id"])
        return jsonify({"status": "error", "message": f"Failed to send DM: {err}"}), 500

    if not pending["event"].wait(timeout_seconds):
        _pop_pending_resolve(pending["request_id"])
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Timed out waiting for resolve reply from {target_address}",
                    "request_id": pending["request_id"],
                }
            ),
            504,
        )

    complete = _pop_pending_resolve(pending["request_id"])
    if not complete or not complete.get("response"):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Resolve response missing",
                    "request_id": pending["request_id"],
                }
            ),
            502,
        )

    response_payload = complete["response"]["payload"]
    source_address = complete["response"]["source"]
    snapshot = response_payload.get("snapshot") if isinstance(response_payload, dict) else None
    if not isinstance(snapshot, dict):
        snapshot = {}

    resolved = snapshot.get("resolved", {})
    return jsonify(
        {
            "status": "success",
            "mode": "remote",
            "request_id": pending["request_id"],
            "target_address": target_address,
            "source_address": source_address,
            "reply": response_payload,
            "snapshot": snapshot,
            "resolved": resolved,
        }
    )

def main():
    global ui

    config = load_config()
    settings, changed = _load_router_settings(config)
    if changed:
        save_config(config)

    listen_host = settings["listen_host"]
    listen_port = settings["listen_port"]

    service_endpoints["adapter_router_info_url"] = settings["adapter_router_info_url"]
    service_endpoints["camera_router_info_url"] = settings["camera_router_info_url"]

    nkn_settings["enable"] = settings["nkn_enable"]
    nkn_settings["seed_hex"] = settings["seed_hex"]
    nkn_settings["identifier"] = settings["identifier"]
    nkn_settings["subclients"] = settings["subclients"]
    nkn_settings["dm_retries"] = settings["dm_retries"]
    nkn_settings["resolve_timeout_seconds"] = settings["resolve_timeout_seconds"]
    nkn_settings["auto_install_sdk"] = settings["auto_install_sdk"]

    if UI_AVAILABLE:
        ui = TerminalUI("NKN Router", config_spec=_build_router_config_spec(), config_path=CONFIG_PATH)
        ui.log("Starting NKN Router...")

    service_refresh_running.set()
    collect_service_snapshot()
    threading.Thread(target=service_refresh_loop, daemon=True).start()

    if nkn_settings["enable"]:
        if not start_nkn_sidecar():
            log("NKN sidecar failed to start")
    else:
        log("NKN sidecar disabled by config")

    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan_ip = "N/A"
    local_url = f"http://{listen_host}:{listen_port}"
    lan_url = f"http://{lan_ip}:{listen_port}" if lan_ip != "N/A" else "N/A"

    if ui:
        ui.update_metric("Local URL", local_url)
        ui.update_metric("LAN URL", lan_url)
        ui.update_metric("Adapter URL", service_endpoints["adapter_router_info_url"])
        ui.update_metric("Camera URL", service_endpoints["camera_router_info_url"])
        ui.update_metric("Pending", "0")
        ui.update_metric("Requests", "0")
        ui.running = True
        threading.Thread(target=metrics_update_loop, daemon=True).start()

    log(f"Starting NKN router API on {local_url}")
    if lan_url != "N/A":
        log(f"LAN URL: {lan_url}")

    if ui and UI_AVAILABLE:
        flask_thread = threading.Thread(
            target=lambda: app.run(
                host=listen_host,
                port=listen_port,
                debug=False,
                use_reloader=False,
                threaded=True,
            ),
            daemon=True,
        )
        flask_thread.start()
        try:
            ui.start()
        finally:
            log("Shutting down NKN router...")
            service_refresh_running.clear()
            stop_nkn_sidecar()
    else:
        try:
            app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False, threaded=True)
        finally:
            service_refresh_running.clear()
            stop_nkn_sidecar()


if __name__ == "__main__":
    main()
