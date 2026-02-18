#!/usr/bin/env python3
"""
Audio router service.

Capabilities:
- Camera-router-style venv bootstrap and config handling.
- Password auth with expiring session keys.
- Optional terminal UI (terminal_ui.py).
- Cloudflared tunnel support.
- Bidirectional WebRTC audio bridge:
  - Service system microphone -> browser playback.
  - Browser microphone -> service system speaker.
"""

import asyncio
import datetime
import fractions
import json
import os
import pathlib
import platform
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from functools import wraps
from threading import Lock


# ---------------------------------------------------------------------------
# Virtual environment bootstrap
# ---------------------------------------------------------------------------
AUDIO_VENV_DIR_NAME = "audio_route_venv"
AUDIO_CLOUDFLARED_BASENAME = "audio_route_cloudflared"


def ensure_venv():
    script_dir = os.path.abspath(os.path.dirname(__file__))
    venv_dir = os.path.join(script_dir, AUDIO_VENV_DIR_NAME)
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(venv_dir)):
        return

    if os.name == "nt":
        pip_path = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_path = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_path = os.path.join(venv_dir, "bin", "pip")
        python_path = os.path.join(venv_dir, "bin", "python")

    required = ["Flask", "Flask-CORS", "numpy", "sounddevice"]

    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment in '{AUDIO_VENV_DIR_NAME}'...")
        import venv

        venv.create(venv_dir, with_pip=True)
        print("Installing required packages (Flask, Flask-CORS, numpy, sounddevice)...")
        subprocess.check_call([pip_path, "install", *required])
    else:
        try:
            check = subprocess.run(
                [python_path, "-c", "import flask, flask_cors, numpy, sounddevice"],
                capture_output=True,
                timeout=5,
            )
            if check.returncode != 0:
                print("Installing missing packages...")
                subprocess.check_call([pip_path, "install", *required])
        except Exception:
            print("Installing required packages (Flask, Flask-CORS, numpy, sounddevice)...")
            subprocess.check_call([pip_path, "install", *required])

    # Optional WebRTC stack.
    for optional_pkg in ("av", "aiortc"):
        try:
            subprocess.run(
                [python_path, "-c", f"import {optional_pkg}"],
                capture_output=True,
                timeout=5,
                check=True,
            )
        except Exception:
            try:
                print(f"Installing optional package ({optional_pkg})...")
                subprocess.check_call([pip_path, "install", optional_pkg])
            except Exception as exc:
                print(f"Optional package install failed ({optional_pkg}): {exc}")

    print("Re-launching from venv...")
    os.execv(python_path, [python_path] + sys.argv)


ensure_venv()


# ---------------------------------------------------------------------------
# Imports after venv bootstrap
# ---------------------------------------------------------------------------
import numpy as np
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

AUDIO_BACKEND_AVAILABLE = False
AUDIO_BACKEND_ERROR = ""
sd = None
try:
    import sounddevice as sd  # type: ignore

    AUDIO_BACKEND_AVAILABLE = True
except Exception as exc:
    AUDIO_BACKEND_ERROR = f"{type(exc).__name__}: {exc}"

WEBRTC_AVAILABLE = False
WEBRTC_IMPORT_ERROR = ""
RTCPeerConnection = None
RTCSessionDescription = None
AudioStreamTrack = None
AudioFrame = None
AudioResampler = None
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
    from av import AudioFrame
    from av.audio.resampler import AudioResampler

    WEBRTC_AVAILABLE = True
except Exception as exc:
    WEBRTC_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


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
UI_SEARCH_PATHS = [
    SCRIPT_DIR,
    os.path.abspath(os.path.join(SCRIPT_DIR, "..", "frontend")),
]
for candidate in UI_SEARCH_PATHS:
    if candidate not in sys.path:
        sys.path.insert(0, candidate)
    try:
        from terminal_ui import CategorySpec, ConfigSpec, SettingSpec, TerminalUI

        UI_AVAILABLE = True
        break
    except ImportError:
        continue
if not UI_AVAILABLE:
    print("Warning: terminal_ui.py not found, running without UI")


# ---------------------------------------------------------------------------
# Defaults and runtime state
# ---------------------------------------------------------------------------
CONFIG_PATH = "config.json"

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8090

DEFAULT_PASSWORD = "audio2026"
DEFAULT_SESSION_TIMEOUT = 300
DEFAULT_REQUIRE_AUTH = True

DEFAULT_ENABLE_TUNNEL = True
DEFAULT_AUTO_INSTALL_CLOUDFLARED = True
DEFAULT_TUNNEL_RESTART_DELAY_SECONDS = 3.0
DEFAULT_TUNNEL_RATE_LIMIT_DELAY_SECONDS = 45.0
MAX_TUNNEL_RESTART_DELAY_SECONDS = 300.0
DEFAULT_ENABLE_UPNP_FALLBACK = True
UPNP_FALLBACK_REFRESH_SECONDS = 90.0

DEFAULT_AUDIO_SAMPLE_RATE = 48000
DEFAULT_AUDIO_CHANNELS = 1
DEFAULT_AUDIO_FRAME_MS = 20
DEFAULT_AUDIO_INPUT_DEVICE = "default"
DEFAULT_AUDIO_OUTPUT_DEVICE = "default"

_MISSING = object()

SESSION_TIMEOUT = DEFAULT_SESSION_TIMEOUT
runtime_security = {
    "password": DEFAULT_PASSWORD,
    "require_auth": DEFAULT_REQUIRE_AUTH,
}
network_runtime = {
    "listen_host": DEFAULT_LISTEN_HOST,
    "listen_port": DEFAULT_LISTEN_PORT,
}
audio_runtime = {
    "sample_rate": DEFAULT_AUDIO_SAMPLE_RATE,
    "channels": DEFAULT_AUDIO_CHANNELS,
    "frame_ms": DEFAULT_AUDIO_FRAME_MS,
    "input_device": DEFAULT_AUDIO_INPUT_DEVICE,
    "output_device": DEFAULT_AUDIO_OUTPUT_DEVICE,
}
audio_runtime_lock = Lock()

sessions = {}
sessions_lock = Lock()
request_count = {"value": 0}
startup_time = time.time()

service_running = threading.Event()
service_running.set()


tunnel_process = None
tunnel_url = None
tunnel_last_error = ""
tunnel_desired = False
tunnel_url_lock = Lock()
tunnel_restart_lock = Lock()
tunnel_restart_failures = 0
upnp_fallback_lock = Lock()
upnp_fallback_state = {
    "state": "inactive",
    "enabled": DEFAULT_ENABLE_UPNP_FALLBACK,
    "public_ip": "",
    "external_port": 0,
    "public_base_url": "",
    "list_url": "",
    "health_url": "",
    "webrtc_offer_url": "",
    "error": "",
    "last_attempt_ms": 0,
}
_nats_subject_token = secrets.token_hex(8)
_nkn_topic_token = secrets.token_hex(8)
nats_fallback_state = {
    "state": "inactive",
    "broker_url": "wss://demo.nats.io:443",
    "subject": f"dropbear.audio.{_nats_subject_token}",
    "error": "NATS fallback is advertised but no relay is configured in this build",
}
nkn_fallback_state = {
    "state": "inactive",
    "topic": f"dropbear.audio.{_nkn_topic_token}",
    "error": "Per-service NKN sidecar fallback is not configured in this build",
}

# WebRTC runtime (dedicated loop thread).
webrtc_loop = None
webrtc_loop_thread = None
webrtc_loop_ready = threading.Event()
webrtc_loop_lock = Lock()

peer_contexts = {}
peer_contexts_lock = Lock()
peer_seq = {"value": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _upnp_snapshot():
    with upnp_fallback_lock:
        return dict(upnp_fallback_state)


def _refresh_upnp_fallback(listen_port, force=False):
    now_ms = int(time.time() * 1000)
    with upnp_fallback_lock:
        enabled = bool(upnp_fallback_state.get("enabled", DEFAULT_ENABLE_UPNP_FALLBACK))
        last_attempt_ms = int(upnp_fallback_state.get("last_attempt_ms", 0) or 0)
        if not enabled:
            upnp_fallback_state.update(
                {
                    "state": "disabled",
                    "error": "UPnP fallback disabled",
                    "last_attempt_ms": now_ms,
                }
            )
            return dict(upnp_fallback_state)
        if not force and last_attempt_ms and (now_ms - last_attempt_ms) < int(UPNP_FALLBACK_REFRESH_SECONDS * 1000):
            return dict(upnp_fallback_state)
        upnp_fallback_state["last_attempt_ms"] = now_ms

    result = {
        "state": "inactive",
        "enabled": enabled,
        "public_ip": "",
        "external_port": 0,
        "public_base_url": "",
        "list_url": "",
        "health_url": "",
        "webrtc_offer_url": "",
        "error": "",
        "last_attempt_ms": now_ms,
    }

    try:
        import miniupnpc  # type: ignore
    except Exception as exc:
        result["state"] = "unavailable"
        result["error"] = f"miniupnpc unavailable: {exc}"
        with upnp_fallback_lock:
            upnp_fallback_state.update(result)
            return dict(upnp_fallback_state)

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
                        "dropbear-audio-router",
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
        result.update(
            {
                "state": "active",
                "public_ip": public_ip,
                "external_port": mapped_port,
                "public_base_url": public_base,
                "list_url": f"{public_base}/list",
                "health_url": f"{public_base}/health",
                "webrtc_offer_url": f"{public_base}/webrtc/offer",
                "error": "",
            }
        )
    except Exception as exc:
        result["state"] = "error"
        result["error"] = str(exc)

    with upnp_fallback_lock:
        upnp_fallback_state.update(result)
        return dict(upnp_fallback_state)


def _audio_fallback_payload(current_tunnel, process_running, listen_port):
    tunnel_active = bool(process_running and str(current_tunnel or "").strip())
    if tunnel_active:
        upnp_state = _upnp_snapshot()
    else:
        upnp_state = _refresh_upnp_fallback(listen_port, force=False)

    selected = "local"
    if tunnel_active:
        selected = "cloudflare"
    elif str(upnp_state.get("state") or "").strip().lower() == "active":
        selected = "upnp"
    elif str(nats_fallback_state.get("state") or "").strip().lower() == "active":
        selected = "nats"
    elif str(nkn_fallback_state.get("state") or "").strip().lower() == "active":
        selected = "nkn"

    return {
        "selected_transport": selected,
        "order": ["cloudflare", "upnp", "nats", "nkn", "local"],
        "upnp": upnp_state,
        "nats": dict(nats_fallback_state),
        "nkn": dict(nkn_fallback_state),
    }


def log(message):
    msg = str(message)
    if ui and UI_AVAILABLE:
        ui.log(msg)
    else:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")


def _get_nested(data, path, default=_MISSING):
    current = data
    for key in str(path or "").split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def _set_nested(data, path, value):
    current = data
    keys = str(path or "").split(".")
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
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
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


def _normalize_device_setting(value):
    if isinstance(value, bool):
        return "default"
    if value is None:
        return "default"
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return "default"
    lowered = text.lower()
    if lowered in ("default", "system", "auto"):
        return "default"
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except Exception:
            return "default"
    return text


def _device_setting_for_json(value):
    if isinstance(value, (int, str)):
        return value
    return str(value)


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


def _load_audio_settings(config):
    changed = False

    def promote(path, value):
        nonlocal changed
        current = _get_nested(config, path, _MISSING)
        if current is _MISSING or current != value:
            _set_nested(config, path, value)
            changed = True

    listen_host = str(
        _read_config_value(
            config,
            "audio_router.network.listen_host",
            DEFAULT_LISTEN_HOST,
            legacy_keys=("listen_host", "host", "audio_host"),
        )
    ).strip() or DEFAULT_LISTEN_HOST
    promote("audio_router.network.listen_host", listen_host)

    listen_port = _as_int(
        _read_config_value(
            config,
            "audio_router.network.listen_port",
            DEFAULT_LISTEN_PORT,
            legacy_keys=("listen_port", "port", "audio_port"),
        ),
        DEFAULT_LISTEN_PORT,
        minimum=1,
        maximum=65535,
    )
    promote("audio_router.network.listen_port", listen_port)

    password = str(
        _read_config_value(
            config,
            "audio_router.security.password",
            DEFAULT_PASSWORD,
            legacy_keys=("password",),
        )
    ).strip() or DEFAULT_PASSWORD
    promote("audio_router.security.password", password)

    session_timeout = _as_int(
        _read_config_value(
            config,
            "audio_router.security.session_timeout",
            DEFAULT_SESSION_TIMEOUT,
            legacy_keys=("session_timeout",),
        ),
        DEFAULT_SESSION_TIMEOUT,
        minimum=30,
        maximum=86400,
    )
    promote("audio_router.security.session_timeout", session_timeout)

    require_auth = _as_bool(
        _read_config_value(
            config,
            "audio_router.security.require_auth",
            DEFAULT_REQUIRE_AUTH,
            legacy_keys=("require_auth",),
        ),
        default=DEFAULT_REQUIRE_AUTH,
    )
    promote("audio_router.security.require_auth", require_auth)

    enable_tunnel = _as_bool(
        _read_config_value(
            config,
            "audio_router.tunnel.enable",
            DEFAULT_ENABLE_TUNNEL,
            legacy_keys=("enable_tunnel",),
        ),
        default=DEFAULT_ENABLE_TUNNEL,
    )
    promote("audio_router.tunnel.enable", enable_tunnel)

    auto_install_cloudflared = _as_bool(
        _read_config_value(
            config,
            "audio_router.tunnel.auto_install_cloudflared",
            DEFAULT_AUTO_INSTALL_CLOUDFLARED,
            legacy_keys=("auto_install_cloudflared",),
        ),
        default=DEFAULT_AUTO_INSTALL_CLOUDFLARED,
    )
    promote("audio_router.tunnel.auto_install_cloudflared", auto_install_cloudflared)

    sample_rate = _as_int(
        _read_config_value(
            config,
            "audio_router.audio.sample_rate",
            DEFAULT_AUDIO_SAMPLE_RATE,
        ),
        DEFAULT_AUDIO_SAMPLE_RATE,
        minimum=8000,
        maximum=192000,
    )
    promote("audio_router.audio.sample_rate", sample_rate)

    channels = _as_int(
        _read_config_value(
            config,
            "audio_router.audio.channels",
            DEFAULT_AUDIO_CHANNELS,
        ),
        DEFAULT_AUDIO_CHANNELS,
        minimum=1,
        maximum=2,
    )
    promote("audio_router.audio.channels", channels)

    frame_ms = _as_int(
        _read_config_value(
            config,
            "audio_router.audio.frame_ms",
            DEFAULT_AUDIO_FRAME_MS,
        ),
        DEFAULT_AUDIO_FRAME_MS,
        minimum=10,
        maximum=100,
    )
    promote("audio_router.audio.frame_ms", frame_ms)

    input_device = _normalize_device_setting(
        _read_config_value(
            config,
            "audio_router.audio.input_device",
            DEFAULT_AUDIO_INPUT_DEVICE,
        )
    )
    promote("audio_router.audio.input_device", input_device)

    output_device = _normalize_device_setting(
        _read_config_value(
            config,
            "audio_router.audio.output_device",
            DEFAULT_AUDIO_OUTPUT_DEVICE,
        )
    )
    promote("audio_router.audio.output_device", output_device)

    return {
        "listen_host": listen_host,
        "listen_port": listen_port,
        "password": password,
        "session_timeout": session_timeout,
        "require_auth": require_auth,
        "enable_tunnel": enable_tunnel,
        "auto_install_cloudflared": auto_install_cloudflared,
        "sample_rate": sample_rate,
        "channels": channels,
        "frame_ms": frame_ms,
        "input_device": input_device,
        "output_device": output_device,
    }, changed


def _build_audio_config_spec():
    if not UI_AVAILABLE:
        return None
    return ConfigSpec(
        label="Audio Router",
        categories=(
            CategorySpec(
                id="network",
                label="Network",
                settings=(
                    SettingSpec(
                        id="listen_host",
                        label="Listen Host",
                        path="audio_router.network.listen_host",
                        value_type="str",
                        default=DEFAULT_LISTEN_HOST,
                        description="HTTP bind host.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="listen_port",
                        label="Listen Port",
                        path="audio_router.network.listen_port",
                        value_type="int",
                        default=DEFAULT_LISTEN_PORT,
                        min_value=1,
                        max_value=65535,
                        description="HTTP bind port.",
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
                        path="audio_router.security.password",
                        value_type="secret",
                        default=DEFAULT_PASSWORD,
                        description="Password used by /auth.",
                    ),
                    SettingSpec(
                        id="session_timeout",
                        label="Session Timeout",
                        path="audio_router.security.session_timeout",
                        value_type="int",
                        default=DEFAULT_SESSION_TIMEOUT,
                        min_value=30,
                        max_value=86400,
                        description="Session expiration in seconds.",
                    ),
                    SettingSpec(
                        id="require_auth",
                        label="Require Auth",
                        path="audio_router.security.require_auth",
                        value_type="bool",
                        default=DEFAULT_REQUIRE_AUTH,
                        description="Protect audio routes with session keys.",
                    ),
                ),
            ),
            CategorySpec(
                id="audio",
                label="Audio",
                settings=(
                    SettingSpec(
                        id="sample_rate",
                        label="Sample Rate",
                        path="audio_router.audio.sample_rate",
                        value_type="int",
                        default=DEFAULT_AUDIO_SAMPLE_RATE,
                        min_value=8000,
                        max_value=192000,
                        description="WebRTC/audio backend sample rate in Hz.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="channels",
                        label="Channels",
                        path="audio_router.audio.channels",
                        value_type="int",
                        default=DEFAULT_AUDIO_CHANNELS,
                        min_value=1,
                        max_value=2,
                        description="1=mono, 2=stereo.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="frame_ms",
                        label="Frame Size ms",
                        path="audio_router.audio.frame_ms",
                        value_type="int",
                        default=DEFAULT_AUDIO_FRAME_MS,
                        min_value=10,
                        max_value=100,
                        description="PCM chunk size in milliseconds.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="input_device",
                        label="Input Device",
                        path="audio_router.audio.input_device",
                        value_type="str",
                        default=DEFAULT_AUDIO_INPUT_DEVICE,
                        description="default, device index, or partial device name.",
                    ),
                    SettingSpec(
                        id="output_device",
                        label="Output Device",
                        path="audio_router.audio.output_device",
                        value_type="str",
                        default=DEFAULT_AUDIO_OUTPUT_DEVICE,
                        description="default, device index, or partial device name.",
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
                        path="audio_router.tunnel.enable",
                        value_type="bool",
                        default=DEFAULT_ENABLE_TUNNEL,
                        description="Enable Cloudflare tunnel.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="auto_install_cloudflared",
                        label="Auto-install Cloudflared",
                        path="audio_router.tunnel.auto_install_cloudflared",
                        value_type="bool",
                        default=DEFAULT_AUTO_INSTALL_CLOUDFLARED,
                        description="Install cloudflared if missing.",
                        restart_required=True,
                    ),
                ),
            ),
        ),
    )


def apply_runtime_settings(settings):
    runtime_security["password"] = str(settings["password"])
    runtime_security["require_auth"] = bool(settings["require_auth"])

    global SESSION_TIMEOUT
    SESSION_TIMEOUT = int(settings["session_timeout"])

    network_runtime["listen_host"] = str(settings["listen_host"])
    network_runtime["listen_port"] = int(settings["listen_port"])

    with audio_runtime_lock:
        audio_runtime["sample_rate"] = int(settings["sample_rate"])
        audio_runtime["channels"] = int(settings["channels"])
        audio_runtime["frame_ms"] = int(settings["frame_ms"])
        audio_runtime["input_device"] = _normalize_device_setting(settings["input_device"])
        audio_runtime["output_device"] = _normalize_device_setting(settings["output_device"])

    if ui:
        ui.update_metric("Auth", "Required" if runtime_security["require_auth"] else "Disabled")
        ui.update_metric("Session Timeout", str(SESSION_TIMEOUT))


def apply_runtime_settings_from_config(saved_config):
    settings, _ = _load_audio_settings(saved_config if isinstance(saved_config, dict) else {})
    apply_runtime_settings(settings)
    if ui:
        ui.log("Applied live audio runtime updates from config save")


# ---------------------------------------------------------------------------
# Authentication/session
# ---------------------------------------------------------------------------
def create_session():
    session_key = secrets.token_urlsafe(32)
    now = time.time()
    with sessions_lock:
        sessions[session_key] = {"created_at": now, "last_used": now}
    return session_key


def rotate_sessions():
    now = time.time()
    next_session_key = secrets.token_urlsafe(32)
    with sessions_lock:
        invalidated = len(sessions)
        sessions.clear()
        sessions[next_session_key] = {"created_at": now, "last_used": now}
    return next_session_key, invalidated


def validate_session(session_key):
    if not session_key:
        return False
    with sessions_lock:
        entry = sessions.get(session_key)
        if not entry:
            return False
        now = time.time()
        if now - entry["last_used"] > SESSION_TIMEOUT:
            sessions.pop(session_key, None)
            return False
        entry["last_used"] = now
        return True


def cleanup_expired_sessions():
    now = time.time()
    with sessions_lock:
        expired = [key for key, value in sessions.items() if now - value["last_used"] > SESSION_TIMEOUT]
        for key in expired:
            sessions.pop(key, None)


def get_session_key_from_request():
    key = request.headers.get("X-Session-Key", "").strip()
    if key:
        return key
    key = request.args.get("session_key", "").strip()
    if key:
        return key
    if request.method in ("POST", "PUT", "PATCH"):
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            key = str(payload.get("session_key", "")).strip()
            if key:
                return key
    return ""


def require_session(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not runtime_security["require_auth"]:
            return handler(*args, **kwargs)
        session_key = get_session_key_from_request()
        if not validate_session(session_key):
            return jsonify({"status": "error", "message": "Invalid or expired session"}), 401
        return handler(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Cloudflared support
# ---------------------------------------------------------------------------
def get_cloudflared_path():
    if os.name == "nt":
        return os.path.join(SCRIPT_DIR, f"{AUDIO_CLOUDFLARED_BASENAME}.exe")
    return os.path.join(SCRIPT_DIR, AUDIO_CLOUDFLARED_BASENAME)


def is_cloudflared_installed():
    local_path = get_cloudflared_path()
    if os.path.exists(local_path):
        return True
    try:
        subprocess.run(["cloudflared", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def install_cloudflared():
    log("Installing cloudflared...")
    cloudflared_path = get_cloudflared_path()
    system = platform.system().lower()
    machine = platform.machine().lower()

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
        log(f"[ERROR] Unsupported platform for cloudflared: {system} {machine}")
        return False

    try:
        import urllib.request

        log(f"Downloading cloudflared from {url}...")
        urllib.request.urlretrieve(url, cloudflared_path)
        if os.name != "nt":
            os.chmod(cloudflared_path, 0o755)
        log("[OK] Cloudflared installed successfully")
        return True
    except Exception as exc:
        log(f"[ERROR] Failed to install cloudflared: {exc}")
        return False


def stop_cloudflared_tunnel():
    global tunnel_process, tunnel_last_error, tunnel_url, tunnel_desired, tunnel_restart_failures
    tunnel_desired = False
    tunnel_restart_failures = 0
    process = tunnel_process
    if process is None:
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


def start_cloudflared_tunnel(local_port):
    global tunnel_process, tunnel_url, tunnel_last_error, tunnel_desired

    with tunnel_restart_lock:
        if tunnel_process is not None and tunnel_process.poll() is None:
            return True
        tunnel_desired = True

    cloudflared_path = get_cloudflared_path()
    if not os.path.exists(cloudflared_path):
        cloudflared_path = "cloudflared"

    with tunnel_url_lock:
        tunnel_url = None
    tunnel_last_error = ""

    cmd = [
        cloudflared_path,
        "tunnel",
        "--protocol",
        "http2",
        "--url",
        f"http://localhost:{int(local_port)}",
    ]
    log(f"[START] Launching cloudflared: {' '.join(cmd)}")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        tunnel_process = process
    except Exception as exc:
        tunnel_last_error = str(exc)
        log(f"[ERROR] Failed to start cloudflared tunnel: {exc}")
        return False

    def monitor_output():
        global tunnel_process, tunnel_url, tunnel_last_error, tunnel_restart_failures
        found_url = False
        captured_url = ""
        rate_limited = False

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
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if not match:
                    match = re.search(r"https://[^\s]+trycloudflare\.com[^\s]*", line)
                if match:
                    with tunnel_url_lock:
                        if tunnel_url is None:
                            captured_url = match.group(0)
                            tunnel_url = captured_url
                            tunnel_last_error = ""
                            found_url = True
                            tunnel_restart_failures = 0
                            log("")
                            log("=" * 60)
                            log(f"[TUNNEL] Audio Router URL: {tunnel_url}")
                            log("=" * 60)
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
                try:
                    upnp_state = _refresh_upnp_fallback(local_port, force=True)
                    if str(upnp_state.get("state") or "").strip().lower() == "active":
                        log(
                            f"[FALLBACK] UPnP audio endpoint ready: "
                            f"{upnp_state.get('public_base_url') or ''}"
                        )
                except Exception as exc:
                    log(f"[WARN] UPnP fallback refresh failed: {exc}")

            if tunnel_desired and service_running.is_set():
                delay = _next_tunnel_restart_delay(rate_limited=rate_limited and not found_url)
                log(f"[WARN] Restarting cloudflared in {delay:.1f}s...")
                time.sleep(delay)
                if tunnel_desired and service_running.is_set():
                    start_cloudflared_tunnel(local_port)

    threading.Thread(target=monitor_output, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Audio device + runtime helpers
# ---------------------------------------------------------------------------
def _query_audio_backend_devices():
    if not AUDIO_BACKEND_AVAILABLE:
        return []
    try:
        devices = sd.query_devices()
        if not isinstance(devices, (list, tuple)):
            return []
        return list(devices)
    except Exception:
        return []


def _query_hostapi_names():
    if not AUDIO_BACKEND_AVAILABLE:
        return {}
    names = {}
    try:
        hostapis = sd.query_hostapis()
        if isinstance(hostapis, dict):
            hostapis = [hostapis]
        for idx, item in enumerate(hostapis or []):
            if isinstance(item, dict):
                names[idx] = str(item.get("name", ""))
    except Exception:
        pass
    return names


def _default_device_index(kind):
    if not AUDIO_BACKEND_AVAILABLE:
        return None
    try:
        default_value = sd.default.device
    except Exception:
        return None
    if isinstance(default_value, (list, tuple)):
        target_idx = 0 if kind == "input" else 1
        if len(default_value) > target_idx:
            try:
                idx = int(default_value[target_idx])
                return idx if idx >= 0 else None
            except Exception:
                return None
    try:
        idx = int(default_value)
        return idx if idx >= 0 else None
    except Exception:
        return None


def _device_supports_kind(device_entry, kind):
    if not isinstance(device_entry, dict):
        return False
    if kind == "input":
        return int(device_entry.get("max_input_channels", 0) or 0) > 0
    return int(device_entry.get("max_output_channels", 0) or 0) > 0


def _resolve_device_index(kind, desired):
    devices = _query_audio_backend_devices()
    if not devices:
        return None

    desired = _normalize_device_setting(desired)

    if isinstance(desired, int):
        if 0 <= desired < len(devices) and _device_supports_kind(devices[desired], kind):
            return int(desired)

    if isinstance(desired, str) and desired != "default":
        lowered = desired.lower()
        for idx, device in enumerate(devices):
            name = str(device.get("name", "")).strip().lower()
            if name == lowered and _device_supports_kind(device, kind):
                return idx
        for idx, device in enumerate(devices):
            name = str(device.get("name", "")).strip().lower()
            if lowered in name and _device_supports_kind(device, kind):
                return idx

    default_idx = _default_device_index(kind)
    if isinstance(default_idx, int) and 0 <= default_idx < len(devices):
        if _device_supports_kind(devices[default_idx], kind):
            return int(default_idx)

    for idx, device in enumerate(devices):
        if _device_supports_kind(device, kind):
            return idx
    return None


def _describe_device(index):
    devices = _query_audio_backend_devices()
    if not isinstance(index, int):
        return None
    if index < 0 or index >= len(devices):
        return None

    hostapi_names = _query_hostapi_names()
    entry = devices[index]
    hostapi_idx = entry.get("hostapi")
    try:
        hostapi_idx = int(hostapi_idx)
    except Exception:
        hostapi_idx = -1

    return {
        "id": index,
        "name": str(entry.get("name", "")),
        "hostapi": hostapi_names.get(hostapi_idx, ""),
        "hostapi_index": hostapi_idx,
        "max_input_channels": int(entry.get("max_input_channels", 0) or 0),
        "max_output_channels": int(entry.get("max_output_channels", 0) or 0),
        "default_samplerate": float(entry.get("default_samplerate", 0.0) or 0.0),
    }


def _build_device_catalog():
    catalog = {
        "inputs": [],
        "outputs": [],
    }
    if not AUDIO_BACKEND_AVAILABLE:
        return catalog

    devices = _query_audio_backend_devices()
    hostapi_names = _query_hostapi_names()
    default_in = _default_device_index("input")
    default_out = _default_device_index("output")

    for idx, entry in enumerate(devices):
        hostapi_idx = entry.get("hostapi")
        try:
            hostapi_idx = int(hostapi_idx)
        except Exception:
            hostapi_idx = -1

        base = {
            "id": idx,
            "name": str(entry.get("name", "")),
            "hostapi": hostapi_names.get(hostapi_idx, ""),
            "hostapi_index": hostapi_idx,
            "default_samplerate": float(entry.get("default_samplerate", 0.0) or 0.0),
            "max_input_channels": int(entry.get("max_input_channels", 0) or 0),
            "max_output_channels": int(entry.get("max_output_channels", 0) or 0),
        }
        if base["max_input_channels"] > 0:
            item = dict(base)
            item["is_default"] = idx == default_in
            catalog["inputs"].append(item)
        if base["max_output_channels"] > 0:
            item = dict(base)
            item["is_default"] = idx == default_out
            catalog["outputs"].append(item)

    return catalog


def current_audio_config():
    with audio_runtime_lock:
        return {
            "sample_rate": int(audio_runtime["sample_rate"]),
            "channels": int(audio_runtime["channels"]),
            "frame_ms": int(audio_runtime["frame_ms"]),
            "input_device": _normalize_device_setting(audio_runtime["input_device"]),
            "output_device": _normalize_device_setting(audio_runtime["output_device"]),
        }


def current_audio_selection():
    cfg = current_audio_config()
    input_idx = _resolve_device_index("input", cfg["input_device"])
    output_idx = _resolve_device_index("output", cfg["output_device"])

    return {
        **cfg,
        "input_index": input_idx,
        "output_index": output_idx,
        "input_device_info": _describe_device(input_idx),
        "output_device_info": _describe_device(output_idx),
    }


def _persist_audio_device_settings(input_value, output_value):
    cfg = load_config()
    if input_value is not _MISSING:
        _set_nested(cfg, "audio_router.audio.input_device", _normalize_device_setting(input_value))
    if output_value is not _MISSING:
        _set_nested(cfg, "audio_router.audio.output_device", _normalize_device_setting(output_value))
    save_config(cfg)


def _set_runtime_audio_devices(input_value=_MISSING, output_value=_MISSING):
    with audio_runtime_lock:
        if input_value is not _MISSING:
            audio_runtime["input_device"] = _normalize_device_setting(input_value)
        if output_value is not _MISSING:
            audio_runtime["output_device"] = _normalize_device_setting(output_value)


# ---------------------------------------------------------------------------
# WebRTC runtime
# ---------------------------------------------------------------------------
def _webrtc_loop_worker():
    global webrtc_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with webrtc_loop_lock:
        webrtc_loop = loop
        webrtc_loop_ready.set()

    try:
        loop.run_forever()
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        with webrtc_loop_lock:
            webrtc_loop = None
            webrtc_loop_ready.clear()


def ensure_webrtc_loop():
    global webrtc_loop_thread
    if not WEBRTC_AVAILABLE:
        return False
    with webrtc_loop_lock:
        existing = webrtc_loop
        thread = webrtc_loop_thread
    if existing is not None and thread and thread.is_alive():
        return True

    with webrtc_loop_lock:
        existing = webrtc_loop
        thread = webrtc_loop_thread
        if existing is not None and thread and thread.is_alive():
            return True
        webrtc_loop_ready.clear()
        webrtc_loop_thread = threading.Thread(target=_webrtc_loop_worker, daemon=True)
        webrtc_loop_thread.start()

    return webrtc_loop_ready.wait(timeout=2.5)


def run_webrtc_coro(coro, timeout=15.0):
    if not ensure_webrtc_loop():
        raise RuntimeError("WebRTC loop is not available")
    with webrtc_loop_lock:
        loop = webrtc_loop
    if loop is None:
        raise RuntimeError("WebRTC loop is not running")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def stop_webrtc_loop():
    global webrtc_loop_thread

    if WEBRTC_AVAILABLE:
        try:
            run_webrtc_coro(_close_all_peer_connections(), timeout=8.0)
        except Exception:
            pass

    with webrtc_loop_lock:
        loop = webrtc_loop
        thread = webrtc_loop_thread

    if loop is not None:
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    if thread and thread.is_alive():
        thread.join(timeout=2.0)

    with webrtc_loop_lock:
        webrtc_loop_thread = None


def _next_peer_id():
    with peer_contexts_lock:
        peer_seq["value"] += 1
        return f"peer-{peer_seq['value']}"


if WEBRTC_AVAILABLE:

    class MicrophoneAudioTrack(AudioStreamTrack):
        kind = "audio"

        def __init__(self, device_index, sample_rate, channels, frame_ms):
            super().__init__()
            self.device_index = device_index
            self.sample_rate = int(max(8000, sample_rate))
            self.channels = int(max(1, min(2, channels)))
            self.frame_ms = int(max(10, frame_ms))
            self.samples_per_frame = max(1, int(round(self.sample_rate * (self.frame_ms / 1000.0))))
            self.time_base = fractions.Fraction(1, self.sample_rate)
            self.pts = 0
            self.layout = "stereo" if self.channels > 1 else "mono"
            self.stream = None
            self.error = ""

            if AUDIO_BACKEND_AVAILABLE and self.device_index is not None:
                try:
                    self.stream = sd.InputStream(
                        device=int(self.device_index),
                        channels=self.channels,
                        samplerate=self.sample_rate,
                        dtype="int16",
                        blocksize=self.samples_per_frame,
                    )
                    self.stream.start()
                except Exception as exc:
                    self.error = str(exc)
                    self.stream = None

        async def recv(self):
            if self.stream is not None:
                try:
                    chunk, _ = await asyncio.to_thread(self.stream.read, self.samples_per_frame)
                    pcm = np.asarray(chunk, dtype=np.int16)
                    if pcm.ndim == 1:
                        pcm = pcm.reshape(-1, 1)
                    if pcm.shape[1] != self.channels:
                        if pcm.shape[1] > self.channels:
                            pcm = pcm[:, : self.channels]
                        else:
                            pad = np.zeros((pcm.shape[0], self.channels - pcm.shape[1]), dtype=np.int16)
                            pcm = np.concatenate([pcm, pad], axis=1)
                except Exception:
                    pcm = np.zeros((self.samples_per_frame, self.channels), dtype=np.int16)
            else:
                # Keep the connection alive even when no capture device is available.
                await asyncio.sleep(self.samples_per_frame / float(self.sample_rate))
                pcm = np.zeros((self.samples_per_frame, self.channels), dtype=np.int16)

            frame = AudioFrame(format="s16", layout=self.layout, samples=int(pcm.shape[0]))
            frame.planes[0].update(np.ascontiguousarray(pcm, dtype=np.int16).tobytes())
            frame.sample_rate = self.sample_rate
            frame.time_base = self.time_base
            frame.pts = self.pts
            self.pts += int(pcm.shape[0])
            return frame

        def stop(self):
            try:
                if self.stream is not None:
                    self.stream.stop()
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            super().stop()


    class SpeakerPlaybackSink:
        def __init__(self, device_index, sample_rate, channels, frame_ms, peer_id=""):
            self.device_index = device_index
            self.sample_rate = int(max(8000, sample_rate))
            self.channels = int(max(1, min(2, channels)))
            self.frame_ms = int(max(10, frame_ms))
            self.samples_per_frame = max(1, int(round(self.sample_rate * (self.frame_ms / 1000.0))))
            self.layout = "stereo" if self.channels > 1 else "mono"
            self.peer_id = str(peer_id)
            self.stream = None
            self.task = None
            self.running = False
            self.resampler = AudioResampler(format="s16", layout=self.layout, rate=self.sample_rate)

        def _open_stream(self):
            if self.stream is not None:
                return
            if not AUDIO_BACKEND_AVAILABLE:
                return
            if self.device_index is None:
                return
            self.stream = sd.OutputStream(
                device=int(self.device_index),
                channels=self.channels,
                samplerate=self.sample_rate,
                dtype="int16",
                blocksize=self.samples_per_frame,
            )
            self.stream.start()

        def start(self, track):
            if self.task is not None:
                return
            self.running = True
            self.task = asyncio.create_task(self._run(track))

        async def _run(self, track):
            try:
                try:
                    self._open_stream()
                except Exception as exc:
                    log(f"[AUDIO] Speaker stream open failed for {self.peer_id}: {exc}")
                    self.stream = None

                while self.running:
                    frame = await track.recv()
                    if self.stream is None:
                        continue

                    try:
                        resampled = self.resampler.resample(frame)
                    except Exception:
                        continue

                    if resampled is None:
                        continue
                    if not isinstance(resampled, list):
                        resampled = [resampled]

                    for out_frame in resampled:
                        if out_frame is None:
                            continue
                        try:
                            arr = out_frame.to_ndarray(format="s16")
                        except Exception:
                            continue

                        if arr.ndim == 1:
                            arr = arr[np.newaxis, :]
                        if arr.shape[0] != self.channels:
                            if arr.shape[0] > self.channels:
                                arr = arr[: self.channels, :]
                            else:
                                pad = np.zeros((self.channels - arr.shape[0], arr.shape[1]), dtype=np.int16)
                                arr = np.concatenate([arr, pad], axis=0)

                        pcm = np.ascontiguousarray(arr.T, dtype=np.int16)
                        await asyncio.to_thread(self.stream.write, pcm)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log(f"[AUDIO] Speaker sink error for {self.peer_id}: {exc}")
            finally:
                self.running = False
                try:
                    if self.stream is not None:
                        self.stream.stop()
                        self.stream.close()
                except Exception:
                    pass
                self.stream = None

        async def stop(self):
            self.running = False
            task = self.task
            self.task = None
            if task is not None:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass


async def _close_peer_connection(peer_id):
    with peer_contexts_lock:
        context = peer_contexts.pop(peer_id, None)
    if not context:
        return

    sink = context.get("sink")
    mic_track = context.get("mic_track")
    pc = context.get("pc")

    try:
        if sink is not None:
            await sink.stop()
    except Exception:
        pass

    try:
        if mic_track is not None:
            mic_track.stop()
    except Exception:
        pass

    try:
        if pc is not None:
            await pc.close()
    except Exception:
        pass


async def _close_all_peer_connections():
    with peer_contexts_lock:
        peer_ids = list(peer_contexts.keys())
    for peer_id in peer_ids:
        await _close_peer_connection(peer_id)


def active_peer_count():
    with peer_contexts_lock:
        return len(peer_contexts)


async def _wait_for_ice_complete(pc, timeout_seconds=3.5):
    deadline = time.time() + float(max(0.2, timeout_seconds))
    while time.time() < deadline:
        if pc.iceGatheringState == "complete":
            return
        await asyncio.sleep(0.05)


async def _create_webrtc_answer(offer_sdp, offer_type):
    if not WEBRTC_AVAILABLE:
        raise RuntimeError("WebRTC backend unavailable")

    # Keep audio routing deterministic: single active peer.
    await _close_all_peer_connections()

    selection = current_audio_selection()
    sample_rate = int(selection["sample_rate"])
    channels = int(selection["channels"])
    frame_ms = int(selection["frame_ms"])

    pc = RTCPeerConnection()
    peer_id = _next_peer_id()

    mic_track = None
    if WEBRTC_AVAILABLE:
        mic_track = MicrophoneAudioTrack(
            selection["input_index"],
            sample_rate=sample_rate,
            channels=channels,
            frame_ms=frame_ms,
        )
        pc.addTrack(mic_track)

    context = {
        "id": peer_id,
        "pc": pc,
        "mic_track": mic_track,
        "sink": None,
        "created_at": time.time(),
    }
    with peer_contexts_lock:
        peer_contexts[peer_id] = context

    @pc.on("connectionstatechange")
    async def _on_connectionstatechange():
        if pc.connectionState in ("closed", "failed", "disconnected"):
            await _close_peer_connection(peer_id)

    @pc.on("track")
    def _on_track(track):
        if track.kind != "audio":
            return

        sink = SpeakerPlaybackSink(
            selection["output_index"],
            sample_rate=sample_rate,
            channels=channels,
            frame_ms=frame_ms,
            peer_id=peer_id,
        )
        context["sink"] = sink
        sink.start(track)

        @track.on("ended")
        async def _on_ended():
            try:
                await sink.stop()
            except Exception:
                pass

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await _wait_for_ice_complete(pc)

    local = pc.localDescription
    if not local:
        raise RuntimeError("Missing local SDP answer")

    return {
        "sdp": local.sdp,
        "type": local.type,
        "peer_id": peer_id,
        "audio": {
            "input_index": selection["input_index"],
            "output_index": selection["output_index"],
            "sample_rate": sample_rate,
            "channels": channels,
            "frame_ms": frame_ms,
        },
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.before_request
def _count_requests():
    request_count["value"] += 1


def _index_payload():
    return {
        "status": "ok",
        "service": "audio_router",
        "routes": {
            "health": "/health",
            "auth": "/auth",
            "session_rotate": "/session/rotate",
            "list": "/list",
            "devices": "/devices",
            "devices_select": "/devices/select",
            "webrtc_offer": "/webrtc/offer",
            "webrtc_player": "/webrtc/player",
            "router_info": "/router_info",
            "tunnel_info": "/tunnel_info",
        },
    }


@app.route("/", methods=["GET"])
def index():
    if request.args.get("format", "").lower() == "json":
        return jsonify(_index_payload())
    accept = str(request.headers.get("Accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return jsonify(_index_payload())

    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Audio Router</title>
    <style>
      body {{ font-family: monospace; background: #111; color: #f0f0f0; margin: 0; }}
      .wrap {{ max-width: 980px; margin: 0 auto; padding: 1rem; }}
      .card {{ border: 1px solid #333; border-radius: 10px; padding: 1rem; background: #1b1b1b; }}
      .ok {{ color: #00d08a; }}
      .warn {{ color: #ffcc66; }}
      code {{ color: #ffcc66; }}
    </style>
  </head>
  <body>
    <div class=\"wrap\">
      <div class=\"card\">
        <h2 style=\"margin-top:0\">Audio Router</h2>
        <p>Service is running.</p>
        <p>Use <code>/auth</code> then <code>/list</code> and <code>/webrtc/offer</code> for client integration.</p>
        <p class=\"warn\">WebRTC: {'enabled' if WEBRTC_AVAILABLE else 'disabled'} | Audio backend: {'enabled' if AUDIO_BACKEND_AVAILABLE else 'disabled'}</p>
      </div>
    </div>
  </body>
</html>
"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api", methods=["GET"])
def api_index():
    return jsonify(_index_payload())


@app.route("/auth", methods=["POST"])
def auth():
    payload = request.get_json(silent=True) or {}
    provided = str(payload.get("password", ""))
    if provided == runtime_security["password"]:
        session_key = create_session()
        log(f"New audio session created: {session_key[:8]}...")
        return jsonify({"status": "success", "session_key": session_key, "timeout": SESSION_TIMEOUT})
    log("Authentication failed: invalid password")
    return jsonify({"status": "error", "message": "Invalid password"}), 401


@app.route("/session/rotate", methods=["POST"])
@require_session
def rotate_session():
    next_session_key, invalidated = rotate_sessions()
    log(f"Audio session keys rotated: invalidated={invalidated}")
    return jsonify(
        {
            "status": "success",
            "message": "Session keys rotated",
            "session_key": next_session_key,
            "timeout": SESSION_TIMEOUT,
            "invalidated_sessions": int(invalidated),
        }
    )


def _build_list_payload():
    selection = current_audio_selection()
    catalog = _build_device_catalog()
    process_running = tunnel_process is not None and tunnel_process.poll() is None
    with tunnel_url_lock:
        current_tunnel = tunnel_url if process_running else None

    return {
        "status": "success",
        "audio": {
            "sample_rate": int(selection["sample_rate"]),
            "channels": int(selection["channels"]),
            "frame_ms": int(selection["frame_ms"]),
            "input_device": _device_setting_for_json(selection["input_device"]),
            "output_device": _device_setting_for_json(selection["output_device"]),
            "input_index": selection["input_index"],
            "output_index": selection["output_index"],
            "input_device_info": selection["input_device_info"],
            "output_device_info": selection["output_device_info"],
        },
        "devices": catalog,
        "protocols": {
            "webrtc": WEBRTC_AVAILABLE,
            "webrtc_error": WEBRTC_IMPORT_ERROR if not WEBRTC_AVAILABLE else "",
            "audio_backend": AUDIO_BACKEND_AVAILABLE,
            "audio_backend_error": AUDIO_BACKEND_ERROR if not AUDIO_BACKEND_AVAILABLE else "",
        },
        "connections": {
            "active_webrtc_peers": active_peer_count(),
        },
        "session_timeout": SESSION_TIMEOUT,
        "tunnel_url": current_tunnel,
        "routes": {
            "auth": "/auth",
            "session_rotate": "/session/rotate",
            "health": "/health",
            "list": "/list",
            "devices": "/devices",
            "devices_select": "/devices/select",
            "webrtc_offer": "/webrtc/offer",
            "webrtc_player": "/webrtc/player",
            "router_info": "/router_info",
        },
    }


@app.route("/list", methods=["GET"])
@require_session
def list_audio():
    return jsonify(_build_list_payload())


@app.route("/devices", methods=["GET"])
@require_session
def list_devices():
    payload = _build_list_payload()
    return jsonify(
        {
            "status": "success",
            "devices": payload.get("devices", {}),
            "audio": payload.get("audio", {}),
            "connections": payload.get("connections", {}),
        }
    )


@app.route("/devices/select", methods=["POST"])
@require_session
def select_devices():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Payload must be an object"}), 400

    input_value = _MISSING
    output_value = _MISSING
    if "input_device" in payload:
        input_value = payload.get("input_device")
    if "output_device" in payload:
        output_value = payload.get("output_device")

    if input_value is _MISSING and output_value is _MISSING:
        return jsonify(
            {
                "status": "error",
                "message": "Provide input_device and/or output_device",
            }
        ), 400

    _set_runtime_audio_devices(input_value=input_value, output_value=output_value)
    _persist_audio_device_settings(input_value, output_value)

    if WEBRTC_AVAILABLE and active_peer_count() > 0:
        try:
            run_webrtc_coro(_close_all_peer_connections(), timeout=6.0)
        except Exception:
            pass

    updated = _build_list_payload()
    return jsonify(
        {
            "status": "success",
            "message": "Audio device settings updated",
            "audio": updated.get("audio", {}),
            "devices": updated.get("devices", {}),
            "connections": updated.get("connections", {}),
        }
    )


@app.route("/health", methods=["GET"])
def health():
    with sessions_lock:
        session_count = len(sessions)
    process_running = tunnel_process is not None and tunnel_process.poll() is None
    with tunnel_url_lock:
        current_tunnel = tunnel_url if process_running else None
        current_error = tunnel_last_error

    selection = current_audio_selection()
    return jsonify(
        {
            "status": "ok",
            "service": "audio_router",
            "uptime_seconds": round(time.time() - startup_time, 2),
            "require_auth": runtime_security["require_auth"],
            "sessions_active": int(session_count),
            "requests_served": int(request_count["value"]),
            "webrtc_available": WEBRTC_AVAILABLE,
            "webrtc_error": WEBRTC_IMPORT_ERROR,
            "audio_backend_available": AUDIO_BACKEND_AVAILABLE,
            "audio_backend_error": AUDIO_BACKEND_ERROR,
            "active_webrtc_peers": int(active_peer_count()),
            "tunnel_running": process_running,
            "tunnel_error": current_error,
            "tunnel_url": current_tunnel,
            "audio": {
                "sample_rate": int(selection["sample_rate"]),
                "channels": int(selection["channels"]),
                "frame_ms": int(selection["frame_ms"]),
                "input_device": _device_setting_for_json(selection["input_device"]),
                "output_device": _device_setting_for_json(selection["output_device"]),
                "input_index": selection["input_index"],
                "output_index": selection["output_index"],
            },
        }
    )


@app.route("/webrtc/offer", methods=["POST"])
@require_session
def webrtc_offer():
    if not WEBRTC_AVAILABLE:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "WebRTC backend unavailable",
                    "detail": WEBRTC_IMPORT_ERROR,
                }
            ),
            503,
        )

    payload = request.get_json(silent=True) or {}
    offer_sdp = str(payload.get("sdp", "")).strip()
    offer_type = str(payload.get("type", "")).strip()
    if not offer_sdp or not offer_type:
        return jsonify({"status": "error", "message": "Missing SDP offer"}), 400

    if not AUDIO_BACKEND_AVAILABLE:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Audio backend unavailable",
                    "detail": AUDIO_BACKEND_ERROR,
                }
            ),
            503,
        )

    try:
        answer = run_webrtc_coro(_create_webrtc_answer(offer_sdp, offer_type), timeout=18.0)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"WebRTC negotiation failed: {exc}"}), 500

    return jsonify({"status": "success", "answer": {"sdp": answer["sdp"], "type": answer["type"]}, "peer_id": answer.get("peer_id", "")})


@app.route("/webrtc/player", methods=["GET"])
@require_session
def webrtc_player():
    html = """
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Audio Router WebRTC Player</title>
    <style>
      body { margin: 0; background: #111; color: #fff; font-family: monospace; }
      .wrap { max-width: 900px; margin: 0 auto; padding: 1rem; }
      button { padding: 0.5rem 0.8rem; }
      audio { width: 100%; margin-top: 0.75rem; }
      .meta { opacity: 0.85; }
    </style>
  </head>
  <body>
    <div class=\"wrap\">
      <h2>Audio Router WebRTC</h2>
      <p class=\"meta\">Click Start, allow microphone access, and keep this page open.</p>
      <button id=\"startBtn\" type=\"button\">Start</button>
      <button id=\"stopBtn\" type=\"button\">Stop</button>
      <audio id=\"remoteAudio\" autoplay controls playsinline></audio>
      <p id=\"status\" class=\"meta\">Idle</p>
    </div>
    <script>
      const params = new URLSearchParams(window.location.search);
      const sessionKey = params.get("session_key") || "";
      const statusEl = document.getElementById("status");
      const remoteAudio = document.getElementById("remoteAudio");
      let pc = null;
      let localStream = null;

      function setStatus(msg, isError = false) {
        statusEl.textContent = String(msg || "");
        statusEl.style.color = isError ? "#ff6666" : "#ffffff";
      }

      async function stopBridge() {
        if (pc) {
          try { pc.ontrack = null; } catch (_) {}
          try { pc.getSenders().forEach((s) => { if (s.track) s.track.stop(); }); } catch (_) {}
          try { pc.close(); } catch (_) {}
          pc = null;
        }
        if (localStream) {
          try { localStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
          localStream = null;
        }
        remoteAudio.srcObject = null;
        setStatus("Stopped");
      }

      async function startBridge() {
        await stopBridge();
        try {
          setStatus("Requesting microphone permission...");
          localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });

          pc = new RTCPeerConnection();
          localStream.getAudioTracks().forEach((track) => pc.addTrack(track, localStream));
          pc.addTransceiver("audio", { direction: "recvonly" });
          pc.ontrack = (event) => {
            if (event.streams && event.streams[0]) {
              remoteAudio.srcObject = event.streams[0];
            }
          };

          const offer = await pc.createOffer();
          await pc.setLocalDescription(offer);

          const url = `/webrtc/offer?session_key=${encodeURIComponent(sessionKey)}`;
          const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
          });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            throw new Error(data.message || `HTTP ${res.status}`);
          }

          await pc.setRemoteDescription(data.answer);
          setStatus("Bidirectional audio active");
        } catch (err) {
          setStatus(`Start failed: ${err}`, true);
          await stopBridge();
        }
      }

      document.getElementById("startBtn").addEventListener("click", () => startBridge());
      document.getElementById("stopBtn").addEventListener("click", () => stopBridge());
      window.addEventListener("beforeunload", () => { stopBridge(); });
    </script>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.route("/tunnel_info", methods=["GET"])
def tunnel_info():
    process_running = tunnel_process is not None and tunnel_process.poll() is None
    with tunnel_url_lock:
        current_tunnel = tunnel_url if process_running else None
        stale_tunnel = tunnel_url if (tunnel_url and not process_running) else None
    listen_port = int(network_runtime.get("listen_port", DEFAULT_LISTEN_PORT))
    fallback_payload = _audio_fallback_payload(current_tunnel or "", process_running, listen_port)

    if current_tunnel:
        return jsonify(
            {
                "status": "success",
                "tunnel_url": current_tunnel,
                "running": process_running,
                "fallback": fallback_payload,
                "message": "Tunnel URL available",
            }
        )
    if stale_tunnel:
        return jsonify(
            {
                "status": "error",
                "running": process_running,
                "tunnel_url": "",
                "stale_tunnel_url": stale_tunnel,
                "error": tunnel_last_error or "Tunnel URL expired",
                "fallback": fallback_payload,
                "message": "Tunnel process is not running; URL is stale",
            }
        )
    if tunnel_last_error:
        return jsonify(
            {
                "status": "error",
                "running": process_running,
                "error": tunnel_last_error,
                "fallback": fallback_payload,
                "message": "Tunnel failed to start",
            }
        )
    return jsonify(
        {
            "status": "pending",
            "running": process_running,
            "fallback": fallback_payload,
            "message": "Tunnel URL not yet available",
        }
    )


@app.route("/router_info", methods=["GET"])
def router_info():
    process_running = tunnel_process is not None and tunnel_process.poll() is None
    with tunnel_url_lock:
        current_tunnel = tunnel_url if process_running else ""
        stale_tunnel = tunnel_url if (tunnel_url and not process_running) else ""
        current_error = tunnel_last_error

    listen_port = int(network_runtime.get("listen_port", DEFAULT_LISTEN_PORT))
    listen_host = str(network_runtime.get("listen_host", DEFAULT_LISTEN_HOST))
    local_base = f"http://127.0.0.1:{listen_port}"
    fallback_payload = _audio_fallback_payload(current_tunnel, process_running, listen_port)
    selected_transport = str(fallback_payload.get("selected_transport") or "local").strip().lower()
    upnp_payload = fallback_payload.get("upnp", {}) if isinstance(fallback_payload, dict) else {}
    upnp_base = str((upnp_payload or {}).get("public_base_url") or "").strip()
    selected_base = current_tunnel or (upnp_base if selected_transport == "upnp" else "") or local_base
    tunnel_state = "active" if (process_running and current_tunnel) else ("starting" if process_running else "inactive")
    if stale_tunnel and not process_running:
        tunnel_state = "stale"
    if current_error and not process_running and not current_tunnel and not stale_tunnel:
        tunnel_state = "error"

    selection = current_audio_selection()

    return jsonify(
        {
            "status": "success",
            "service": "audio_router",
            "transport": selected_transport,
            "base_url": selected_base,
            "local": {
                "base_url": local_base,
                "listen_host": listen_host,
                "listen_port": listen_port,
                "auth_url": f"{local_base}/auth",
                "list_url": f"{local_base}/list",
                "health_url": f"{local_base}/health",
                "webrtc_offer_url": f"{local_base}/webrtc/offer",
            },
            "tunnel": {
                "state": tunnel_state,
                "tunnel_url": current_tunnel,
                "list_url": f"{current_tunnel}/list" if current_tunnel else "",
                "health_url": f"{current_tunnel}/health" if current_tunnel else "",
                "webrtc_offer_url": f"{current_tunnel}/webrtc/offer" if current_tunnel else "",
                "stale_tunnel_url": stale_tunnel,
                "error": current_error,
            },
            "fallback": fallback_payload,
            "security": {
                "require_auth": bool(runtime_security["require_auth"]),
                "session_timeout": int(SESSION_TIMEOUT),
            },
            "audio": {
                "sample_rate": int(selection["sample_rate"]),
                "channels": int(selection["channels"]),
                "frame_ms": int(selection["frame_ms"]),
                "input_device": _device_setting_for_json(selection["input_device"]),
                "output_device": _device_setting_for_json(selection["output_device"]),
                "input_device_info": selection["input_device_info"],
                "output_device_info": selection["output_device_info"],
            },
        }
    )


# ---------------------------------------------------------------------------
# Runtime utility threads
# ---------------------------------------------------------------------------
def session_cleanup_loop():
    while service_running.is_set():
        cleanup_expired_sessions()
        time.sleep(5)


def metrics_update_loop():
    while ui and ui.running:
        with sessions_lock:
            session_count = len(sessions)

        process_running = tunnel_process is not None and tunnel_process.poll() is None
        with tunnel_url_lock:
            current_tunnel = tunnel_url if process_running else ""
            current_error = tunnel_last_error

        selection = current_audio_selection()
        in_name = (selection.get("input_device_info") or {}).get("name") or "N/A"
        out_name = (selection.get("output_device_info") or {}).get("name") or "N/A"

        ui.update_metric("Peers", str(active_peer_count()))
        ui.update_metric("Sessions", str(session_count))
        ui.update_metric("Requests", str(request_count["value"]))
        ui.update_metric("Input", in_name)
        ui.update_metric("Output", out_name)

        if current_tunnel:
            ui.update_metric("Tunnel", "Active")
            ui.update_metric("Tunnel URL", current_tunnel)
        elif current_error:
            ui.update_metric("Tunnel", f"Error: {current_error}")
        elif process_running:
            ui.update_metric("Tunnel", "Starting...")
        else:
            ui.update_metric("Tunnel", "Stopped")

        time.sleep(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global ui

    config = load_config()
    settings, changed = _load_audio_settings(config)
    if changed:
        save_config(config)

    apply_runtime_settings(settings)

    listen_host = settings["listen_host"]
    listen_port = settings["listen_port"]
    enable_tunnel = settings["enable_tunnel"]
    auto_install_cloudflared = settings["auto_install_cloudflared"]

    if WEBRTC_AVAILABLE:
        if not ensure_webrtc_loop():
            log("[WARN] Failed to initialize WebRTC loop; WebRTC routes may fail")

    threading.Thread(target=session_cleanup_loop, daemon=True).start()

    if UI_AVAILABLE:
        ui = TerminalUI("Audio Router", config_spec=_build_audio_config_spec(), config_path=CONFIG_PATH)
        ui.on_save(apply_runtime_settings_from_config)
        ui.log("Starting Audio Router...")

    if enable_tunnel:
        if not is_cloudflared_installed():
            if auto_install_cloudflared:
                log("Cloudflared not found, attempting install...")
                if not install_cloudflared():
                    log("Cloudflared install failed; tunnel disabled.")
                    enable_tunnel = False
            else:
                log("Cloudflared missing and auto-install disabled; tunnel disabled.")
                enable_tunnel = False
        if enable_tunnel:
            threading.Thread(target=lambda: (time.sleep(2), start_cloudflared_tunnel(listen_port)), daemon=True).start()
            log("Cloudflare Tunnel will be available shortly...")

    def fallback_refresh_loop():
        while service_running.is_set():
            process_running = tunnel_process is not None and tunnel_process.poll() is None
            with tunnel_url_lock:
                current_tunnel = tunnel_url if process_running else ""
            if not current_tunnel:
                _refresh_upnp_fallback(listen_port, force=False)
            time.sleep(max(15.0, float(UPNP_FALLBACK_REFRESH_SECONDS)))

    _refresh_upnp_fallback(listen_port, force=True)
    threading.Thread(target=fallback_refresh_loop, daemon=True).start()

    local_url = f"http://{listen_host}:{listen_port}"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
        lan_url = f"http://{lan_ip}:{listen_port}"
    except Exception:
        lan_url = "N/A"

    if ui:
        ui.update_metric("Local URL", local_url)
        ui.update_metric("LAN URL", lan_url)
        ui.update_metric("Peers", "0")
        ui.update_metric("Sessions", "0")
        ui.update_metric("Requests", "0")
        ui.update_metric("Auth", "Required" if runtime_security["require_auth"] else "Disabled")
        ui.update_metric("Session Timeout", str(SESSION_TIMEOUT))
        ui.update_metric("Tunnel", "Starting..." if enable_tunnel else "Disabled")

    log(f"Starting audio router on {local_url}")
    if lan_url != "N/A":
        log(f"LAN URL: {lan_url}")

    if not WEBRTC_AVAILABLE:
        log(f"[WARN] WebRTC unavailable: {WEBRTC_IMPORT_ERROR}")
    if not AUDIO_BACKEND_AVAILABLE:
        log(f"[WARN] Audio backend unavailable: {AUDIO_BACKEND_ERROR}")

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
        ui.running = True
        threading.Thread(target=metrics_update_loop, daemon=True).start()
        try:
            ui.start()
        finally:
            log("Shutting down audio router...")
            service_running.clear()
            stop_cloudflared_tunnel()
            stop_webrtc_loop()
    else:
        try:
            app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False, threaded=True)
        finally:
            service_running.clear()
            stop_cloudflared_tunnel()
            stop_webrtc_loop()


if __name__ == "__main__":
    main()
