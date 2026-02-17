#!/usr/bin/env python3
"""
Camera router service.

Capabilities:
- Adapter-style venv bootstrap (no reinstall on every run).
- Config-backed settings with nested camera_router.* keys.
- Password auth with expiring session keys.
- Optional terminal UI (terminal_ui.py).
- Cloudflared tunnel support.
- Efficient MJPEG streaming (single JPEG encode per frame, shared by all clients).
- Health and listing endpoints.
- Process supervisor to auto-restart after native crashes (e.g., RealSense aborts).
"""

import datetime
import glob
import json
import os
import platform
import re
import secrets
import signal
import shutil
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
CAMERA_VENV_DIR_NAME = "camera_route_venv"
CAMERA_CLOUDFLARED_BASENAME = "camera_route_cloudflared"
SUPERVISOR_ENV_CHILD = "CAMERA_ROUTE_CHILD"
SUPERVISOR_ENV_ENABLED = "CAMERA_ROUTE_SUPERVISE"
SUPERVISOR_ENV_SAFE_MODE = "CAMERA_ROUTE_SAFE_MODE"
SUPERVISOR_BACKOFF_MAX_SECONDS = 15.0
SUPERVISOR_CRASH_WINDOW_SECONDS = 120.0
SUPERVISOR_SAFE_MODE_AFTER_CRASHES = 3


def env_truthy(var_name, default=False):
    value = os.environ.get(var_name)
    if value is None:
        return bool(default)
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    return bool(default)


SAFE_MODE_ACTIVE = env_truthy(SUPERVISOR_ENV_SAFE_MODE, default=False)
# RealSense CUDA conversion asserts can crash the interpreter; safe mode forces CPU path.
if SAFE_MODE_ACTIVE:
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def ensure_venv():
    script_dir = os.path.abspath(os.path.dirname(__file__))
    venv_dir = os.path.join(script_dir, CAMERA_VENV_DIR_NAME)
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(venv_dir)):
        return

    if os.name == "nt":
        pip_path = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_path = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_path = os.path.join(venv_dir, "bin", "pip")
        python_path = os.path.join(venv_dir, "bin", "python")

    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment in '{CAMERA_VENV_DIR_NAME}'...")
        import venv

        venv.create(venv_dir, with_pip=True)
        print("Installing required packages (Flask, Flask-CORS, opencv-python, numpy)...")
        subprocess.check_call([pip_path, "install", "Flask", "Flask-CORS", "opencv-python", "numpy"])
        for optional_pkg in ("av", "aiortc"):
            try:
                print(f"Installing optional package ({optional_pkg})...")
                subprocess.check_call([pip_path, "install", optional_pkg])
            except Exception as exc:
                print(f"Optional package install failed ({optional_pkg}): {exc}")
    else:
        try:
            result = subprocess.run(
                [python_path, "-c", "import flask, flask_cors, cv2, numpy"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                print("Installing missing packages...")
                subprocess.check_call([pip_path, "install", "Flask", "Flask-CORS", "opencv-python", "numpy"])
        except Exception:
            print("Installing required packages (Flask, Flask-CORS, opencv-python, numpy)...")
            subprocess.check_call([pip_path, "install", "Flask", "Flask-CORS", "opencv-python", "numpy"])
        for optional_module, optional_pkg in (("av", "av"), ("aiortc", "aiortc")):
            try:
                check = subprocess.run(
                    [python_path, "-c", f"import {optional_module}"],
                    capture_output=True,
                    timeout=5,
                )
                if check.returncode != 0:
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
import cv2
from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context
from flask_cors import CORS

WEBRTC_AVAILABLE = False
WEBRTC_IMPORT_ERROR = ""
try:
    import asyncio
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from av import VideoFrame

    WEBRTC_AVAILABLE = True
except Exception as exc:
    WEBRTC_IMPORT_ERROR = str(exc)


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
# Optional RealSense support
# ---------------------------------------------------------------------------
REALSENSE_AVAILABLE = False
REALSENSE_IMPORT_ERROR = ""
RealsenseCapture = None
try:
    from realsensecv import RealsenseCapture

    REALSENSE_AVAILABLE = True
except Exception as exc:
    REALSENSE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    # Under supervisor mode the parent process only starts the child; avoid duplicate warnings.
    if env_truthy(SUPERVISOR_ENV_CHILD, default=False) or not env_truthy(
        SUPERVISOR_ENV_ENABLED, default=True
    ):
        print(f"Warning: RealSense unavailable: {REALSENSE_IMPORT_ERROR}")


# ---------------------------------------------------------------------------
# Defaults and runtime state
# ---------------------------------------------------------------------------
CONFIG_PATH = "config.json"

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8080

DEFAULT_PASSWORD = "camera2026"
DEFAULT_SESSION_TIMEOUT = 300
DEFAULT_REQUIRE_AUTH = True

DEFAULT_ENABLE_TUNNEL = True
DEFAULT_AUTO_INSTALL_CLOUDFLARED = True

DEFAULT_DEFAULT_CAMERAS_ENABLED = False
DEFAULT_CAMERA_DEVICE_GLOB = "/dev/video*"
DEFAULT_CAMERA_CAPTURE_WIDTH = 1280
DEFAULT_CAMERA_CAPTURE_HEIGHT = 720
DEFAULT_CAMERA_CAPTURE_FPS = 30
DEFAULT_DEFAULT_CAMERA_OPEN_RETRY_INITIAL_SECONDS = 2.0
DEFAULT_DEFAULT_CAMERA_OPEN_RETRY_MAX_SECONDS = 20.0
DEFAULT_DEFAULT_CAMERA_DISCONNECT_RETRY_SECONDS = 0.5
DEFAULT_STALE_CAMERA_TERM_WAIT_SECONDS = 2.0
DEFAULT_STALE_CAMERA_KILL_WAIT_SECONDS = 1.0

DEFAULT_REALSENSE_ENABLED = True
DEFAULT_REALSENSE_STREAM_DEPTH = True
DEFAULT_REALSENSE_STREAM_IR = True
DEFAULT_REALSENSE_START_ATTEMPTS = 4

DEFAULT_STREAM_MAX_WIDTH = 960
DEFAULT_STREAM_MAX_HEIGHT = 540
DEFAULT_STREAM_JPEG_QUALITY = 72
DEFAULT_STREAM_TARGET_FPS = 30
DEFAULT_ROTATE_CLOCKWISE = True
DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES = 90 if DEFAULT_ROTATE_CLOCKWISE else 0
DEFAULT_WEBRTC_TARGET_FPS = 24
DEFAULT_MPEGTS_TARGET_FPS = 24
DEFAULT_MPEGTS_JPEG_QUALITY = 60
DEFAULT_TUNNEL_RESTART_DELAY_SECONDS = 3.0

SESSION_TIMEOUT = DEFAULT_SESSION_TIMEOUT
runtime_security = {
    "password": DEFAULT_PASSWORD,
    "require_auth": DEFAULT_REQUIRE_AUTH,
}
stream_options = {
    "max_width": DEFAULT_STREAM_MAX_WIDTH,
    "max_height": DEFAULT_STREAM_MAX_HEIGHT,
    "jpeg_quality": DEFAULT_STREAM_JPEG_QUALITY,
    "target_fps": DEFAULT_STREAM_TARGET_FPS,
    "default_rotation_degrees": DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES,
    "rotate_clockwise": DEFAULT_ROTATE_CLOCKWISE,
    "webrtc_target_fps": DEFAULT_WEBRTC_TARGET_FPS,
    "mpegts_target_fps": DEFAULT_MPEGTS_TARGET_FPS,
    "mpegts_jpeg_quality": DEFAULT_MPEGTS_JPEG_QUALITY,
}
source_options = {
    "default_cameras_enabled": DEFAULT_DEFAULT_CAMERAS_ENABLED,
    "camera_device_glob": DEFAULT_CAMERA_DEVICE_GLOB,
    "camera_capture_width": DEFAULT_CAMERA_CAPTURE_WIDTH,
    "camera_capture_height": DEFAULT_CAMERA_CAPTURE_HEIGHT,
    "camera_capture_fps": DEFAULT_CAMERA_CAPTURE_FPS,
    "realsense_enabled": DEFAULT_REALSENSE_ENABLED,
    "realsense_stream_depth": DEFAULT_REALSENSE_STREAM_DEPTH,
    "realsense_stream_ir": DEFAULT_REALSENSE_STREAM_IR,
}
network_runtime = {
    "listen_host": DEFAULT_LISTEN_HOST,
    "listen_port": DEFAULT_LISTEN_PORT,
}


def should_enable_default_camera_fallback():
    """Enable default /dev/video fallback when no usable RealSense path exists."""
    if source_options["default_cameras_enabled"]:
        return False
    if os.name == "nt":
        return False
    if source_options["realsense_enabled"] and REALSENSE_AVAILABLE:
        return False
    return True

camera_feeds = {}
camera_feeds_lock = Lock()
capture_threads = []
service_running = threading.Event()
capture_threads_lock = Lock()
active_capture_handles = {}
active_capture_handles_lock = Lock()
camera_rotation_rules = {}
camera_rotation_rules_lock = Lock()
camera_enable_rules = {}
camera_enable_rules_lock = Lock()

imu_state = {}
imu_lock = Lock()

sessions = {}
sessions_lock = Lock()

request_count = {"value": 0}
startup_time = time.time()

tunnel_url = None
tunnel_url_lock = Lock()
tunnel_process = None
tunnel_last_error = ""
tunnel_desired = False
tunnel_restart_lock = Lock()

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
MPEGTS_AVAILABLE = shutil.which(FFMPEG_BIN) is not None
peer_connections = set()
peer_connections_lock = Lock()


def log(message):
    if ui and UI_AVAILABLE:
        ui.log(message)
    else:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}")


def _register_active_capture_handle(feed_id, handle):
    if handle is None:
        return
    key = str(feed_id or "")
    with active_capture_handles_lock:
        bucket = active_capture_handles.setdefault(key, [])
        if all(item is not handle for item in bucket):
            bucket.append(handle)


def _unregister_active_capture_handle(feed_id, handle):
    if handle is None:
        return
    key = str(feed_id or "")
    with active_capture_handles_lock:
        handles = active_capture_handles.get(key)
        if not handles:
            return
        active_capture_handles[key] = [item for item in handles if item is not handle]
        handles = active_capture_handles.get(key)
        if not handles:
            active_capture_handles.pop(key, None)


def _release_active_capture_handles(feed_id):
    key = str(feed_id or "").strip()
    if not key:
        return 0
    with active_capture_handles_lock:
        handles = list(active_capture_handles.pop(key, []))
    released = 0
    for handle in handles:
        try:
            handle.release()
            released += 1
        except Exception:
            pass
    return released


def _release_all_active_capture_handles():
    with active_capture_handles_lock:
        handles = []
        for bucket in active_capture_handles.values():
            handles.extend(bucket)
        active_capture_handles.clear()
    for handle in handles:
        try:
            handle.release()
        except Exception:
            pass


def _rotation_rule_keys_for_feed(feed):
    if not feed:
        return []
    keys = [str(feed.camera_id or "").strip()]
    device_path = str(feed.device_path or "").strip()
    if device_path:
        keys.append(device_path)
        keys.append(os.path.basename(device_path))
    return [item for item in keys if item]


def get_feed_rotation_degrees(feed):
    keys = _rotation_rule_keys_for_feed(feed)
    with camera_rotation_rules_lock:
        for key in keys:
            if key in camera_rotation_rules:
                return int(camera_rotation_rules[key])
    return _rotation_or_default(
        stream_options.get("default_rotation_degrees", DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES),
        DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES,
    )


def get_feed_enabled(feed):
    keys = _rotation_rule_keys_for_feed(feed)
    with camera_enable_rules_lock:
        for key in keys:
            if key in camera_enable_rules:
                return bool(camera_enable_rules[key])
    return True


def get_feed_enable_rule(feed):
    keys = _rotation_rule_keys_for_feed(feed)
    with camera_enable_rules_lock:
        for key in keys:
            if key in camera_enable_rules:
                return key, bool(camera_enable_rules[key])
    return None, None


def _persist_camera_enable_rules():
    config = load_config()
    with camera_enable_rules_lock:
        rules_copy = dict(camera_enable_rules)
    _set_nested(config, "camera_router.sources.camera_enabled", rules_copy)
    save_config(config)


def set_camera_enable_rule(rule_key, enabled, persist=True):
    key = str(rule_key or "").strip()
    if not key:
        raise ValueError("Missing rule key")
    normalized = _as_bool(enabled, default=None)
    if normalized is None:
        raise ValueError("enabled must be true or false")
    with camera_enable_rules_lock:
        camera_enable_rules[key] = bool(normalized)
    if persist:
        _persist_camera_enable_rules()
    return bool(normalized)


def clear_camera_enable_rule(rule_key, persist=True):
    key = str(rule_key or "").strip()
    if not key:
        return False
    changed = False
    with camera_enable_rules_lock:
        if key in camera_enable_rules:
            camera_enable_rules.pop(key, None)
            changed = True
    if changed and persist:
        _persist_camera_enable_rules()
    return changed


def _persist_camera_rotation_rules():
    config = load_config()
    with camera_rotation_rules_lock:
        rules_copy = dict(camera_rotation_rules)
    _set_nested(config, "camera_router.stream.camera_rotation_degrees", rules_copy)
    save_config(config)


def set_camera_rotation_rule(rule_key, rotation_degrees, persist=True):
    key = str(rule_key or "").strip()
    if not key:
        raise ValueError("Missing rule key")
    normalized = _parse_rotation_degrees(rotation_degrees)
    if normalized is None:
        raise ValueError("rotation_degrees must be one of 0, 90, 180, 270")
    with camera_rotation_rules_lock:
        camera_rotation_rules[key] = normalized
    if persist:
        _persist_camera_rotation_rules()
    return normalized


def clear_camera_rotation_rule(rule_key, persist=True):
    key = str(rule_key or "").strip()
    if not key:
        return False
    changed = False
    with camera_rotation_rules_lock:
        if key in camera_rotation_rules:
            camera_rotation_rules.pop(key, None)
            changed = True
    if changed and persist:
        _persist_camera_rotation_rules()
    return changed


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


def _parse_rotation_degrees(value):
    if isinstance(value, bool):
        return 90 if value else 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed in (0, 90, 180, 270) else None


def _rotation_or_default(value, default=0):
    parsed = _parse_rotation_degrees(value)
    fallback = _parse_rotation_degrees(default)
    if parsed is not None:
        return parsed
    return fallback if fallback is not None else 0


def _normalize_rotation_rules(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, rule_value in value.items():
        normalized = _parse_rotation_degrees(rule_value)
        if normalized is None:
            continue
        rule_key = str(key or "").strip()
        if not rule_key:
            continue
        clean[rule_key] = normalized
    return clean


def _normalize_camera_enable_rules(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, rule_value in value.items():
        normalized = _as_bool(rule_value, default=None)
        if normalized is None:
            continue
        rule_key = str(key or "").strip()
        if not rule_key:
            continue
        clean[rule_key] = bool(normalized)
    return clean


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


def _load_camera_settings(config):
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
            "camera_router.network.listen_host",
            DEFAULT_LISTEN_HOST,
            legacy_keys=("host", "listen_host", "camera_host"),
        )
    ).strip() or DEFAULT_LISTEN_HOST
    promote("camera_router.network.listen_host", listen_host)

    listen_port = _as_int(
        _read_config_value(
            config,
            "camera_router.network.listen_port",
            DEFAULT_LISTEN_PORT,
            legacy_keys=("port", "listen_port", "camera_port"),
        ),
        DEFAULT_LISTEN_PORT,
        minimum=1,
        maximum=65535,
    )
    promote("camera_router.network.listen_port", listen_port)

    password = str(
        _read_config_value(
            config,
            "camera_router.security.password",
            DEFAULT_PASSWORD,
            legacy_keys=("password",),
        )
    ).strip() or DEFAULT_PASSWORD
    promote("camera_router.security.password", password)

    session_timeout = _as_int(
        _read_config_value(
            config,
            "camera_router.security.session_timeout",
            DEFAULT_SESSION_TIMEOUT,
            legacy_keys=("session_timeout",),
        ),
        DEFAULT_SESSION_TIMEOUT,
        minimum=30,
        maximum=86400,
    )
    promote("camera_router.security.session_timeout", session_timeout)

    require_auth = _as_bool(
        _read_config_value(
            config,
            "camera_router.security.require_auth",
            DEFAULT_REQUIRE_AUTH,
            legacy_keys=("require_auth",),
        ),
        default=DEFAULT_REQUIRE_AUTH,
    )
    promote("camera_router.security.require_auth", require_auth)

    enable_tunnel = _as_bool(
        _read_config_value(
            config,
            "camera_router.tunnel.enable",
            DEFAULT_ENABLE_TUNNEL,
            legacy_keys=("enable_tunnel",),
        ),
        default=DEFAULT_ENABLE_TUNNEL,
    )
    promote("camera_router.tunnel.enable", enable_tunnel)

    auto_install_cloudflared = _as_bool(
        _read_config_value(
            config,
            "camera_router.tunnel.auto_install_cloudflared",
            DEFAULT_AUTO_INSTALL_CLOUDFLARED,
            legacy_keys=("auto_install_cloudflared",),
        ),
        default=DEFAULT_AUTO_INSTALL_CLOUDFLARED,
    )
    promote("camera_router.tunnel.auto_install_cloudflared", auto_install_cloudflared)

    default_cameras_enabled = _as_bool(
        _read_config_value(
            config,
            "camera_router.sources.default.enable",
            DEFAULT_DEFAULT_CAMERAS_ENABLED,
        ),
        default=DEFAULT_DEFAULT_CAMERAS_ENABLED,
    )
    promote("camera_router.sources.default.enable", default_cameras_enabled)

    camera_device_glob = str(
        _read_config_value(
            config,
            "camera_router.sources.default.device_glob",
            DEFAULT_CAMERA_DEVICE_GLOB,
        )
    ).strip() or DEFAULT_CAMERA_DEVICE_GLOB
    promote("camera_router.sources.default.device_glob", camera_device_glob)

    camera_capture_width = _as_int(
        _read_config_value(
            config,
            "camera_router.sources.default.capture_width",
            DEFAULT_CAMERA_CAPTURE_WIDTH,
        ),
        DEFAULT_CAMERA_CAPTURE_WIDTH,
        minimum=160,
        maximum=3840,
    )
    promote("camera_router.sources.default.capture_width", camera_capture_width)

    camera_capture_height = _as_int(
        _read_config_value(
            config,
            "camera_router.sources.default.capture_height",
            DEFAULT_CAMERA_CAPTURE_HEIGHT,
        ),
        DEFAULT_CAMERA_CAPTURE_HEIGHT,
        minimum=120,
        maximum=2160,
    )
    promote("camera_router.sources.default.capture_height", camera_capture_height)

    camera_capture_fps = _as_int(
        _read_config_value(
            config,
            "camera_router.sources.default.capture_fps",
            DEFAULT_CAMERA_CAPTURE_FPS,
        ),
        DEFAULT_CAMERA_CAPTURE_FPS,
        minimum=1,
        maximum=240,
    )
    promote("camera_router.sources.default.capture_fps", camera_capture_fps)

    realsense_enabled = _as_bool(
        _read_config_value(
            config,
            "camera_router.sources.realsense.enable",
            DEFAULT_REALSENSE_ENABLED,
        ),
        default=DEFAULT_REALSENSE_ENABLED,
    )
    promote("camera_router.sources.realsense.enable", realsense_enabled)

    realsense_stream_depth = _as_bool(
        _read_config_value(
            config,
            "camera_router.sources.realsense.stream_depth",
            DEFAULT_REALSENSE_STREAM_DEPTH,
        ),
        default=DEFAULT_REALSENSE_STREAM_DEPTH,
    )
    promote("camera_router.sources.realsense.stream_depth", realsense_stream_depth)

    realsense_stream_ir = _as_bool(
        _read_config_value(
            config,
            "camera_router.sources.realsense.stream_ir",
            DEFAULT_REALSENSE_STREAM_IR,
        ),
        default=DEFAULT_REALSENSE_STREAM_IR,
    )
    promote("camera_router.sources.realsense.stream_ir", realsense_stream_ir)

    stream_max_width = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.max_width",
            DEFAULT_STREAM_MAX_WIDTH,
        ),
        DEFAULT_STREAM_MAX_WIDTH,
        minimum=160,
        maximum=3840,
    )
    promote("camera_router.stream.max_width", stream_max_width)

    stream_max_height = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.max_height",
            DEFAULT_STREAM_MAX_HEIGHT,
        ),
        DEFAULT_STREAM_MAX_HEIGHT,
        minimum=120,
        maximum=2160,
    )
    promote("camera_router.stream.max_height", stream_max_height)

    stream_jpeg_quality = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.jpeg_quality",
            DEFAULT_STREAM_JPEG_QUALITY,
        ),
        DEFAULT_STREAM_JPEG_QUALITY,
        minimum=30,
        maximum=95,
    )
    promote("camera_router.stream.jpeg_quality", stream_jpeg_quality)

    stream_target_fps = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.target_fps",
            DEFAULT_STREAM_TARGET_FPS,
        ),
        DEFAULT_STREAM_TARGET_FPS,
        minimum=1,
        maximum=240,
    )
    promote("camera_router.stream.target_fps", stream_target_fps)

    webrtc_target_fps = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.webrtc_target_fps",
            DEFAULT_WEBRTC_TARGET_FPS,
        ),
        DEFAULT_WEBRTC_TARGET_FPS,
        minimum=1,
        maximum=120,
    )
    promote("camera_router.stream.webrtc_target_fps", webrtc_target_fps)

    mpegts_target_fps = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.mpegts_target_fps",
            DEFAULT_MPEGTS_TARGET_FPS,
        ),
        DEFAULT_MPEGTS_TARGET_FPS,
        minimum=1,
        maximum=120,
    )
    promote("camera_router.stream.mpegts_target_fps", mpegts_target_fps)

    mpegts_jpeg_quality = _as_int(
        _read_config_value(
            config,
            "camera_router.stream.mpegts_jpeg_quality",
            DEFAULT_MPEGTS_JPEG_QUALITY,
        ),
        DEFAULT_MPEGTS_JPEG_QUALITY,
        minimum=30,
        maximum=95,
    )
    promote("camera_router.stream.mpegts_jpeg_quality", mpegts_jpeg_quality)

    rotate_clockwise = _as_bool(
        _read_config_value(
            config,
            "camera_router.stream.rotate_clockwise",
            DEFAULT_ROTATE_CLOCKWISE,
        ),
        default=DEFAULT_ROTATE_CLOCKWISE,
    )
    default_rotation_degrees = _rotation_or_default(
        _read_config_value(
            config,
            "camera_router.stream.default_rotation_degrees",
            90 if rotate_clockwise else 0,
        ),
        default=90 if rotate_clockwise else 0,
    )
    rotate_clockwise = default_rotation_degrees == 90
    promote("camera_router.stream.rotate_clockwise", rotate_clockwise)
    promote("camera_router.stream.default_rotation_degrees", default_rotation_degrees)

    camera_rotation_rules_value = _read_config_value(
        config,
        "camera_router.stream.camera_rotation_degrees",
        {},
    )
    camera_rotation_degrees = _normalize_rotation_rules(camera_rotation_rules_value)
    promote("camera_router.stream.camera_rotation_degrees", camera_rotation_degrees)

    camera_enabled_value = _read_config_value(
        config,
        "camera_router.sources.camera_enabled",
        {},
    )
    camera_enabled = _normalize_camera_enable_rules(camera_enabled_value)
    promote("camera_router.sources.camera_enabled", camera_enabled)

    return {
        "listen_host": listen_host,
        "listen_port": listen_port,
        "password": password,
        "session_timeout": session_timeout,
        "require_auth": require_auth,
        "enable_tunnel": enable_tunnel,
        "auto_install_cloudflared": auto_install_cloudflared,
        "default_cameras_enabled": default_cameras_enabled,
        "camera_device_glob": camera_device_glob,
        "camera_capture_width": camera_capture_width,
        "camera_capture_height": camera_capture_height,
        "camera_capture_fps": camera_capture_fps,
        "realsense_enabled": realsense_enabled,
        "realsense_stream_depth": realsense_stream_depth,
        "realsense_stream_ir": realsense_stream_ir,
        "stream_max_width": stream_max_width,
        "stream_max_height": stream_max_height,
        "stream_jpeg_quality": stream_jpeg_quality,
        "stream_target_fps": stream_target_fps,
        "webrtc_target_fps": webrtc_target_fps,
        "mpegts_target_fps": mpegts_target_fps,
        "mpegts_jpeg_quality": mpegts_jpeg_quality,
        "rotate_clockwise": rotate_clockwise,
        "default_rotation_degrees": default_rotation_degrees,
        "camera_rotation_degrees": camera_rotation_degrees,
        "camera_enabled": camera_enabled,
    }, changed


def _build_camera_config_spec():
    if not UI_AVAILABLE:
        return None
    return ConfigSpec(
        label="Camera Router",
        categories=(
            CategorySpec(
                id="network",
                label="Network",
                settings=(
                    SettingSpec(
                        id="listen_host",
                        label="Listen Host",
                        path="camera_router.network.listen_host",
                        value_type="str",
                        default=DEFAULT_LISTEN_HOST,
                        description="HTTP bind host.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="listen_port",
                        label="Listen Port",
                        path="camera_router.network.listen_port",
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
                        path="camera_router.security.password",
                        value_type="secret",
                        default=DEFAULT_PASSWORD,
                        description="Password used by /auth.",
                    ),
                    SettingSpec(
                        id="session_timeout",
                        label="Session Timeout",
                        path="camera_router.security.session_timeout",
                        value_type="int",
                        default=DEFAULT_SESSION_TIMEOUT,
                        min_value=30,
                        max_value=86400,
                        description="Session expiration in seconds.",
                    ),
                    SettingSpec(
                        id="require_auth",
                        label="Require Auth",
                        path="camera_router.security.require_auth",
                        value_type="bool",
                        default=DEFAULT_REQUIRE_AUTH,
                        description="Protect list/camera/video/imu routes.",
                    ),
                ),
            ),
            CategorySpec(
                id="stream",
                label="Stream",
                settings=(
                    SettingSpec(
                        id="max_width",
                        label="Max Width",
                        path="camera_router.stream.max_width",
                        value_type="int",
                        default=DEFAULT_STREAM_MAX_WIDTH,
                        min_value=160,
                        max_value=3840,
                        description="Encoded stream max width.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="max_height",
                        label="Max Height",
                        path="camera_router.stream.max_height",
                        value_type="int",
                        default=DEFAULT_STREAM_MAX_HEIGHT,
                        min_value=120,
                        max_value=2160,
                        description="Encoded stream max height.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="jpeg_quality",
                        label="JPEG Quality",
                        path="camera_router.stream.jpeg_quality",
                        value_type="int",
                        default=DEFAULT_STREAM_JPEG_QUALITY,
                        min_value=30,
                        max_value=95,
                        description="JPEG quality (lower is faster/smaller).",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="target_fps",
                        label="Target FPS",
                        path="camera_router.stream.target_fps",
                        value_type="int",
                        default=DEFAULT_STREAM_TARGET_FPS,
                        min_value=1,
                        max_value=240,
                        description="Publish cap; drops frames instead of buffering.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="webrtc_target_fps",
                        label="WebRTC FPS",
                        path="camera_router.stream.webrtc_target_fps",
                        value_type="int",
                        default=DEFAULT_WEBRTC_TARGET_FPS,
                        min_value=1,
                        max_value=120,
                        description="Target FPS for WebRTC track pacing.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="mpegts_target_fps",
                        label="MPEG-TS FPS",
                        path="camera_router.stream.mpegts_target_fps",
                        value_type="int",
                        default=DEFAULT_MPEGTS_TARGET_FPS,
                        min_value=1,
                        max_value=120,
                        description="Target FPS fed into MPEG-TS transcoder.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="mpegts_jpeg_quality",
                        label="MPEG-TS Input Q",
                        path="camera_router.stream.mpegts_jpeg_quality",
                        value_type="int",
                        default=DEFAULT_MPEGTS_JPEG_QUALITY,
                        min_value=30,
                        max_value=95,
                        description="JPEG quality used for MPEG-TS feeder frames.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="default_rotation_degrees",
                        label="Default Rotation",
                        path="camera_router.stream.default_rotation_degrees",
                        value_type="int",
                        default=DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES,
                        min_value=0,
                        max_value=270,
                        description="Default camera rotation degrees (allowed: 0, 90, 180, 270).",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="camera_rotation_rules",
                        label="Camera Rotation Rules",
                        path="camera_router.stream.camera_rotation_degrees",
                        value_type="str",
                        default="{}",
                        description='JSON map like {"default_0":90,"rs_color":180}.',
                        restart_required=True,
                    ),
                ),
            ),
            CategorySpec(
                id="sources",
                label="Sources",
                settings=(
                    SettingSpec(
                        id="default_enable",
                        label="Default Cameras",
                        path="camera_router.sources.default.enable",
                        value_type="bool",
                        default=DEFAULT_DEFAULT_CAMERAS_ENABLED,
                        description="Enable /dev/video* workers.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="device_glob",
                        label="Device Glob",
                        path="camera_router.sources.default.device_glob",
                        value_type="str",
                        default=DEFAULT_CAMERA_DEVICE_GLOB,
                        description="Camera discovery glob pattern.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="capture_width",
                        label="Capture Width",
                        path="camera_router.sources.default.capture_width",
                        value_type="int",
                        default=DEFAULT_CAMERA_CAPTURE_WIDTH,
                        min_value=160,
                        max_value=3840,
                        description="Requested default camera width.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="capture_height",
                        label="Capture Height",
                        path="camera_router.sources.default.capture_height",
                        value_type="int",
                        default=DEFAULT_CAMERA_CAPTURE_HEIGHT,
                        min_value=120,
                        max_value=2160,
                        description="Requested default camera height.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="capture_fps",
                        label="Capture FPS",
                        path="camera_router.sources.default.capture_fps",
                        value_type="int",
                        default=DEFAULT_CAMERA_CAPTURE_FPS,
                        min_value=1,
                        max_value=240,
                        description="Requested default camera FPS.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="realsense_enable",
                        label="RealSense",
                        path="camera_router.sources.realsense.enable",
                        value_type="bool",
                        default=DEFAULT_REALSENSE_ENABLED,
                        description="Enable RealSense streams.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="realsense_depth",
                        label="RS Depth Stream",
                        path="camera_router.sources.realsense.stream_depth",
                        value_type="bool",
                        default=DEFAULT_REALSENSE_STREAM_DEPTH,
                        description="Expose rs_depth stream.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="realsense_ir",
                        label="RS IR Streams",
                        path="camera_router.sources.realsense.stream_ir",
                        value_type="bool",
                        default=DEFAULT_REALSENSE_STREAM_IR,
                        description="Expose rs_ir_left and rs_ir_right streams.",
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
                        path="camera_router.tunnel.enable",
                        value_type="bool",
                        default=DEFAULT_ENABLE_TUNNEL,
                        description="Enable Cloudflare tunnel.",
                        restart_required=True,
                    ),
                    SettingSpec(
                        id="auto_install_cloudflared",
                        label="Auto-install Cloudflared",
                        path="camera_router.tunnel.auto_install_cloudflared",
                        value_type="bool",
                        default=DEFAULT_AUTO_INSTALL_CLOUDFLARED,
                        description="Install cloudflared if missing.",
                        restart_required=True,
                    ),
                ),
            ),
        ),
    )


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
        "choices": list(spec.choices or ()),
        "sensitive": bool(spec.sensitive),
        "restart_required": bool(spec.restart_required),
        "min_value": spec.min_value,
        "max_value": spec.max_value,
    }


def _camera_config_schema_payload(config_data=None):
    if not _config_spec_available():
        return {
            "status": "error",
            "message": "Configurator unavailable (terminal_ui support is not loaded).",
        }, 503

    spec = _build_camera_config_spec()
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
            parsed = raw_value
        elif isinstance(raw_value, (int, float)):
            parsed = bool(raw_value)
        elif isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                parsed = True
            elif normalized in ("0", "false", "no", "off"):
                parsed = False
            else:
                raise ValueError("Expected boolean value (true/false)")
        else:
            raise ValueError("Expected boolean value")
        return parsed

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

    if value_type == "enum":
        parsed = str(raw_value)
        choices = tuple(setting_spec.choices or ())
        if choices and parsed not in choices:
            raise ValueError(f"Expected one of: {', '.join(str(item) for item in choices)}")
        return parsed

    if value_type in ("secret", "str"):
        return str(raw_value if raw_value is not None else "")

    return str(raw_value if raw_value is not None else "")


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
        if session_key not in sessions:
            return False
        entry = sessions[session_key]
        now = time.time()
        if now - entry["last_used"] > SESSION_TIMEOUT:
            del sessions[session_key]
            return False
        entry["last_used"] = now
        return True


def cleanup_expired_sessions():
    now = time.time()
    with sessions_lock:
        expired = [k for k, v in sessions.items() if now - v["last_used"] > SESSION_TIMEOUT]
        for key in expired:
            del sessions[key]


def get_session_key_from_request():
    key = request.headers.get("X-Session-Key", "").strip()
    if key:
        return key
    key = request.args.get("session_key", "").strip()
    if key:
        return key
    if request.method in ("POST", "PUT", "PATCH"):
        data = request.get_json(silent=True) or {}
        if isinstance(data, dict):
            key = str(data.get("session_key", "")).strip()
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
        return os.path.join(SCRIPT_DIR, f"{CAMERA_CLOUDFLARED_BASENAME}.exe")
    return os.path.join(SCRIPT_DIR, CAMERA_CLOUDFLARED_BASENAME)


def is_cloudflared_installed():
    cloudflared_path = get_cloudflared_path()
    if os.path.exists(cloudflared_path):
        return True
    try:
        subprocess.run(["cloudflared", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
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
        log(f"[ERROR] Unsupported platform: {system} {machine}")
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


def start_cloudflared_tunnel(local_port):
    global tunnel_url, tunnel_process, tunnel_last_error, tunnel_desired
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
        f"http://localhost:{local_port}",
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
        global tunnel_url, tunnel_process, tunnel_last_error
        found_url = False
        captured_url = ""
        for raw_line in iter(process.stdout.readline, ""):
            line = raw_line.strip()
            if not line:
                continue
            lowered = line.lower()
            if any(token in lowered for token in ("error", "failed", "unable", "panic")):
                log(f"[CLOUDFLARED] {line}")
            if "trycloudflare.com" in line:
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if not match:
                    match = re.search(r"https://[^\s]+trycloudflare\.com[^\s]*", line)
                if match:
                    with tunnel_url_lock:
                        if tunnel_url is None:
                            captured_url = match.group(0)
                            tunnel_url = captured_url
                            found_url = True
                            tunnel_last_error = ""
                            log("")
                            log("=" * 60)
                            log(f"[TUNNEL] Camera Router URL: {tunnel_url}")
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
                    start_cloudflared_tunnel(local_port)

    threading.Thread(target=monitor_output, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Camera feed abstraction
# ---------------------------------------------------------------------------
class FrameFeed:
    def __init__(self, camera_id, label, source_type="generic", device_path=""):
        self.camera_id = camera_id
        self.label = label
        self.source_type = str(source_type or "generic")
        self.device_path = str(device_path or "")
        self.lock = Lock()
        self.cond = threading.Condition(self.lock)

        self.latest_jpeg = None
        self.latest_frame = None
        self.frame_id = 0

        self.width = 0
        self.height = 0
        self.fps = 0.0
        self.kbps = 0.0
        self.last_frame_ts = 0.0
        self.total_frames = 0
        self.client_count = 0

        self.online = False
        self.last_error = ""
        self.available_profiles = []
        self.profile_query_error = ""
        self.capture_profile = {
            "pixel_format": "",
            "width": int(source_options.get("camera_capture_width", DEFAULT_CAMERA_CAPTURE_WIDTH)),
            "height": int(source_options.get("camera_capture_height", DEFAULT_CAMERA_CAPTURE_HEIGHT)),
            "fps": int(source_options.get("camera_capture_fps", DEFAULT_CAMERA_CAPTURE_FPS)),
        }
        self.capture_revision = 0
        self.active_capture = {
            "backend": "",
            "pixel_format": "",
            "width": 0,
            "height": 0,
            "fps": 0,
        }

    def publish(self, frame, options, rotation_degrees=None):
        prepared = prepare_frame(frame, options, rotation_degrees=rotation_degrees)
        if prepared is None:
            return
        ok, encoded = cv2.imencode(
            ".jpg",
            prepared,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(options["jpeg_quality"])],
        )
        if not ok:
            return

        jpeg = encoded.tobytes()
        now = time.time()

        with self.cond:
            previous = self.last_frame_ts
            self.latest_jpeg = jpeg
            self.latest_frame = prepared
            self.frame_id += 1
            self.total_frames += 1
            self.width = int(prepared.shape[1])
            self.height = int(prepared.shape[0])
            self.last_frame_ts = now
            self.online = True
            self.last_error = ""

            if previous > 0:
                dt = now - previous
                if dt > 0:
                    inst_fps = 1.0 / dt
                    inst_kbps = (len(jpeg) * 8.0 / dt) / 1000.0
                    self.fps = inst_fps if self.fps <= 0 else 0.8 * self.fps + 0.2 * inst_fps
                    self.kbps = inst_kbps if self.kbps <= 0 else 0.8 * self.kbps + 0.2 * inst_kbps

            self.cond.notify_all()

    def mark_error(self, message):
        with self.cond:
            self.online = False
            self.last_error = message

    def set_available_profiles(self, profiles, error_message=""):
        with self.lock:
            clean = []
            seen = set()
            for profile in profiles or []:
                pix = str(profile.get("pixel_format", "")).strip().upper()
                width = _as_int(profile.get("width"), 0, minimum=1, maximum=7680)
                height = _as_int(profile.get("height"), 0, minimum=1, maximum=4320)
                fps_value = float(profile.get("fps", 0.0) or 0.0)
                if width <= 0 or height <= 0 or fps_value <= 0:
                    continue
                key = (pix, width, height, round(fps_value, 3))
                if key in seen:
                    continue
                seen.add(key)
                clean.append(
                    {
                        "pixel_format": pix,
                        "width": width,
                        "height": height,
                        "fps": round(fps_value, 3),
                    }
                )
            clean.sort(key=lambda item: (item["pixel_format"], item["width"], item["height"], item["fps"]))
            self.available_profiles = clean
            self.profile_query_error = str(error_message or "").strip()

    def get_available_profiles(self):
        with self.lock:
            return [dict(item) for item in self.available_profiles], self.profile_query_error

    def get_capture_profile(self):
        with self.lock:
            return dict(self.capture_profile), int(self.capture_revision)

    def set_capture_profile(self, profile):
        requested = profile or {}
        next_profile = {
            "pixel_format": str(requested.get("pixel_format", "")).strip().upper(),
            "width": _as_int(
                requested.get("width"),
                self.capture_profile.get("width", DEFAULT_CAMERA_CAPTURE_WIDTH),
                minimum=1,
                maximum=7680,
            ),
            "height": _as_int(
                requested.get("height"),
                self.capture_profile.get("height", DEFAULT_CAMERA_CAPTURE_HEIGHT),
                minimum=1,
                maximum=4320,
            ),
            "fps": float(
                _as_int(
                    requested.get("fps"),
                    int(round(float(self.capture_profile.get("fps", DEFAULT_CAMERA_CAPTURE_FPS)))),
                    minimum=1,
                    maximum=240,
                )
            ),
        }

        with self.lock:
            changed = next_profile != self.capture_profile
            if changed:
                self.capture_profile = next_profile
                self.capture_revision += 1
            return changed, dict(self.capture_profile), int(self.capture_revision)

    def set_active_capture(self, backend, profile):
        profile_data = profile or {}
        with self.lock:
            self.active_capture = {
                "backend": str(backend or ""),
                "pixel_format": str(profile_data.get("pixel_format", "")).strip().upper(),
                "width": _as_int(profile_data.get("width"), 0, minimum=0, maximum=7680),
                "height": _as_int(profile_data.get("height"), 0, minimum=0, maximum=4320),
                "fps": float(profile_data.get("fps", 0.0) or 0.0),
            }

    def mark_offline(self):
        with self.cond:
            self.online = False

    def snapshot(self):
        with self.lock:
            return self.latest_jpeg

    def latest_frame_copy(self):
        with self.lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def acquire_client(self):
        with self.lock:
            self.client_count += 1

    def release_client(self):
        with self.lock:
            self.client_count = max(0, self.client_count - 1)

    def status(self):
        configured_enabled_key, configured_enabled = get_feed_enable_rule(self)
        with self.lock:
            return {
                "id": self.camera_id,
                "label": self.label,
                "source_type": self.source_type,
                "device_path": self.device_path,
                "online": self.online,
                "has_frame": self.latest_jpeg is not None,
                "frame_size": {"width": self.width, "height": self.height},
                "fps": round(self.fps, 2),
                "kbps": round(self.kbps, 2),
                "clients": self.client_count,
                "total_frames": self.total_frames,
                "last_error": self.last_error,
                "capture_profile": dict(self.capture_profile),
                "active_capture": dict(self.active_capture),
                "available_profiles": [dict(item) for item in self.available_profiles],
                "profile_query_error": self.profile_query_error,
                "rotation_degrees": int(get_feed_rotation_degrees(self)),
                "enabled": bool(get_feed_enabled(self)),
                "configured_enabled": configured_enabled if configured_enabled is not None else None,
                "configured_enabled_key": configured_enabled_key or "",
            }


def prepare_frame(frame, options, rotation_degrees=None):
    if frame is None:
        return None
    out = frame
    rotation = _parse_rotation_degrees(rotation_degrees)
    if rotation is None:
        # Legacy fallback for older configs.
        if options.get("rotate_clockwise", False):
            rotation = 90
        else:
            rotation = _rotation_or_default(options.get("default_rotation_degrees"), 0)
    if rotation == 90:
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
    elif rotation == 180:
        out = cv2.rotate(out, cv2.ROTATE_180)
    elif rotation == 270:
        out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)

    h, w = out.shape[:2]
    max_w = int(options.get("max_width", 0))
    max_h = int(options.get("max_height", 0))
    scale_w = max_w / float(w) if max_w > 0 else 1.0
    scale_h = max_h / float(h) if max_h > 0 else 1.0
    scale = min(scale_w, scale_h, 1.0)
    if scale < 1.0:
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        out = cv2.resize(out, (new_w, new_h), interpolation=cv2.INTER_AREA)
    if len(out.shape) == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    return out


def register_feed(camera_id, label, source_type="generic", device_path=""):
    with camera_feeds_lock:
        feed = FrameFeed(
            camera_id,
            label,
            source_type=source_type,
            device_path=device_path,
        )
        camera_feeds[camera_id] = feed
    return feed


def get_feed(camera_id):
    with camera_feeds_lock:
        return camera_feeds.get(camera_id)


def all_feed_statuses():
    with camera_feeds_lock:
        feeds = list(camera_feeds.values())
    return [feed.status() for feed in feeds]


def stream_protocol_capabilities():
    return {
        "jpeg_snapshot": True,
        "mjpeg": True,
        "webrtc": WEBRTC_AVAILABLE,
        "mpegts": MPEGTS_AVAILABLE,
        "webrtc_error": WEBRTC_IMPORT_ERROR if not WEBRTC_AVAILABLE else "",
        "mpegts_error": "" if MPEGTS_AVAILABLE else f"{FFMPEG_BIN} not found in PATH",
    }


def camera_mode_urls(camera_id):
    return {
        "jpeg": f"/jpeg/{camera_id}",
        "mjpeg": f"/mjpeg/{camera_id}",
        "mpegts": f"/mpegts/{camera_id}",
        "webrtc_offer": f"/webrtc/offer/{camera_id}",
        "webrtc_player": f"/webrtc/player/{camera_id}",
    }


def _video_sysfs_dir(device_path):
    if os.name == "nt":
        return None
    base = os.path.basename(str(device_path))
    if not re.fullmatch(r"video\d+", base):
        return None
    return os.path.join("/sys/class/video4linux", base)


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def _video_device_label(device_path):
    sysfs_dir = _video_sysfs_dir(device_path)
    if not sysfs_dir:
        return ""
    return _read_text(os.path.join(sysfs_dir, "name"))


def _is_realsense_video_node(device_path):
    label = _video_device_label(device_path).lower()
    return "realsense" in label


def _video_device_index(device_path):
    base = os.path.basename(str(device_path))
    match = re.fullmatch(r"video(\d+)", base)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _pid_is_running(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _pid_parent(pid):
    try:
        with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("PPid:"):
                    return int(line.split(":", 1)[1].strip() or 0)
    except Exception:
        return 0
    return 0


def _protected_pids():
    protected = set()
    pid = os.getpid()
    for _ in range(24):
        if not pid or pid <= 1 or pid in protected:
            break
        protected.add(pid)
        pid = _pid_parent(pid)
    try:
        protected.add(os.getppid())
    except Exception:
        pass
    return protected


def _pid_cmdline(pid):
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fp:
            raw = fp.read().replace(b"\x00", b" ").strip()
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _pid_is_camera_router(pid):
    cmdline = _pid_cmdline(pid).lower()
    if not cmdline:
        return False
    return ("camera_route.py" in cmdline) or ("teleoperation/vision/camera_route.py" in cmdline)


def _camera_device_owner_pids(device_path):
    owners = set()
    device_path = str(device_path or "").strip()
    if not device_path or os.name == "nt":
        return []

    fuser_bin = shutil.which("fuser")
    if fuser_bin:
        try:
            result = subprocess.run([fuser_bin, device_path], capture_output=True, text=True, timeout=2)
            merged = f"{result.stdout or ''}\n{result.stderr or ''}"
            for token in re.findall(r"\b\d+\b", merged):
                owners.add(int(token))
        except Exception:
            pass

    if not owners:
        lsof_bin = shutil.which("lsof")
        if lsof_bin:
            try:
                result = subprocess.run([lsof_bin, "-t", device_path], capture_output=True, text=True, timeout=2)
                for line in (result.stdout or "").splitlines():
                    line = line.strip()
                    if line.isdigit():
                        owners.add(int(line))
            except Exception:
                pass

    protected = _protected_pids()
    return sorted(pid for pid in owners if pid not in protected)


def _wait_for_pid_exit(pids, timeout_seconds):
    deadline = time.time() + max(0.1, float(timeout_seconds))
    remaining = {int(pid) for pid in pids if pid}
    while remaining and time.time() < deadline:
        remaining = {pid for pid in remaining if _pid_is_running(pid)}
        if not remaining:
            return True
        time.sleep(0.05)
    return not remaining


def _recover_stale_camera_holders(device_path, force_all=False):
    owners = _camera_device_owner_pids(device_path)
    if not owners:
        return False

    stale_router_owners = [pid for pid in owners if _pid_is_camera_router(pid)]
    targets = stale_router_owners if stale_router_owners else (owners if force_all else [])
    if not targets:
        return False

    target_text = ", ".join(str(pid) for pid in targets)
    reason = "stale camera_route holder(s)" if stale_router_owners else "camera holder(s)"
    log(f"[WARN] Recovering busy camera {device_path}: {reason} pid={target_text}")

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    _wait_for_pid_exit(targets, DEFAULT_STALE_CAMERA_TERM_WAIT_SECONDS)
    survivors = [pid for pid in targets if _pid_is_running(pid)]
    if not survivors:
        return True

    survivor_text = ", ".join(str(pid) for pid in survivors)
    log(f"[WARN] Forcing kill of camera holder(s) on {device_path}: pid={survivor_text}")
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    _wait_for_pid_exit(survivors, DEFAULT_STALE_CAMERA_KILL_WAIT_SECONDS)
    return True


def discover_default_devices(device_glob):
    if os.name == "nt":
        return []

    candidates = sorted(glob.glob(device_glob))
    filtered = []
    skipped_realsense = 0

    for device in candidates:
        if not os.path.exists(device):
            continue

        # When RealSense is enabled through pyrealsense2, leave its V4L2 nodes to that worker.
        # Otherwise keep nodes in the list and let runtime open-probing decide.
        if source_options["realsense_enabled"] and REALSENSE_AVAILABLE and _is_realsense_video_node(device):
            skipped_realsense += 1
            continue

        filtered.append(device)

    if skipped_realsense:
        log(f"[INFO] Ignored {skipped_realsense} RealSense V4L2 node(s) for default camera workers")

    return filtered


def _normalize_pixel_format_code(value):
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    aliases = {
        "JPEG": "MJPG",
        "JPG": "MJPG",
    }
    return aliases.get(raw, raw)


def query_default_camera_profiles(device_path):
    if os.name == "nt":
        return [], "v4l2 profile discovery is only supported on Linux"

    v4l2_ctl = shutil.which("v4l2-ctl")
    if not v4l2_ctl:
        return [], "v4l2-ctl not found"

    try:
        result = subprocess.run(
            [v4l2_ctl, "-d", device_path, "--list-formats-ext"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception as exc:
        return [], f"v4l2-ctl error: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return [], detail

    current_pixfmt = ""
    current_desc = ""
    current_size = None
    profiles = []
    seen = set()

    for raw_line in (result.stdout or "").splitlines():
        line = raw_line.strip()
        fmt_match = re.match(r"^\[\d+\]:\s+'([^']+)'\s+\(([^)]*)\)", line)
        if fmt_match:
            current_pixfmt = _normalize_pixel_format_code(fmt_match.group(1))
            current_desc = fmt_match.group(2).strip()
            current_size = None
            continue

        size_match = re.match(r"^Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size_match and current_pixfmt:
            current_size = (int(size_match.group(1)), int(size_match.group(2)))
            continue

        interval_match = re.match(r"^Interval:\s+Discrete\s+[0-9.]+s\s+\(([0-9.]+)\s+fps\)", line)
        if interval_match and current_pixfmt and current_size:
            fps_value = float(interval_match.group(1))
            width, height = current_size
            key = (current_pixfmt, width, height, round(fps_value, 3))
            if key in seen:
                continue
            seen.add(key)
            profiles.append(
                {
                    "pixel_format": current_pixfmt,
                    "description": current_desc,
                    "width": width,
                    "height": height,
                    "fps": round(fps_value, 3),
                }
            )

    profiles.sort(key=lambda item: (item["pixel_format"], item["width"], item["height"], item["fps"]))
    if profiles:
        return profiles, ""

    # Fallback entry if format parsing produced no discrete combinations.
    return (
        [
            {
                "pixel_format": "",
                "description": "default",
                "width": int(source_options["camera_capture_width"]),
                "height": int(source_options["camera_capture_height"]),
                "fps": float(source_options["camera_capture_fps"]),
            }
        ],
        "No discrete format list from v4l2-ctl; using configured defaults",
    )


def select_initial_default_profile(profiles):
    if not profiles:
        return {
            "pixel_format": "",
            "width": int(source_options["camera_capture_width"]),
            "height": int(source_options["camera_capture_height"]),
            "fps": float(source_options["camera_capture_fps"]),
        }

    target_width = int(source_options["camera_capture_width"])
    target_height = int(source_options["camera_capture_height"])
    target_fps = float(source_options["camera_capture_fps"])

    def score(profile):
        return (
            abs(int(profile.get("width", 0)) - target_width)
            + abs(int(profile.get("height", 0)) - target_height)
            + abs(float(profile.get("fps", 0.0)) - target_fps) * 25.0
        )

    best = min(profiles, key=score)
    return {
        # Do not force a pixel format at startup; many Orin camera drivers expose
        # formats that are valid in v4l2-ctl but not stable through OpenCV paths.
        # Users can still choose an explicit format from available_profiles later.
        "pixel_format": "",
        "width": int(best.get("width", target_width)),
        "height": int(best.get("height", target_height)),
        "fps": float(best.get("fps", target_fps)),
    }


def find_matching_profile(available_profiles, requested_profile):
    if not available_profiles:
        return None

    req_pixfmt = _normalize_pixel_format_code(requested_profile.get("pixel_format", ""))
    req_width = _as_int(requested_profile.get("width"), 0, minimum=0, maximum=7680)
    req_height = _as_int(requested_profile.get("height"), 0, minimum=0, maximum=4320)
    req_fps = float(requested_profile.get("fps", 0.0) or 0.0)

    filtered = []
    for profile in available_profiles:
        pixfmt = _normalize_pixel_format_code(profile.get("pixel_format", ""))
        width = _as_int(profile.get("width"), 0, minimum=0, maximum=7680)
        height = _as_int(profile.get("height"), 0, minimum=0, maximum=4320)
        fps = float(profile.get("fps", 0.0) or 0.0)

        if req_pixfmt and pixfmt and pixfmt != req_pixfmt:
            continue
        if req_width and width != req_width:
            continue
        if req_height and height != req_height:
            continue
        filtered.append(profile)

    if not filtered:
        return None

    if req_fps > 0:
        return min(filtered, key=lambda profile: abs(float(profile.get("fps", 0.0) or 0.0) - req_fps))
    return filtered[0]


def _build_gstreamer_capture_pipelines(device_path, width, height, fps, pixel_format=""):
    width = max(1, int(width))
    height = max(1, int(height))
    fps = max(1, int(fps))
    pixfmt = _normalize_pixel_format_code(pixel_format)

    raw_caps = "video/x-raw"
    if pixfmt and pixfmt not in ("MJPG", "RG10", "RG12", "RG16", "BA81", "GBRG", "GRBG", "BGGR"):
        raw_caps += f",format=(string){pixfmt}"
    raw_caps += (
        f",width=(int){width},height=(int){height},framerate=(fraction){fps}/1"
    )

    jpeg_caps = (
        "image/jpeg,"
        f"width=(int){width},height=(int){height},framerate=(fraction){fps}/1"
    )

    return [
        (
            "nvv4l2camerasrc "
            f"device={device_path} ! "
            "video/x-raw(memory:NVMM),"
            f"format=(string){pixfmt if pixfmt and pixfmt != 'MJPG' else 'UYVY'},"
            f"width=(int){width},height=(int){height},framerate=(fraction){fps}/1 ! "
            "nvvidconv ! video/x-raw,format=(string)BGRx ! "
            "videoconvert ! appsink drop=1 max-buffers=1 sync=false"
        ),
        (
            "v4l2src "
            f"device={device_path} io-mode=2 ! "
            f"{raw_caps} ! "
            "videoconvert ! appsink drop=1 max-buffers=1 sync=false"
        ),
        (
            "v4l2src "
            f"device={device_path} io-mode=2 ! "
            f"{jpeg_caps} ! "
            "jpegdec ! videoconvert ! appsink drop=1 max-buffers=1 sync=false"
        ),
    ]


def _apply_requested_camera_props(cap, width, height, fps, pixel_format=""):
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
    except Exception:
        pass
    try:
        cap.set(cv2.CAP_PROP_FPS, int(max(1, round(float(fps)))))
    except Exception:
        pass

    pixfmt = _normalize_pixel_format_code(pixel_format)
    safe_fourcc = {"MJPG", "YUYV", "UYVY", "NV12"}
    if len(pixfmt) == 4 and pixfmt in safe_fourcc:
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*pixfmt))
        except Exception:
            pass


def open_default_camera(device_path, width, height, fps, pixel_format=""):
    if os.name == "nt":
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    else:
        backends = [cv2.CAP_V4L2, cv2.CAP_ANY]

    attempts = []
    cap_gstreamer = getattr(cv2, "CAP_GSTREAMER", None)
    if os.name != "nt" and cap_gstreamer is not None:
        for idx, pipeline in enumerate(
            _build_gstreamer_capture_pipelines(device_path, width, height, fps, pixel_format)
        ):
            attempts.append(("gstreamer", pipeline, cap_gstreamer, f"gstreamer:{idx}"))

    for backend in backends:
        attempts.append(("path", device_path, backend, f"path:{backend}"))

    device_index = _video_device_index(device_path)
    if device_index is not None:
        for backend in backends:
            attempts.append(("index", device_index, backend, f"index:{backend}"))

    for attempt_kind, source, backend, backend_label in attempts:
        try:
            cap = cv2.VideoCapture(source, backend)
        except Exception:
            cap = None
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            continue

        # GStreamer pipelines encode desired stream params in the pipeline string.
        if attempt_kind != "gstreamer":
            _apply_requested_camera_props(cap, width, height, fps, pixel_format)

        # Some camera stacks report opened before frames are actually available.
        # Hold a short warm-up window and only accept a capture that can read frames.
        healthy = False
        for _ in range(18):
            ok, frame = cap.read()
            if ok and frame is not None:
                healthy = True
                break
            time.sleep(0.03)
        if not healthy:
            cap.release()
            continue

        return cap, backend_label

    return None, ""


def default_camera_worker(feed, device_path):
    publish_interval = 1.0 / float(max(1, int(stream_options["target_fps"])))
    open_failures = 0
    open_retry_delay = DEFAULT_DEFAULT_CAMERA_OPEN_RETRY_INITIAL_SECONDS
    last_recovery_attempt_ts = 0.0
    disabled_reported = False

    while service_running.is_set():
        if not get_feed_enabled(feed):
            if not disabled_reported:
                feed.mark_error("Camera disabled by policy")
                feed.mark_offline()
                disabled_reported = True
            time.sleep(0.25)
            continue
        disabled_reported = False

        requested_profile, requested_revision = feed.get_capture_profile()
        cap, backend_label = open_default_camera(
            device_path,
            requested_profile["width"],
            requested_profile["height"],
            requested_profile["fps"],
            requested_profile.get("pixel_format", ""),
        )
        if cap is None:
            open_failures += 1
            feed.mark_error(f"Unable to open {device_path}")
            now = time.time()
            # On repeated open failures, clear stale/hung processes still holding /dev/video*.
            if now - last_recovery_attempt_ts >= 1.0:
                recovered = _recover_stale_camera_holders(device_path, force_all=(open_failures >= 3))
                last_recovery_attempt_ts = now
                if recovered:
                    time.sleep(0.2)
                    continue
            if open_failures in (1, 3) or open_failures % 10 == 0:
                log(
                    f"[WARN] Failed to open camera {device_path} "
                    f"({requested_profile['width']}x{requested_profile['height']} @ {requested_profile['fps']}fps "
                    f"{requested_profile.get('pixel_format') or ''}); "
                    f"retrying in {open_retry_delay:.1f}s (failure {open_failures})"
                )
            time.sleep(open_retry_delay)
            open_retry_delay = min(
                DEFAULT_DEFAULT_CAMERA_OPEN_RETRY_MAX_SECONDS,
                open_retry_delay * 1.6,
            )
            continue

        open_failures = 0
        open_retry_delay = DEFAULT_DEFAULT_CAMERA_OPEN_RETRY_INITIAL_SECONDS
        feed.set_active_capture(backend_label, requested_profile)
        _register_active_capture_handle(feed.camera_id, cap)
        log(
            f"[OK] Camera worker started: {device_path} "
            f"({requested_profile['width']}x{requested_profile['height']} @ {requested_profile['fps']}fps "
            f"{requested_profile.get('pixel_format') or 'auto'}, {backend_label})"
        )
        next_emit = 0.0
        read_failures = 0
        while service_running.is_set():
            if not get_feed_enabled(feed):
                feed.mark_error("Camera disabled by policy")
                feed.mark_offline()
                log(f"[INFO] Camera disabled; closing capture for {device_path}")
                break

            latest_profile, latest_revision = feed.get_capture_profile()
            if latest_revision != requested_revision:
                log(f"[INFO] Capture profile updated for {device_path}; restarting camera worker")
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                read_failures += 1
                if read_failures < 3:
                    time.sleep(0.05)
                    continue
                feed.mark_error(f"Read failure on {device_path}")
                log(f"[WARN] Camera read failed on {device_path}; reconnecting")
                break
            read_failures = 0
            now = time.time()
            if now < next_emit:
                continue
            next_emit = now + publish_interval
            feed.publish(frame, stream_options, rotation_degrees=get_feed_rotation_degrees(feed))

        try:
            cap.release()
        except Exception:
            pass
        _unregister_active_capture_handle(feed.camera_id, cap)
        time.sleep(DEFAULT_DEFAULT_CAMERA_DISCONNECT_RETRY_SECONDS)

    feed.mark_offline()


def _is_realsense_ir_profile_error(exc):
    text = str(exc).lower()
    return (
        "failed to resolve the request" in text
        or ("y8i" in text and "y8" in text)
        or ("infrared" in text and "format" in text)
    )


def _any_realsense_feed_enabled(rs_ids):
    for feed_id in rs_ids.values():
        if not feed_id:
            continue
        feed = get_feed(feed_id)
        if feed and get_feed_enabled(feed):
            return True
    return False


def realsense_worker(rs_ids):
    if not REALSENSE_AVAILABLE:
        return

    publish_interval = 1.0 / float(max(1, int(stream_options["target_fps"])))
    include_ir = bool(source_options["realsense_stream_ir"])
    start_retry_delay = 1.0

    while service_running.is_set():
        if not _any_realsense_feed_enabled(rs_ids):
            for feed_id in rs_ids.values():
                feed = get_feed(feed_id)
                if feed:
                    feed.mark_error("Camera disabled by policy")
                    feed.mark_offline()
            time.sleep(0.25)
            continue

        cap = RealsenseCapture(stream_ir=include_ir)
        try:
            cap.start(max_retries=DEFAULT_REALSENSE_START_ATTEMPTS)
            _register_active_capture_handle("realsense", cap)
            log(f"[OK] RealSense capture started (ir={'on' if include_ir else 'off'})")
            start_retry_delay = 1.0
        except Exception as exc:
            if include_ir and _is_realsense_ir_profile_error(exc):
                include_ir = False
                source_options["realsense_stream_ir"] = False
                log("[WARN] RealSense IR profile unsupported; continuing with color/depth only")
                for feed_id in (rs_ids.get("ir_left"), rs_ids.get("ir_right")):
                    feed = get_feed(feed_id) if feed_id else None
                    if feed:
                        feed.mark_error("IR disabled: unsupported stream profile")
                time.sleep(0.25)
                continue

            log(f"[WARN] RealSense start failed: {exc}; retrying in {start_retry_delay:.1f}s")
            for feed_id in rs_ids.values():
                feed = get_feed(feed_id)
                if feed:
                    feed.mark_error(f"RealSense start failed: {exc}")
            time.sleep(start_retry_delay)
            start_retry_delay = min(10.0, start_retry_delay * 1.8)
            continue

        next_emit = 0.0
        read_failures = 0
        while service_running.is_set():
            if not _any_realsense_feed_enabled(rs_ids):
                for feed_id in rs_ids.values():
                    feed = get_feed(feed_id)
                    if feed:
                        feed.mark_error("Camera disabled by policy")
                        feed.mark_offline()
                log("[INFO] RealSense feeds disabled; stopping current capture loop")
                break

            try:
                ok, payload = cap.read(include_ir=include_ir)
            except Exception as exc:
                log(f"[WARN] RealSense read error: {exc}")
                ok, payload = False, None
            if not ok or payload is None:
                read_failures += 1
                if read_failures >= 20:
                    for feed_id in rs_ids.values():
                        feed = get_feed(feed_id)
                        if feed:
                            feed.mark_error("RealSense frame timeout; restarting pipeline")
                    log("[WARN] RealSense capture stalled; restarting pipeline")
                    break
                time.sleep(0.05)
                continue
            read_failures = 0
            now = time.time()
            if now < next_emit:
                continue
            next_emit = now + publish_interval

            if include_ir:
                color, depth_vis, ir_left, ir_right, imu_data = payload
            else:
                color, depth_vis, imu_data = payload
                ir_left = ir_right = None
            with imu_lock:
                imu_state.clear()
                imu_state.update(imu_data or {})

            feed = get_feed(rs_ids["color"])
            if feed:
                if get_feed_enabled(feed):
                    feed.publish(color, stream_options, rotation_degrees=get_feed_rotation_degrees(feed))
                else:
                    feed.mark_offline()

            if source_options["realsense_stream_depth"]:
                feed = get_feed(rs_ids["depth"])
                if feed:
                    if get_feed_enabled(feed):
                        feed.publish(depth_vis, stream_options, rotation_degrees=get_feed_rotation_degrees(feed))
                    else:
                        feed.mark_offline()

            if include_ir and source_options["realsense_stream_ir"]:
                feed = get_feed(rs_ids["ir_left"])
                if feed:
                    if get_feed_enabled(feed):
                        feed.publish(ir_left, stream_options, rotation_degrees=get_feed_rotation_degrees(feed))
                    else:
                        feed.mark_offline()
                feed = get_feed(rs_ids["ir_right"])
                if feed:
                    if get_feed_enabled(feed):
                        feed.publish(ir_right, stream_options, rotation_degrees=get_feed_rotation_degrees(feed))
                    else:
                        feed.mark_offline()

        try:
            cap.release()
        except Exception:
            pass
        _unregister_active_capture_handle("realsense", cap)
        time.sleep(0.25)

    for feed_id in rs_ids.values():
        feed = get_feed(feed_id)
        if feed:
            feed.mark_offline()


def initialize_camera_workers():
    service_running.set()
    default_worker_started = False

    if source_options["default_cameras_enabled"]:
        devices = discover_default_devices(source_options["camera_device_glob"])
        for device in devices:
            _recover_stale_camera_holders(device, force_all=False)
        for index, device in enumerate(devices):
            cam_id = f"default_{index}"
            feed = register_feed(
                cam_id,
                f"Default Camera ({device})",
                source_type="default",
                device_path=device,
            )
            profiles, profile_error = query_default_camera_profiles(device)
            feed.set_available_profiles(profiles, profile_error)
            initial_profile = select_initial_default_profile(profiles)
            feed.set_capture_profile(initial_profile)
            if profile_error:
                log(f"[INFO] {device}: {profile_error}")
            elif profiles:
                log(f"[INFO] {device}: discovered {len(profiles)} capture profile(s)")

            thread = threading.Thread(target=default_camera_worker, args=(feed, device), daemon=True)
            thread.start()
            with capture_threads_lock:
                capture_threads.append(thread)
            default_worker_started = True

        if not devices:
            log("[WARN] Default camera mode enabled but no usable /dev/video feeds were found")

    if source_options["realsense_enabled"] and REALSENSE_AVAILABLE:
        rs_ids = {
            "color": "rs_color",
            "depth": "rs_depth",
            "ir_left": "rs_ir_left",
            "ir_right": "rs_ir_right",
        }
        register_feed(rs_ids["color"], "RealSense D455 - Color", source_type="realsense")
        if source_options["realsense_stream_depth"]:
            register_feed(rs_ids["depth"], "RealSense D455 - Depth", source_type="realsense")
        if source_options["realsense_stream_ir"]:
            register_feed(rs_ids["ir_left"], "RealSense D455 - IR Left", source_type="realsense")
            register_feed(rs_ids["ir_right"], "RealSense D455 - IR Right", source_type="realsense")
        thread = threading.Thread(target=realsense_worker, args=(rs_ids,), daemon=True)
        thread.start()
        with capture_threads_lock:
            capture_threads.append(thread)
    elif source_options["realsense_enabled"] and not REALSENSE_AVAILABLE:
        if REALSENSE_IMPORT_ERROR:
            log(f"[INFO] RealSense source disabled at runtime: {REALSENSE_IMPORT_ERROR}")
        else:
            log("[INFO] RealSense source disabled at runtime: dependencies unavailable")

    if not default_worker_started and not (source_options["realsense_enabled"] and REALSENSE_AVAILABLE):
        log("[WARN] No active camera workers started. Check /dev/video devices or RealSense availability.")


def stop_camera_workers():
    service_running.clear()
    with camera_feeds_lock:
        feeds = list(camera_feeds.values())
    for feed in feeds:
        try:
            with feed.cond:
                feed.cond.notify_all()
        except Exception:
            pass
    _release_all_active_capture_handles()

    with capture_threads_lock:
        threads = list(capture_threads)
        capture_threads.clear()
    for thread in threads:
        try:
            if thread.is_alive():
                thread.join(timeout=1.5)
        except Exception:
            pass


if WEBRTC_AVAILABLE:
    class FeedVideoStreamTrack(VideoStreamTrack):
        def __init__(self, feed, target_fps):
            super().__init__()
            self.feed = feed
            self.last_frame_id = -1
            self.min_frame_interval = 1.0 / float(max(1, int(target_fps)))
            self.last_emit = 0.0

        async def recv(self):
            while True:
                if not get_feed_enabled(self.feed):
                    await asyncio.sleep(0.05)
                    continue
                with self.feed.cond:
                    if self.feed.frame_id == self.last_frame_id:
                        self.feed.cond.wait(timeout=0.25)
                    if self.feed.frame_id == self.last_frame_id:
                        frame = None
                    else:
                        frame = self.feed.latest_frame
                        next_frame_id = self.feed.frame_id

                if frame is None:
                    await asyncio.sleep(0.01)
                    continue

                now = time.time()
                delay = self.min_frame_interval - (now - self.last_emit)
                if delay > 0:
                    await asyncio.sleep(delay)

                self.last_emit = time.time()
                self.last_frame_id = next_frame_id

                video = VideoFrame.from_ndarray(frame, format="bgr24")
                pts, time_base = await self.next_timestamp()
                video.pts = pts
                video.time_base = time_base
                return video


async def _create_webrtc_answer(offer_sdp, offer_type, feed):
    pc = RTCPeerConnection()
    with peer_connections_lock:
        peer_connections.add(pc)

    @pc.on("connectionstatechange")
    async def _on_state_change():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            try:
                await pc.close()
            except Exception:
                pass
            with peer_connections_lock:
                if pc in peer_connections:
                    peer_connections.remove(pc)

    if WEBRTC_AVAILABLE:
        track = FeedVideoStreamTrack(feed, stream_options["webrtc_target_fps"])
        pc.addTrack(track)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    }


async def _close_all_peer_connections():
    with peer_connections_lock:
        pcs = list(peer_connections)
        peer_connections.clear()
    for pc in pcs:
        try:
            await pc.close()
        except Exception:
            pass


def mpegts_stream(feed):
    if not MPEGTS_AVAILABLE:
        return None

    ffmpeg_cmd = [
        FFMPEG_BIN,
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-f",
        "mjpeg",
        "-r",
        str(max(1, int(stream_options["mpegts_target_fps"]))),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "mpeg1video",
        "-g",
        str(max(1, int(stream_options["mpegts_target_fps"]))),
        "-bf",
        "0",
        "-q:v",
        "5",
        "-f",
        "mpegts",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "pipe:1",
    ]

    process = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stop_event = threading.Event()
    feed.acquire_client()

    mpeg_options = dict(stream_options)
    mpeg_options["jpeg_quality"] = stream_options["mpegts_jpeg_quality"]

    def feeder():
        last_frame_id = -1
        try:
            while not stop_event.is_set():
                if not get_feed_enabled(feed):
                    break
                with feed.cond:
                    if feed.frame_id == last_frame_id:
                        feed.cond.wait(timeout=1.0)
                    if feed.frame_id == last_frame_id:
                        continue
                    last_frame_id = feed.frame_id
                    frame = feed.latest_frame
                if frame is None:
                    continue
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(mpeg_options["jpeg_quality"])],
                )
                if not ok:
                    continue
                try:
                    process.stdin.write(encoded.tobytes())
                    process.stdin.flush()
                except Exception:
                    break
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    feeder_thread = threading.Thread(target=feeder, daemon=True)
    feeder_thread.start()

    try:
        while True:
            if not get_feed_enabled(feed):
                break
            chunk = process.stdout.read(8192)
            if not chunk:
                break
            yield chunk
    finally:
        stop_event.set()
        try:
            feeder_thread.join(timeout=0.5)
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
        except Exception:
            pass
        feed.release_client()


# ---------------------------------------------------------------------------
# Flask app and routes
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Camera Router</title>
    <style>
      body { margin: 0; background: #111; color: #fff; font-family: monospace; }
      .wrap { max-width: 1280px; margin: 0 auto; padding: 1rem; }
      .panel { background: #1b1b1b; border: 1px solid #333; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; }
      .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
      input, button { background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 0.5rem; }
      button { cursor: pointer; }
      .tabs { display: flex; gap: 0.5rem; flex-wrap: wrap; }
      .tab-btn.active { border-color: #8aa0ff; color: #d7e1ff; }
      .tab-panel { display: none; margin-top: 0.8rem; }
      .tab-panel.active { display: block; }
      .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 1rem; }
      .card { background: #1b1b1b; border: 1px solid #333; border-radius: 10px; padding: 0.8rem; }
      .meta { opacity: 0.85; font-size: 0.85rem; margin: 0.3rem 0; }
      .ok { color: #00d08a; }
      .bad { color: #ff5c5c; }
      .rotation-controls { margin-top: 0.45rem; }
      .rotation-select { min-width: 90px; }
      .rotation-manifest { margin-top: 0.25rem; }
      .rotation-manifest code { color: #dfe8ff; }
      .enable-controls { margin-top: 0.45rem; }
      .enable-manifest { margin-top: 0.25rem; }
      .camera-enable-btn.enabled { border-color: #397f57; color: #d6ffe7; }
      .camera-enable-btn.disabled { border-color: #7f4b39; color: #ffe1d6; }
      .disabled-preview {
        margin-top: 0.45rem;
        padding: 0.85rem;
        border: 1px dashed #555;
        border-radius: 8px;
        color: #b8b8b8;
        background: #151515;
        text-align: center;
      }
      .card a, .card a:visited, .card a:hover, .card a:focus {
        color: #fff !important;
        font-weight: 700;
      }
      img { width: 100%; border-radius: 8px; background: #000; }
      code { color: #ffcc66; }
      table { width: 100%; border-collapse: collapse; }
      th, td { text-align: left; padding: 0.45rem; border-bottom: 1px solid #333; vertical-align: top; }
      th { opacity: 0.85; }
      .cfg-wrap { overflow: auto; max-height: 460px; border: 1px solid #333; border-radius: 8px; }
      .cfg-input, .cfg-select {
        width: 100%;
        background: #222;
        color: #fff;
        border: 1px solid #444;
        border-radius: 6px;
        padding: 0.35rem 0.45rem;
      }
      .cfg-check { transform: scale(1.1); }
      .cfg-row.pending { background: rgba(0, 208, 138, 0.08); }
      .cfg-source { font-size: 0.8rem; opacity: 0.85; }
      .cfg-path { font-size: 0.78rem; opacity: 0.72; }
      .badge { display: inline-block; margin-left: 0.4rem; padding: 0.1rem 0.4rem; border-radius: 999px; border: 1px solid #666; font-size: 0.7rem; }
      .config-status.ok { color: #00d08a; }
      .config-status.bad { color: #ff5c5c; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="panel">
        <h2 style="margin-top:0">Camera Router</h2>
        <div class="row">
          <label for="password">Password</label>
          <input id="password" type="password" placeholder="Enter password">
          <button id="connectBtn">Authenticate</button>
          <button id="refreshBtn">Refresh /list</button>
          <button id="rotateSessionBtn" type="button">Rotate Session Key</button>
        </div>
        <div id="statusLine" class="meta">Not authenticated.</div>
        <div class="meta">Tip: append <code>?session_key=...</code> to <code>/video/&lt;camera_id&gt;</code> for OpenCV clients.</div>
      </div>
      <div class="panel">
        <div class="tabs">
          <button id="tabHealthBtn" class="tab-btn active" type="button">Health</button>
          <button id="tabConfigBtn" class="tab-btn" type="button">Configurator</button>
        </div>
        <div id="healthPanel" class="tab-panel active">
          <h3 style="margin-top:0">/health</h3>
          <pre id="healthOut" class="meta">loading...</pre>
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
      <div id="cards" class="cards"></div>
    </div>
    <script>
      let sessionKey = localStorage.getItem("camera_router_session_key") || "";
      let listRefreshInFlight = false;
      const latestCards = {
        cameras: [],
      };
      const rotationManifestByCamera = {};
      const rotationManifestFetchInFlight = {};
      const rotationUpdateInFlight = {};
      const cameraEnableUpdateInFlight = {};
      const ROTATION_VALUES = [0, 90, 180, 270];
      const configState = {
        schema: null,
        selectedCategoryId: "",
        pending: {},
      };

      function withSession(path) {
        if (!sessionKey) return path;
        const sep = path.includes("?") ? "&" : "?";
        return `${path}${sep}session_key=${encodeURIComponent(sessionKey)}`;
      }

      function esc(value) {
        var raw = value;
        if (raw === null || raw === undefined) {
          raw = "";
        }
        return String(raw).replace(/[&<>"']/g, function (m) {
          if (m === "&") return "&amp;";
          if (m === "<") return "&lt;";
          if (m === ">") return "&gt;";
          if (m === '"') return "&quot;";
          return "&#39;";
        });
      }

      function normalizeScalar(value) {
        if (value === null || value === undefined) return "";
        if (typeof value === "object") {
          try { return JSON.stringify(value); } catch (_) { return String(value); }
        }
        return String(value);
      }

      function asBool(value) {
        if (typeof value === "boolean") return value;
        var raw = value;
        if (raw === null || raw === undefined) {
          raw = "";
        }
        const text = String(raw).trim().toLowerCase();
        return ["1", "true", "yes", "on"].includes(text);
      }

      function showTab(tabName) {
        const healthBtn = document.getElementById("tabHealthBtn");
        const configBtn = document.getElementById("tabConfigBtn");
        const healthPanel = document.getElementById("healthPanel");
        const configPanel = document.getElementById("configPanel");
        const healthActive = tabName === "health";
        healthBtn.classList.toggle("active", healthActive);
        configBtn.classList.toggle("active", !healthActive);
        healthPanel.classList.toggle("active", healthActive);
        configPanel.classList.toggle("active", !healthActive);
      }

      function setConfigStatus(message, isError = false) {
        const node = document.getElementById("configStatus");
        node.textContent = message;
        node.classList.toggle("ok", !isError);
        node.classList.toggle("bad", !!isError);
      }

      function getSpecByPath(path) {
        if (!configState.schema || !Array.isArray(configState.schema.categories)) return null;
        for (const category of configState.schema.categories) {
          if (!Array.isArray(category.settings)) continue;
          for (const setting of category.settings) {
            if (setting.path === path) return setting;
          }
        }
        return null;
      }

      function getBaseValue(path) {
        const spec = getSpecByPath(path);
        if (!spec) return "";
        return spec.current_value;
      }

      function comparableValue(value, spec) {
        if (!spec) return normalizeScalar(value);
        if (spec.value_type === "bool") {
          return asBool(value) ? "true" : "false";
        }
        return normalizeScalar(value);
      }

      function setPendingValue(path, value) {
        const spec = getSpecByPath(path);
        if (!spec) return;
        const base = getBaseValue(path);
        if (comparableValue(value, spec) === comparableValue(base, spec)) {
          delete configState.pending[path];
        } else {
          configState.pending[path] = value;
        }
      }

      function renderConfigCategoryTabs() {
        const root = document.getElementById("configCategoryTabs");
        if (!configState.schema || !Array.isArray(configState.schema.categories) || !configState.schema.categories.length) {
          root.innerHTML = "";
          return;
        }
        root.innerHTML = configState.schema.categories.map((category) => {
          const active = category.id === configState.selectedCategoryId;
          return `<button type="button" class="tab-btn ${active ? "active" : ""}" data-category-id="${esc(category.id)}">${esc(category.label)}</button>`;
        }).join("");
      }

      function renderConfigRows() {
        const root = document.getElementById("configRows");
        if (!configState.schema || !Array.isArray(configState.schema.categories) || !configState.schema.categories.length) {
          root.innerHTML = "<tr><td colspan='5' class='meta'>No configurator schema available.</td></tr>";
          return;
        }
        const category = configState.schema.categories.find((item) => item.id === configState.selectedCategoryId) || configState.schema.categories[0];
        if (!category || !Array.isArray(category.settings) || !category.settings.length) {
          root.innerHTML = "<tr><td colspan='5' class='meta'>No settings in this category.</td></tr>";
          return;
        }

        root.innerHTML = category.settings.map((setting) => {
          const path = String(setting.path || "");
          const pending = Object.prototype.hasOwnProperty.call(configState.pending, path);
          const currentValue = pending ? configState.pending[path] : setting.current_value;
          const source = pending ? "pending" : (setting.current_source || "default");
          const valueType = String(setting.value_type || "str");
          let valueEditor = "";

          if (valueType === "bool") {
            valueEditor = `<input class="cfg-check" type="checkbox" data-config-path="${esc(path)}" ${asBool(currentValue) ? "checked" : ""}>`;
          } else if (valueType === "enum") {
            const currentText = normalizeScalar(currentValue);
            const options = (setting.choices || []).map((choice) => {
              const c = normalizeScalar(choice);
              return `<option value="${esc(c)}" ${c === currentText ? "selected" : ""}>${esc(c)}</option>`;
            }).join("");
            valueEditor = `<select class="cfg-select" data-config-path="${esc(path)}">${options}</select>`;
          } else {
            const inputType = valueType === "secret" ? "password" : (valueType === "int" || valueType === "float" ? "number" : "text");
            const step = valueType === "float" ? "any" : (valueType === "int" ? "1" : "");
            const minAttr = setting.min_value !== null && setting.min_value !== undefined ? ` min="${esc(setting.min_value)}"` : "";
            const maxAttr = setting.max_value !== null && setting.max_value !== undefined ? ` max="${esc(setting.max_value)}"` : "";
            const stepAttr = step ? ` step="${step}"` : "";
            valueEditor = `<input class="cfg-input" type="${inputType}" data-config-path="${esc(path)}" value="${esc(normalizeScalar(currentValue))}"${minAttr}${maxAttr}${stepAttr}>`;
          }

          const restartBadge = setting.restart_required ? "<span class='badge'>restart</span>" : "";
          const rowClass = pending ? "cfg-row pending" : "cfg-row";
          return `
            <tr class="${rowClass}">
              <td>
                <div><strong>${esc(setting.label || setting.id || path)}</strong>${restartBadge}</div>
                <div class="cfg-path"><code>${esc(path)}</code></div>
              </td>
              <td>${valueEditor}</td>
              <td class="cfg-source">${esc(valueType)}</td>
              <td class="cfg-source">${esc(source)}</td>
              <td>${esc(setting.description || "")}</td>
            </tr>
          `;
        }).join("");
      }

      function renderConfigPanel() {
        renderConfigCategoryTabs();
        renderConfigRows();
        const pendingCount = Object.keys(configState.pending).length;
        if (pendingCount > 0) {
          setConfigStatus(`${pendingCount} pending change(s)`, false);
        }
      }

      async function loadConfigSchema() {
        try {
          const res = await fetch(withSession("/config/schema"), { cache: "no-store" });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            if (res.status === 401) {
              setConfigStatus("Authenticate to access configurator settings.", true);
              return;
            }
            setConfigStatus(`Config schema error: ${data.message || res.status}`, true);
            return;
          }
          configState.schema = data.config || { categories: [] };
          configState.pending = {};
          const categories = configState.schema.categories || [];
          configState.selectedCategoryId = categories.length ? String(categories[0].id) : "";
          renderConfigPanel();
          setConfigStatus("Configurator loaded.", false);
        } catch (err) {
          setConfigStatus(`Config schema request failed: ${err}`, true);
        }
      }

      async function saveConfigChanges() {
        const pendingPaths = Object.keys(configState.pending);
        if (!pendingPaths.length) {
          setConfigStatus("No pending changes to save.", false);
          return;
        }
        try {
          const res = await fetch(withSession("/config/save"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({changes: configState.pending}),
          });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            const errorText = data.errors ? JSON.stringify(data.errors) : (data.message || res.status);
            setConfigStatus(`Save failed: ${errorText}`, true);
            return;
          }
          await loadConfigSchema();
          const restartNote = data.restart_required ? " Restart required for one or more changes." : "";
          setConfigStatus(`Saved ${data.saved_paths ? data.saved_paths.length : pendingPaths.length} change(s).${restartNote}`, false);
        } catch (err) {
          setConfigStatus(`Save request failed: ${err}`, true);
        }
      }

      function discardConfigChanges() {
        configState.pending = {};
        renderConfigPanel();
        setConfigStatus("Pending changes discarded.", false);
      }

      async function authenticate() {
        const password = document.getElementById("password").value.trim();
        if (!password) {
          alert("Enter password first.");
          return;
        }
        const res = await fetch("/auth", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({password})
        });
        const data = await res.json();
        if (!res.ok || data.status !== "success") {
          document.getElementById("statusLine").textContent = `Auth failed: ${data.message || res.status}`;
          return;
        }
        sessionKey = data.session_key;
        localStorage.setItem("camera_router_session_key", sessionKey);
        document.getElementById("statusLine").textContent = `Authenticated. Timeout ${data.timeout}s`;
        await Promise.all([refreshList(), loadConfigSchema()]);
      }

      async function rotateSessionKey() {
        if (!sessionKey) {
          document.getElementById("statusLine").textContent = "Authenticate first to rotate the session key";
          return;
        }
        const button = document.getElementById("rotateSessionBtn");
        if (button) button.disabled = true;
        document.getElementById("statusLine").textContent = "Rotating session key...";
        try {
          const res = await fetch(withSession("/session/rotate"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
          });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            document.getElementById("statusLine").textContent = `Session rotation failed: ${data.message || res.status}`;
            if (res.status === 401) {
              sessionKey = "";
              localStorage.removeItem("camera_router_session_key");
              setConfigStatus("Session expired; authenticate to edit config.", true);
            }
            return;
          }
          sessionKey = String(data.session_key || "").trim();
          if (!sessionKey) {
            localStorage.removeItem("camera_router_session_key");
            document.getElementById("statusLine").textContent = "Session rotation failed: missing session key in response";
            return;
          }
          localStorage.setItem("camera_router_session_key", sessionKey);
          document.getElementById("statusLine").textContent =
            `Session key rotated. Invalidated ${Number(data.invalidated_sessions) || 0} session(s).`;
          await refreshList();
        } catch (err) {
          document.getElementById("statusLine").textContent = `Session rotation error: ${err}`;
        } finally {
          if (button) button.disabled = false;
        }
      }

      async function refreshHealth() {
        try {
          const res = await fetch("/health");
          const data = await res.json();
          document.getElementById("healthOut").textContent = JSON.stringify(data, null, 2);
        } catch (err) {
          document.getElementById("healthOut").textContent = String(err);
        }
      }

      function normalizeRotationDegrees(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return null;
        const rounded = Math.round(n);
        return ROTATION_VALUES.includes(rounded) ? rounded : null;
      }

      function normalizeEnabled(value, defaultValue = true) {
        if (value === null || value === undefined) return !!defaultValue;
        if (typeof value === "boolean") return value;
        return asBool(value);
      }

      function readRotationManifest(cameraId) {
        const key = String(cameraId || "").trim();
        if (!key) return null;
        return rotationManifestByCamera[key] || null;
      }

      function upsertRotationManifest(cameraId, updates) {
        const key = String(cameraId || "").trim();
        if (!key) return null;
        const existing = rotationManifestByCamera[key] || {};
        const merged = Object.assign({}, existing, updates || {});
        rotationManifestByCamera[key] = merged;
        return merged;
      }

      function selectedRotationForCard(cam, manifest) {
        const selected = normalizeRotationDegrees(manifest && manifest.selected_rotation_degrees);
        if (selected !== null) return selected;
        const configured = normalizeRotationDegrees(manifest && manifest.configured_rotation_degrees);
        if (configured !== null) return configured;
        const effectiveFromManifest = normalizeRotationDegrees(manifest && manifest.effective_rotation_degrees);
        if (effectiveFromManifest !== null) return effectiveFromManifest;
        const effectiveFromCam = normalizeRotationDegrees(cam && cam.rotation_degrees);
        if (effectiveFromCam !== null) return effectiveFromCam;
        return 0;
      }

      function buildRotationManifestText(cam, manifest) {
        const effective = normalizeRotationDegrees(
          manifest && manifest.effective_rotation_degrees !== undefined
            ? manifest.effective_rotation_degrees
            : (cam ? cam.rotation_degrees : null)
        );
        const configured = normalizeRotationDegrees(manifest && manifest.configured_rotation_degrees);
        const defaultRotation = normalizeRotationDegrees(manifest && manifest.default_rotation_degrees);
        const keyText = manifest && manifest.configured_rotation_key ? String(manifest.configured_rotation_key) : "default";
        const sourceText = manifest && manifest.source ? String(manifest.source) : "list";
        const updatedText = manifest && manifest.updated_at ? String(manifest.updated_at) : "";
        const effectiveText = effective === null ? "n/a" : `${effective}deg`;
        const configuredText = configured === null ? "default" : `${configured}deg`;
        const defaultText = defaultRotation === null ? "n/a" : `${defaultRotation}deg`;
        const updatedSuffix = updatedText ? ` | updated ${updatedText}` : "";
        return `rotation_manifest: effective=${effectiveText} | configured=${configuredText} | default=${defaultText} | key=${keyText} | source=${sourceText}${updatedSuffix}`;
      }

      async function fetchRotationManifest(cameraId, forceReload = false) {
        const key = String(cameraId || "").trim();
        if (!key || !sessionKey) return null;
        const existing = readRotationManifest(key);
        if (!forceReload && existing && existing.detail_loaded) return existing;
        if (rotationManifestFetchInFlight[key]) return null;
        rotationManifestFetchInFlight[key] = true;
        try {
          const res = await fetch(withSession(`/stream_options/${encodeURIComponent(key)}`), { cache: "no-store" });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            if (res.status === 401) {
              sessionKey = "";
              localStorage.removeItem("camera_router_session_key");
              setConfigStatus("Session expired; authenticate to edit config.", true);
            }
            return null;
          }
          const updated = upsertRotationManifest(key, {
            configured_rotation_degrees: normalizeRotationDegrees(data.configured_rotation_degrees),
            configured_rotation_key: data.configured_rotation_key || "",
            effective_rotation_degrees: normalizeRotationDegrees(data.effective_rotation_degrees),
            default_rotation_degrees: normalizeRotationDegrees(data.default_rotation_degrees),
            detail_loaded: true,
            source: "stream_options",
            updated_at: new Date().toLocaleTimeString(),
          });
          if (latestCards.cameras.length) {
            renderCards(latestCards.cameras);
          }
          return updated;
        } catch (err) {
          return null;
        } finally {
          delete rotationManifestFetchInFlight[key];
        }
      }

      function ensureRotationManifestForCameras(cameras) {
        if (!sessionKey || !Array.isArray(cameras)) return;
        cameras.forEach((cam) => {
          const cameraId = String((cam && cam.id) || "").trim();
          if (!cameraId) return;
          const manifest = readRotationManifest(cameraId);
          if (manifest && manifest.detail_loaded) return;
          fetchRotationManifest(cameraId, false);
        });
      }

      function renderCards(cameras) {
        const root = document.getElementById("cards");
        root.innerHTML = "";
        cameras.forEach((cam) => {
          const cameraId = String(cam.id || "");
          const enabled = normalizeEnabled(cam.enabled, true);
          const configuredEnabled = cam.configured_enabled;
          const configuredEnabledText = configuredEnabled === null || configuredEnabled === undefined
            ? "default"
            : (normalizeEnabled(configuredEnabled, true) ? "enabled" : "disabled");
          const configuredEnabledKey = cam.configured_enabled_key ? String(cam.configured_enabled_key) : "default";
          const stateText = enabled ? (cam.online ? "online" : "offline") : "disabled";
          const enableBusy = !!cameraEnableUpdateInFlight[cameraId];
          const enableButtonLabel = enabled ? "Disable" : "Enable";
          const enableButtonClass = enabled ? "disabled" : "enabled";
          const nextEnabled = enabled ? "false" : "true";
          const streamUrl = withSession(cam.video_url);
          const snapUrl = withSession(cam.snapshot_url);
          const manifest = readRotationManifest(cameraId);
          const selectedRotation = selectedRotationForCard(cam, manifest);
          const rotationOptions = ROTATION_VALUES.map((value) => {
            const selected = value === selectedRotation ? "selected" : "";
            return `<option value="${value}" ${selected}>${value}deg</option>`;
          }).join("");
          const rotationManifestText = buildRotationManifestText(cam, manifest);
          const rotationBusy = !!rotationUpdateInFlight[cameraId];
          const previewBlock = enabled
            ? `<img src="${streamUrl}" alt="${esc(cam.label)}">`
            : `<div class="disabled-preview">Stream disabled by policy. Enable this camera to resume preview.</div>`;
          const card = document.createElement("div");
          card.className = "card";
          card.innerHTML = `
            <h3 style="margin-top:0">${esc(cam.label)}</h3>
            <div class="meta ${enabled ? (cam.online ? "ok" : "bad") : "bad"}">status: ${esc(stateText)}</div>
            <div class="meta">id: ${esc(cameraId)}</div>
            <div class="meta">fps: ${cam.fps} | kbps: ${cam.kbps} | clients: ${cam.clients}</div>
            <div class="row enable-controls">
              <label style="min-width:64px;">camera</label>
              <button
                type="button"
                class="camera-enable-btn ${enableButtonClass}"
                data-camera-id="${esc(cameraId)}"
                data-enabled-target="${nextEnabled}"
                ${enableBusy ? "disabled" : ""}
              >${enableButtonLabel}</button>
              <span class="meta">configured: ${esc(configuredEnabledText)}</span>
            </div>
            <div class="meta enable-manifest"><code>enable_manifest: effective=${enabled ? "enabled" : "disabled"} | configured=${esc(configuredEnabledText)} | key=${esc(configuredEnabledKey)}</code></div>
            <div class="row rotation-controls">
              <label style="min-width:64px;">rotate</label>
              <select class="rotation-select" data-camera-id="${esc(cameraId)}">${rotationOptions}</select>
              <button type="button" class="rotation-apply-btn" data-camera-id="${esc(cameraId)}" ${rotationBusy ? "disabled" : ""}>Apply</button>
              <button type="button" class="rotation-clear-btn" data-camera-id="${esc(cameraId)}" ${rotationBusy ? "disabled" : ""}>Default</button>
            </div>
            <div class="meta rotation-manifest"><code>${esc(rotationManifestText)}</code></div>
            ${previewBlock}
            <div class="meta card-links"><a href="${snapUrl}" target="_blank">snapshot</a> | <a href="${streamUrl}" target="_blank">stream</a></div>
          `;
          root.appendChild(card);
        });
      }

      async function applyRotationUpdate(cameraId, nextRotation, clearRule = false) {
        const key = String(cameraId || "").trim();
        if (!key) return;
        if (!sessionKey) {
          document.getElementById("statusLine").textContent = "Authenticate first to change rotation";
          return;
        }
        if (rotationUpdateInFlight[key]) return;
        rotationUpdateInFlight[key] = true;
        const statusNode = document.getElementById("statusLine");
        statusNode.textContent = clearRule
          ? `Clearing rotation override for ${key}...`
          : `Applying ${nextRotation}deg rotation for ${key}...`;
        try {
          const payload = clearRule ? { rotation_degrees: null } : { rotation_degrees: nextRotation };
          const res = await fetch(withSession(`/stream_options/${encodeURIComponent(key)}`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            statusNode.textContent = `Rotation update failed for ${key}: ${data.message || res.status}`;
            if (res.status === 401) {
              sessionKey = "";
              localStorage.removeItem("camera_router_session_key");
              setConfigStatus("Session expired; authenticate to edit config.", true);
            }
            return;
          }
          const effectiveRotation = normalizeRotationDegrees(data.effective_rotation_degrees);
          upsertRotationManifest(key, {
            configured_rotation_degrees: normalizeRotationDegrees(data.configured_rotation_degrees),
            configured_rotation_key: data.configured_rotation_key || "",
            effective_rotation_degrees: effectiveRotation,
            default_rotation_degrees: normalizeRotationDegrees(data.default_rotation_degrees),
            detail_loaded: true,
            source: "stream_options",
            selected_rotation_degrees: effectiveRotation,
            updated_at: new Date().toLocaleTimeString(),
          });
          statusNode.textContent = `Rotation updated for ${key}: effective ${effectiveRotation === null ? "n/a" : `${effectiveRotation}deg`}`;
          await refreshList();
        } catch (err) {
          statusNode.textContent = `Rotation update error for ${key}: ${err}`;
        } finally {
          rotationUpdateInFlight[key] = false;
        }
      }

      async function updateCameraEnabled(cameraId, enabledValue) {
        const key = String(cameraId || "").trim();
        if (!key) return;
        if (!sessionKey) {
          document.getElementById("statusLine").textContent = "Authenticate first to change camera state";
          return;
        }
        if (cameraEnableUpdateInFlight[key]) return;
        cameraEnableUpdateInFlight[key] = true;
        const statusNode = document.getElementById("statusLine");
        const targetEnabled = normalizeEnabled(enabledValue, true);
        statusNode.textContent = `${targetEnabled ? "Enabling" : "Disabling"} ${key}...`;
        try {
          const res = await fetch(withSession(`/camera_state/${encodeURIComponent(key)}`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: targetEnabled }),
          });
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            statusNode.textContent = `Camera state update failed for ${key}: ${data.message || res.status}`;
            if (res.status === 401) {
              sessionKey = "";
              localStorage.removeItem("camera_router_session_key");
              setConfigStatus("Session expired; authenticate to edit config.", true);
            }
            return;
          }
          statusNode.textContent = `Camera ${key} ${data.enabled ? "enabled" : "disabled"}`;
          await refreshList();
        } catch (err) {
          statusNode.textContent = `Camera state update error for ${key}: ${err}`;
        } finally {
          cameraEnableUpdateInFlight[key] = false;
        }
      }

      async function refreshList() {
        if (listRefreshInFlight) return;
        listRefreshInFlight = true;
        try {
          const res = await fetch(withSession("/list"));
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            document.getElementById("statusLine").textContent = `List failed: ${data.message || res.status}`;
            if (res.status === 401) {
              sessionKey = "";
              localStorage.removeItem("camera_router_session_key");
              setConfigStatus("Session expired; authenticate to edit config.", true);
            }
            return;
          }
          latestCards.cameras = Array.isArray(data.cameras) ? data.cameras : [];
          latestCards.cameras.forEach((cam) => {
            const cameraId = String((cam && cam.id) || "").trim();
            if (!cameraId) return;
            const effectiveFromList = normalizeRotationDegrees(cam.rotation_degrees);
            const existing = readRotationManifest(cameraId);
            upsertRotationManifest(cameraId, {
              effective_rotation_degrees: effectiveFromList,
              source: existing && existing.source ? existing.source : "list",
            });
          });
          const enabledCount = latestCards.cameras.filter((cam) => normalizeEnabled(cam && cam.enabled, true)).length;
          document.getElementById("statusLine").textContent = `Loaded ${latestCards.cameras.length} feeds (${enabledCount} enabled)`;
          renderCards(latestCards.cameras);
          ensureRotationManifestForCameras(latestCards.cameras);
        } catch (err) {
          document.getElementById("statusLine").textContent = `List error: ${err}`;
        } finally {
          listRefreshInFlight = false;
        }
      }

      function pollListIfAuthenticated() {
        if (!sessionKey) return;
        refreshList();
      }

      document.getElementById("tabHealthBtn").addEventListener("click", () => showTab("health"));
      document.getElementById("tabConfigBtn").addEventListener("click", () => showTab("config"));
      document.getElementById("configReloadBtn").addEventListener("click", loadConfigSchema);
      document.getElementById("configSaveBtn").addEventListener("click", saveConfigChanges);
      document.getElementById("configDiscardBtn").addEventListener("click", discardConfigChanges);
      document.getElementById("configCategoryTabs").addEventListener("click", (event) => {
        const button = event.target.closest("button[data-category-id]");
        if (!button) return;
        configState.selectedCategoryId = String(button.dataset.categoryId || "");
        renderConfigPanel();
      });
      document.getElementById("configRows").addEventListener("change", (event) => {
        const target = event.target;
        if (!target || !target.dataset || !target.dataset.configPath) return;
        const path = String(target.dataset.configPath);
        const spec = getSpecByPath(path);
        if (!spec) return;
        const value = spec.value_type === "bool" ? !!target.checked : target.value;
        setPendingValue(path, value);
        renderConfigPanel();
      });
      document.getElementById("connectBtn").addEventListener("click", authenticate);
      document.getElementById("refreshBtn").addEventListener("click", refreshList);
      document.getElementById("rotateSessionBtn").addEventListener("click", rotateSessionKey);
      document.getElementById("cards").addEventListener("change", (event) => {
        const selectNode = event.target && event.target.closest ? event.target.closest("select.rotation-select") : null;
        if (!selectNode) return;
        const cameraId = String(selectNode.dataset.cameraId || "").trim();
        if (!cameraId) return;
        const selectedRotation = normalizeRotationDegrees(selectNode.value);
        if (selectedRotation === null) return;
        upsertRotationManifest(cameraId, { selected_rotation_degrees: selectedRotation });
      });
      document.getElementById("cards").addEventListener("click", (event) => {
        const enableBtn = event.target && event.target.closest ? event.target.closest("button.camera-enable-btn") : null;
        if (enableBtn) {
          const cameraId = String(enableBtn.dataset.cameraId || "").trim();
          const targetRaw = String(enableBtn.dataset.enabledTarget || "").trim().toLowerCase();
          const targetEnabled = ["1", "true", "yes", "on"].includes(targetRaw);
          if (!cameraId) return;
          updateCameraEnabled(cameraId, targetEnabled);
          return;
        }
        const applyBtn = event.target && event.target.closest ? event.target.closest("button.rotation-apply-btn") : null;
        if (applyBtn) {
          const cameraId = String(applyBtn.dataset.cameraId || "").trim();
          const controls = applyBtn.closest(".rotation-controls");
          const selectNode = controls ? controls.querySelector("select.rotation-select") : null;
          const selectedRotation = normalizeRotationDegrees(selectNode ? selectNode.value : null);
          if (!cameraId || selectedRotation === null) {
            document.getElementById("statusLine").textContent = "Select a valid rotation before applying";
            return;
          }
          applyRotationUpdate(cameraId, selectedRotation, false);
          return;
        }
        const clearBtn = event.target && event.target.closest ? event.target.closest("button.rotation-clear-btn") : null;
        if (!clearBtn) return;
        const cameraId = String(clearBtn.dataset.cameraId || "").trim();
        if (!cameraId) return;
        applyRotationUpdate(cameraId, null, true);
      });
      showTab("health");
      refreshHealth();
      setInterval(refreshHealth, 3000);
      refreshList();
      setInterval(pollListIfAuthenticated, 3000);
      loadConfigSchema();
    </script>
  </body>
</html>
"""


@app.before_request
def _count_requests():
    request_count["value"] += 1


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json(silent=True) or {}
    provided = str(data.get("password", ""))
    if provided == runtime_security["password"]:
        session_key = create_session()
        log(f"New camera session created: {session_key[:8]}...")
        return jsonify({"status": "success", "session_key": session_key, "timeout": SESSION_TIMEOUT})
    log("Authentication failed: invalid password")
    return jsonify({"status": "error", "message": "Invalid password"}), 401


@app.route("/session/rotate", methods=["POST"])
@require_session
def rotate_session():
    next_session_key, invalidated = rotate_sessions()
    log(f"Camera session keys rotated: invalidated={invalidated}")
    return jsonify(
        {
            "status": "success",
            "message": "Session keys rotated",
            "session_key": next_session_key,
            "timeout": SESSION_TIMEOUT,
            "invalidated_sessions": int(invalidated),
        }
    )


@app.route("/config/schema", methods=["GET"])
@require_session
def config_schema():
    payload, status_code = _camera_config_schema_payload(load_config())
    return jsonify(payload), status_code


@app.route("/config/save", methods=["POST"])
@require_session
def config_save():
    if not _config_spec_available():
        return jsonify({"status": "error", "message": "Configurator unavailable"}), 503

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Payload must be an object"}), 400

    changes = payload.get("changes", payload)
    if not isinstance(changes, dict) or not changes:
        return jsonify({"status": "error", "message": "No config changes provided"}), 400

    spec = _build_camera_config_spec()
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

    # Normalize and promote all config values using the same loader as runtime bootstrap.
    _load_camera_settings(config_data)
    save_config(config_data)
    apply_runtime_security(config_data)

    return jsonify(
        {
            "status": "success",
            "message": "Config saved",
            "saved_paths": sorted(coerced_changes.keys()),
            "restart_required": restart_required,
        }
    )


@app.route("/health", methods=["GET"])
def health():
    statuses = all_feed_statuses()
    online_count = sum(1 for s in statuses if s["online"])
    enabled_count = sum(1 for s in statuses if s.get("enabled", True))
    clients = sum(s["clients"] for s in statuses)
    with sessions_lock:
        sessions_active = len(sessions)
    tunnel_running = tunnel_process is not None and tunnel_process.poll() is None
    with tunnel_url_lock:
        current_tunnel = tunnel_url if tunnel_running else None
    return jsonify(
        {
            "status": "ok",
            "service": "camera_router",
            "uptime_seconds": round(time.time() - startup_time, 2),
            "require_auth": runtime_security["require_auth"],
            "protocols": stream_protocol_capabilities(),
            "tunnel_running": tunnel_running,
            "tunnel_error": tunnel_last_error,
            "feeds_total": len(statuses),
            "feeds_online": online_count,
            "feeds_enabled": enabled_count,
            "clients": clients,
            "sessions_active": sessions_active,
            "requests_served": request_count["value"],
            "realsense_available": REALSENSE_AVAILABLE,
            "realsense_import_error": REALSENSE_IMPORT_ERROR,
            "realsense_enabled": bool(source_options["realsense_enabled"]),
            "default_cameras_enabled": bool(source_options["default_cameras_enabled"]),
            "safe_mode": SAFE_MODE_ACTIVE,
            "tunnel_url": current_tunnel,
        }
    )


@app.route("/list", methods=["GET"])
@require_session
def list_cameras():
    cameras = []
    capabilities = stream_protocol_capabilities()
    for item in all_feed_statuses():
        camera_id = item["id"]
        cameras.append(
            {
                **item,
                "snapshot_url": f"/camera/{camera_id}",
                "video_url": f"/video/{camera_id}",
                "modes": camera_mode_urls(camera_id),
                "protocols": capabilities,
            }
        )
    tunnel_running = tunnel_process is not None and tunnel_process.poll() is None
    with tunnel_url_lock:
        current_tunnel = tunnel_url if tunnel_running else None
    return jsonify(
        {
            "status": "success",
            "cameras": cameras,
            "protocols": capabilities,
            "stream_format": "multipart/x-mixed-replace; boundary=frame",
            "session_timeout": SESSION_TIMEOUT,
            "tunnel_url": current_tunnel,
            "routes": {
                "auth": "/auth",
                "session_rotate": "/session/rotate",
                "health": "/health",
                "list": "/list",
                "config_schema": "/config/schema",
                "config_save": "/config/save",
                "imu": "/imu",
                "imu_stream": "/imu/stream",
                "snapshot": "/camera/<camera_id>",
                "jpeg": "/jpeg/<camera_id>",
                "stream": "/video/<camera_id>",
                "mjpeg": "/mjpeg/<camera_id>",
                "mpegts": "/mpegts/<camera_id>",
                "webrtc_offer": "/webrtc/offer/<camera_id>",
                "webrtc_player": "/webrtc/player/<camera_id>",
                "stream_options": "/stream_options/<camera_id>",
                "camera_state": "/camera_state/<camera_id>",
                "router_info": "/router_info",
            },
        }
    )


@app.route("/camera_state/<camera_id>", methods=["GET", "POST"])
@require_session
def camera_state_for_camera(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return jsonify({"status": "error", "message": "Camera not found"}), 404

    rule_keys = _rotation_rule_keys_for_feed(feed)
    configured_enabled_key, configured_enabled = get_feed_enable_rule(feed)
    effective_enabled = bool(get_feed_enabled(feed))

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "message": "Payload must be an object"}), 400

        enabled_key_present = False
        enabled_input = None
        for candidate in ("enabled", "camera_enabled", "allow"):
            if candidate in payload:
                enabled_key_present = True
                enabled_input = payload.get(candidate)
                break
        if not enabled_key_present and isinstance(payload.get("camera"), dict):
            camera_payload = payload.get("camera")
            for candidate in ("enabled", "camera_enabled", "allow"):
                if candidate in camera_payload:
                    enabled_key_present = True
                    enabled_input = camera_payload.get(candidate)
                    break
        if not enabled_key_present:
            return jsonify(
                {
                    "status": "error",
                    "message": "No state change requested. Provide enabled=true/false (or null to clear override).",
                    "camera_id": camera_id,
                }
            ), 400

        changed = False
        if enabled_input is None:
            changed_keys = [clear_camera_enable_rule(key, persist=False) for key in rule_keys]
            changed = any(changed_keys)
            if changed:
                _persist_camera_enable_rules()
            configured_enabled_key = None
            configured_enabled = None
        else:
            normalized_enabled = _as_bool(enabled_input, default=None)
            if normalized_enabled is None:
                return jsonify(
                    {
                        "status": "error",
                        "message": "enabled must be true or false",
                        "camera_id": camera_id,
                    }
                ), 400
            normalized_enabled = bool(normalized_enabled)
            with camera_enable_rules_lock:
                previous_values = {
                    key: camera_enable_rules[key] if key in camera_enable_rules else _MISSING
                    for key in rule_keys
                }
            for key in rule_keys:
                set_camera_enable_rule(key, normalized_enabled, persist=False)
            _persist_camera_enable_rules()
            changed = any(
                previous_values.get(key, _MISSING) is _MISSING
                or bool(previous_values.get(key)) != normalized_enabled
                for key in rule_keys
            )
            configured_enabled_key = rule_keys[0] if rule_keys else str(camera_id)
            configured_enabled = normalized_enabled

        effective_enabled = bool(get_feed_enabled(feed))
        if not effective_enabled:
            feed.mark_error("Camera disabled by policy")
            feed.mark_offline()

        return jsonify(
            {
                "status": "success",
                "camera_id": camera_id,
                "changed": bool(changed),
                "enabled": effective_enabled,
                "configured_enabled": configured_enabled if configured_enabled is not None else None,
                "configured_enabled_key": configured_enabled_key or "",
                "message": "Camera state updated",
            }
        )

    return jsonify(
        {
            "status": "success",
            "camera_id": camera_id,
            "enabled": effective_enabled,
            "configured_enabled": configured_enabled if configured_enabled is not None else None,
            "configured_enabled_key": configured_enabled_key or "",
        }
    )


@app.route("/stream_options/<camera_id>", methods=["GET", "POST"])
@require_session
def stream_options_for_camera(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return jsonify({"status": "error", "message": "Camera not found"}), 404

    available_profiles, profile_error = feed.get_available_profiles()
    current_profile, current_revision = feed.get_capture_profile()
    rule_keys = _rotation_rule_keys_for_feed(feed)
    configured_rotation_key = None
    configured_rotation = None
    with camera_rotation_rules_lock:
        for candidate_key in rule_keys:
            if candidate_key in camera_rotation_rules:
                configured_rotation_key = candidate_key
                configured_rotation = camera_rotation_rules[candidate_key]
                break
    effective_rotation = get_feed_rotation_degrees(feed)

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "message": "Payload must be an object"}), 400

        rotation_key_present = False
        rotation_input = None
        for candidate in ("rotation_degrees", "rotate_degrees", "rotation"):
            if candidate in payload:
                rotation_key_present = True
                rotation_input = payload.get(candidate)
                break
        if not rotation_key_present and isinstance(payload.get("stream"), dict):
            stream_payload = payload.get("stream")
            for candidate in ("rotation_degrees", "rotate_degrees", "rotation"):
                if candidate in stream_payload:
                    rotation_key_present = True
                    rotation_input = stream_payload.get(candidate)
                    break
        if not rotation_key_present and "rotate_clockwise" in payload:
            rotation_key_present = True
            rotation_input = 90 if _as_bool(payload.get("rotate_clockwise"), default=False) else 0

        rotation_changed = False
        if rotation_key_present:
            if rotation_input is None:
                changed_keys = [clear_camera_rotation_rule(key, persist=False) for key in rule_keys]
                rotation_changed = any(changed_keys)
                if rotation_changed:
                    _persist_camera_rotation_rules()
                configured_rotation = None
                configured_rotation_key = None
            else:
                normalized_rotation = _parse_rotation_degrees(rotation_input)
                if normalized_rotation is None:
                    return jsonify(
                        {
                            "status": "error",
                            "message": "rotation_degrees must be one of 0, 90, 180, 270",
                            "camera_id": camera_id,
                        }
                    ), 400
                previous_rotation = configured_rotation
                for key in rule_keys:
                    set_camera_rotation_rule(key, normalized_rotation, persist=False)
                _persist_camera_rotation_rules()
                rotation_changed = normalized_rotation != previous_rotation
                configured_rotation = normalized_rotation
                configured_rotation_key = rule_keys[0] if rule_keys else str(camera_id)
            effective_rotation = get_feed_rotation_degrees(feed)

        profile_requested = "profile" in payload or any(
            key in payload for key in ("pixel_format", "width", "height", "fps")
        )
        if profile_requested and feed.source_type != "default":
            return jsonify(
                {
                    "status": "error",
                    "message": "Capture profile updates are only supported for default V4L2 feeds",
                    "camera_id": camera_id,
                }
            ), 400

        changed = False
        applied_profile = current_profile
        next_revision = current_revision
        released_handles = 0
        if profile_requested:
            requested = payload.get("profile", payload)
            if not isinstance(requested, dict):
                return jsonify({"status": "error", "message": "Profile payload must be an object"}), 400

            candidate = {
                "pixel_format": _normalize_pixel_format_code(
                    requested.get("pixel_format", current_profile["pixel_format"])
                ),
                "width": _as_int(requested.get("width"), current_profile["width"], minimum=1, maximum=7680),
                "height": _as_int(requested.get("height"), current_profile["height"], minimum=1, maximum=4320),
                "fps": float(
                    _as_int(
                        requested.get("fps"),
                        int(max(1, round(float(current_profile.get("fps", source_options["camera_capture_fps"]))))),
                        minimum=1,
                        maximum=240,
                    )
                ),
            }

            if available_profiles:
                matched = find_matching_profile(available_profiles, candidate)
                if not matched:
                    return jsonify(
                        {
                            "status": "error",
                            "message": "Requested profile is not available for this camera",
                            "camera_id": camera_id,
                            "current_profile": current_profile,
                            "available_profiles": available_profiles,
                        }
                    ), 400
                candidate = {
                    "pixel_format": _normalize_pixel_format_code(matched.get("pixel_format", "")),
                    "width": int(matched.get("width", current_profile["width"])),
                    "height": int(matched.get("height", current_profile["height"])),
                    "fps": float(matched.get("fps", current_profile["fps"])),
                }

            changed, applied_profile, next_revision = feed.set_capture_profile(candidate)
            if changed:
                released_handles = _release_active_capture_handles(feed.camera_id)
                if released_handles:
                    log(
                        f"[INFO] Capture profile update requested for {feed.device_path or camera_id}; "
                        f"released {released_handles} active capture handle(s)"
                    )

        if not rotation_key_present and not profile_requested:
            return jsonify(
                {
                    "status": "error",
                    "message": "No changes requested. Provide profile fields and/or rotation_degrees.",
                    "camera_id": camera_id,
                }
            ), 400

        return jsonify(
            {
                "status": "success",
                "camera_id": camera_id,
                "changed": bool(changed or rotation_changed),
                "profile_changed": bool(changed),
                "profile_revision": int(next_revision),
                "profile": applied_profile,
                "profile_restart_forced": bool(released_handles > 0),
                "rotation_changed": bool(rotation_changed),
                "configured_rotation_degrees": configured_rotation,
                "configured_rotation_key": configured_rotation_key,
                "effective_rotation_degrees": int(effective_rotation),
                "default_rotation_degrees": int(
                    _rotation_or_default(
                        stream_options.get("default_rotation_degrees"),
                        DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES,
                    )
                ),
                "message": "Camera options updated",
            }
        )

    return jsonify(
        {
            "status": "success",
            "camera_id": camera_id,
            "protocols": stream_protocol_capabilities(),
            "modes": camera_mode_urls(camera_id),
            "profile_mutable": feed.source_type == "default",
            "current_profile": current_profile,
            "profile_revision": int(current_revision),
            "available_profiles": available_profiles,
            "profile_query_error": profile_error,
            "default_rotation_degrees": int(
                _rotation_or_default(stream_options.get("default_rotation_degrees"), DEFAULT_STREAM_DEFAULT_ROTATION_DEGREES)
            ),
            "configured_rotation_degrees": configured_rotation,
            "configured_rotation_key": configured_rotation_key,
            "effective_rotation_degrees": int(effective_rotation),
        }
    )


@app.route("/imu", methods=["GET"])
@require_session
def imu_endpoint():
    with imu_lock:
        data = dict(imu_state)
    return jsonify(data)


@app.route("/imu/stream", methods=["GET"])
@require_session
def imu_stream_endpoint():
    hz = _as_int(request.args.get("hz"), 20, minimum=1, maximum=120)
    interval_seconds = 1.0 / float(max(1, hz))

    def generate():
        while True:
            with imu_lock:
                data = dict(imu_state)
            payload = {
                "accel": data.get("accel"),
                "gyro": data.get("gyro"),
                "server_time_ms": int(time.time() * 1000),
            }
            yield f"event: imu\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            time.sleep(interval_seconds)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/camera/<camera_id>")
@require_session
def snapshot(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return Response(b"Camera not found", status=404)
    if not get_feed_enabled(feed):
        return Response(b"Camera disabled", status=403)
    jpeg = feed.snapshot()
    if not jpeg:
        return Response(b"No frame", status=503)
    return Response(jpeg, mimetype="image/jpeg")


@app.route("/jpeg/<camera_id>")
@require_session
def jpeg_snapshot(camera_id):
    return snapshot(camera_id)


def mjpeg_stream(feed):
    feed.acquire_client()
    try:
        last_frame_id = -1
        while True:
            if not get_feed_enabled(feed):
                break
            with feed.cond:
                if feed.frame_id == last_frame_id:
                    feed.cond.wait(timeout=1.0)
                if feed.frame_id == last_frame_id:
                    continue
                last_frame_id = feed.frame_id
                jpeg = feed.latest_jpeg
            if not jpeg:
                continue
            header = (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
            )
            yield header + jpeg + b"\r\n"
    finally:
        feed.release_client()


@app.route("/video/<camera_id>")
@require_session
def video(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return Response(b"Camera not found", status=404)
    if not get_feed_enabled(feed):
        return Response(b"Camera disabled", status=403)
    return Response(
        mjpeg_stream(feed),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/mjpeg/<camera_id>")
@require_session
def mjpeg_alias(camera_id):
    return video(camera_id)


@app.route("/mpegts/<camera_id>")
@require_session
def mpegts(camera_id):
    if not MPEGTS_AVAILABLE:
        return jsonify({"status": "error", "message": f"{FFMPEG_BIN} not available"}), 503
    feed = get_feed(camera_id)
    if not feed:
        return Response(b"Camera not found", status=404)
    if not get_feed_enabled(feed):
        return jsonify({"status": "error", "message": "Camera disabled", "camera_id": camera_id}), 403

    generator = mpegts_stream(feed)
    if generator is None:
        return jsonify({"status": "error", "message": "MPEG-TS stream unavailable"}), 503

    return Response(
        generator,
        mimetype="video/mp2t",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/webrtc/offer/<camera_id>", methods=["POST"])
@require_session
def webrtc_offer(camera_id):
    if not WEBRTC_AVAILABLE:
        return jsonify(
            {
                "status": "error",
                "message": "WebRTC backend unavailable",
                "detail": WEBRTC_IMPORT_ERROR,
            }
        ), 503

    feed = get_feed(camera_id)
    if not feed:
        return jsonify({"status": "error", "message": "Camera not found"}), 404
    if not get_feed_enabled(feed):
        return jsonify({"status": "error", "message": "Camera disabled", "camera_id": camera_id}), 403

    payload = request.get_json(silent=True) or {}
    offer_sdp = str(payload.get("sdp", "")).strip()
    offer_type = str(payload.get("type", "")).strip()
    if not offer_sdp or not offer_type:
        return jsonify({"status": "error", "message": "Missing SDP offer"}), 400

    try:
        answer = asyncio.run(_create_webrtc_answer(offer_sdp, offer_type, feed))
    except Exception as exc:
        return jsonify({"status": "error", "message": f"WebRTC negotiation failed: {exc}"}), 500

    return jsonify({"status": "success", "answer": answer, "camera_id": camera_id})


@app.route("/webrtc/player/<camera_id>", methods=["GET"])
@require_session
def webrtc_player(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return Response("Camera not found", status=404, mimetype="text/plain")
    if not get_feed_enabled(feed):
        return Response("Camera disabled", status=403, mimetype="text/plain")

    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>WebRTC Player - {camera_id}</title>
    <style>
      body {{ margin: 0; background: #111; color: #fff; font-family: monospace; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; padding: 1rem; }}
      video {{ width: 100%; border-radius: 8px; background: #000; }}
      .meta {{ opacity: 0.85; }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <h2>WebRTC - {camera_id}</h2>
      <video id="v" autoplay muted playsinline controls></video>
      <div id="status" class="meta">Connecting...</div>
    </div>
    <script>
      const params = new URLSearchParams(window.location.search);
      const sessionKey = params.get("session_key") || "";
      const video = document.getElementById("v");
      const status = document.getElementById("status");
      let pc = null;

      async function run() {{
        pc = new RTCPeerConnection();
        pc.addTransceiver("video", {{ direction: "recvonly" }});
        pc.ontrack = (ev) => {{
          if (ev.streams && ev.streams[0]) {{
            video.srcObject = ev.streams[0];
          }}
        }};
        pc.onconnectionstatechange = () => {{
          status.textContent = "State: " + pc.connectionState;
        }};

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        const res = await fetch(`/webrtc/offer/{camera_id}?session_key=${{encodeURIComponent(sessionKey)}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ sdp: offer.sdp, type: offer.type }})
        }});
        const data = await res.json();
        if (!res.ok || data.status !== "success") {{
          status.textContent = "Offer failed: " + (data.message || res.status);
          return;
        }}
        await pc.setRemoteDescription(data.answer);
        status.textContent = "Connected";
      }}

      run().catch((err) => {{
        status.textContent = "Error: " + err;
      }});
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

        if current_tunnel:
            return jsonify(
                {
                    "status": "success",
                    "tunnel_url": current_tunnel,
                    "running": process_running,
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
                    "message": "Tunnel process is not running; URL is stale",
                }
            )
        if tunnel_last_error:
            return jsonify(
                {
                    "status": "error",
                    "running": process_running,
                    "error": tunnel_last_error,
                    "message": "Tunnel failed to start",
                }
            )
        return jsonify(
            {
                "status": "pending",
                "running": process_running,
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
    tunnel_state = "active" if (process_running and current_tunnel) else ("starting" if process_running else "inactive")
    if stale_tunnel and not process_running:
        tunnel_state = "stale"
    if current_error and not process_running and not current_tunnel and not stale_tunnel:
        tunnel_state = "error"

    return jsonify(
        {
            "status": "success",
            "service": "camera_router",
            "local": {
                "base_url": local_base,
                "listen_host": listen_host,
                "listen_port": listen_port,
                "auth_url": f"{local_base}/auth",
                "list_url": f"{local_base}/list",
                "health_url": f"{local_base}/health",
            },
            "tunnel": {
                "state": tunnel_state,
                "tunnel_url": current_tunnel,
                "list_url": f"{current_tunnel}/list" if current_tunnel else "",
                "health_url": f"{current_tunnel}/health" if current_tunnel else "",
                "stale_tunnel_url": stale_tunnel,
                "error": current_error,
            },
            "security": {
                "require_auth": bool(runtime_security["require_auth"]),
                "session_timeout": int(SESSION_TIMEOUT),
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
        statuses = all_feed_statuses()
        online_count = sum(1 for s in statuses if s["online"])
        clients = sum(s["clients"] for s in statuses)
        with sessions_lock:
            session_count = len(sessions)

        ui.update_metric("Feeds", f"{online_count}/{len(statuses)}")
        ui.update_metric("Clients", str(clients))
        ui.update_metric("Sessions", str(session_count))
        ui.update_metric("Requests", str(request_count["value"]))

        process_running = tunnel_process is not None and tunnel_process.poll() is None
        with tunnel_url_lock:
            if tunnel_url and process_running:
                ui.update_metric("Tunnel URL", tunnel_url)
                ui.update_metric("Tunnel", "Active")
            elif tunnel_url and not process_running:
                ui.update_metric("Tunnel", "Stale URL")
            elif tunnel_last_error:
                ui.update_metric("Tunnel", f"Error: {tunnel_last_error}")
            elif process_running:
                ui.update_metric("Tunnel", "Starting...")
            else:
                ui.update_metric("Tunnel", "Stopped")

        top = sorted(statuses, key=lambda x: x["fps"], reverse=True)[:2]
        for idx, stat in enumerate(top, start=1):
            ui.update_metric(f"Top{idx}", f"{stat['id']} {stat['fps']}fps {stat['kbps']}kbps")

        time.sleep(1)


def apply_runtime_security(saved_config):
    global SESSION_TIMEOUT

    password = str(
        _read_config_value(
            saved_config,
            "camera_router.security.password",
            runtime_security["password"],
            legacy_keys=("password",),
        )
    ).strip() or DEFAULT_PASSWORD
    session_timeout = _as_int(
        _read_config_value(
            saved_config,
            "camera_router.security.session_timeout",
            SESSION_TIMEOUT,
            legacy_keys=("session_timeout",),
        ),
        SESSION_TIMEOUT,
        minimum=30,
        maximum=86400,
    )
    require_auth = _as_bool(
        _read_config_value(
            saved_config,
            "camera_router.security.require_auth",
            runtime_security["require_auth"],
            legacy_keys=("require_auth",),
        ),
        default=runtime_security["require_auth"],
    )

    runtime_security["password"] = password
    runtime_security["require_auth"] = require_auth
    SESSION_TIMEOUT = session_timeout

    if ui:
        ui.update_metric("Auth", "Required" if require_auth else "Disabled")
        ui.update_metric("Session Timeout", str(SESSION_TIMEOUT))
        ui.log("Applied live security updates from config save")


def _install_runtime_signal_handlers():
    def _handle_signal(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass


def main():
    global ui, SESSION_TIMEOUT
    _install_runtime_signal_handlers()

    config = load_config()
    settings, changed = _load_camera_settings(config)
    if changed:
        save_config(config)

    runtime_security["password"] = settings["password"]
    runtime_security["require_auth"] = settings["require_auth"]
    SESSION_TIMEOUT = settings["session_timeout"]

    stream_options.update(
        {
            "max_width": settings["stream_max_width"],
            "max_height": settings["stream_max_height"],
            "jpeg_quality": settings["stream_jpeg_quality"],
            "target_fps": settings["stream_target_fps"],
            "webrtc_target_fps": settings["webrtc_target_fps"],
            "mpegts_target_fps": settings["mpegts_target_fps"],
            "mpegts_jpeg_quality": settings["mpegts_jpeg_quality"],
            "default_rotation_degrees": settings["default_rotation_degrees"],
            "rotate_clockwise": settings["rotate_clockwise"],
        }
    )
    with camera_rotation_rules_lock:
        camera_rotation_rules.clear()
        camera_rotation_rules.update(settings["camera_rotation_degrees"])
    with camera_enable_rules_lock:
        camera_enable_rules.clear()
        camera_enable_rules.update(settings["camera_enabled"])
    source_options.update(
        {
            "default_cameras_enabled": settings["default_cameras_enabled"],
            "camera_device_glob": settings["camera_device_glob"],
            "camera_capture_width": settings["camera_capture_width"],
            "camera_capture_height": settings["camera_capture_height"],
            "camera_capture_fps": settings["camera_capture_fps"],
            "realsense_enabled": settings["realsense_enabled"],
            "realsense_stream_depth": settings["realsense_stream_depth"],
            "realsense_stream_ir": settings["realsense_stream_ir"],
        }
    )

    if source_options["realsense_enabled"] and not REALSENSE_AVAILABLE:
        source_options["realsense_enabled"] = False
        if REALSENSE_IMPORT_ERROR:
            log(f"[INFO] RealSense disabled: {REALSENSE_IMPORT_ERROR}")
        else:
            log("[INFO] RealSense disabled: module unavailable")

    if should_enable_default_camera_fallback():
        fallback_candidates = discover_default_devices(source_options["camera_device_glob"])
        if fallback_candidates:
            source_options["default_cameras_enabled"] = True
            log(
                f"[INFO] Auto-enabling default camera fallback "
                f"({len(fallback_candidates)} /dev/video candidate(s) discovered)"
            )
        else:
            log("[WARN] No /dev/video candidates found for fallback camera mode")

    if SAFE_MODE_ACTIVE and source_options["realsense_stream_ir"]:
        source_options["realsense_stream_ir"] = False
        log("[SAFE-MODE] Disabled RealSense IR streams after repeated crash recovery")

    listen_host = settings["listen_host"]
    listen_port = settings["listen_port"]
    enable_tunnel = settings["enable_tunnel"]
    auto_install_cloudflared = settings["auto_install_cloudflared"]
    network_runtime["listen_host"] = listen_host
    network_runtime["listen_port"] = int(listen_port)

    initialize_camera_workers()
    threading.Thread(target=session_cleanup_loop, daemon=True).start()

    if UI_AVAILABLE:
        ui = TerminalUI("Camera Router", config_spec=_build_camera_config_spec(), config_path=CONFIG_PATH)
        ui.on_save(apply_runtime_security)
        ui.log("Starting Camera Router...")

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

    local_url = f"http://{listen_host}:{listen_port}"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
        lan_url = f"http://{lan_ip}:{listen_port}"
    except Exception:
        lan_url = "N/A"

    if ui:
        ui.update_metric("Local URL", local_url)
        ui.update_metric("LAN URL", lan_url)
        ui.update_metric("Mode", "Safe" if SAFE_MODE_ACTIVE else "Normal")
        ui.update_metric("Feeds", f"0/{len(all_feed_statuses())}")
        ui.update_metric("Clients", "0")
        ui.update_metric("Sessions", "0")
        ui.update_metric("Requests", "0")
        ui.update_metric("Auth", "Required" if runtime_security["require_auth"] else "Disabled")
        ui.update_metric("Session Timeout", str(SESSION_TIMEOUT))
        ui.update_metric("Tunnel", "Starting..." if enable_tunnel else "Disabled")

    log(f"Starting camera router on {local_url}")
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
        ui.running = True
        threading.Thread(target=metrics_update_loop, daemon=True).start()
        try:
            ui.start()
        finally:
            log("Shutting down camera router...")
            stop_camera_workers()
            stop_cloudflared_tunnel()
            if WEBRTC_AVAILABLE:
                try:
                    asyncio.run(_close_all_peer_connections())
                except Exception:
                    pass
    else:
        try:
            app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False, threaded=True)
        finally:
            stop_camera_workers()
            stop_cloudflared_tunnel()
            if WEBRTC_AVAILABLE:
                try:
                    asyncio.run(_close_all_peer_connections())
                except Exception:
                    pass


def terminate_process_tree(process):
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if not pid:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        return

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.05)

    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def run_with_supervisor():
    if env_truthy(SUPERVISOR_ENV_CHILD, default=False):
        main()
        return
    if not env_truthy(SUPERVISOR_ENV_ENABLED, default=True):
        main()
        return

    crash_times = []
    backoff = 1.0
    safe_mode_next = False
    stop_requested = threading.Event()
    child_ref = {"process": None}

    def _request_stop(signum, _frame):
        stop_requested.set()
        child = child_ref.get("process")
        if child is not None:
            terminate_process_tree(child)

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _request_stop)
        except Exception:
            pass

    try:
        while True:
            if stop_requested.is_set():
                return

            child_env = os.environ.copy()
            child_env[SUPERVISOR_ENV_CHILD] = "1"
            if safe_mode_next:
                child_env[SUPERVISOR_ENV_SAFE_MODE] = "1"
            else:
                child_env.pop(SUPERVISOR_ENV_SAFE_MODE, None)

            popen_kwargs = {"env": child_env}
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kwargs["start_new_session"] = True

            child = subprocess.Popen([sys.executable] + sys.argv, **popen_kwargs)
            child_ref["process"] = child
            exit_code = child.wait()
            child_ref["process"] = None

            # Ensure lingering child processes (e.g., cloudflared) are torn down.
            terminate_process_tree(child)

            if stop_requested.is_set():
                return

            # Clean exits should not be restarted.
            if exit_code in (0, 130, -signal.SIGINT, -signal.SIGTERM):
                return

            now = time.time()
            crash_times = [ts for ts in crash_times if (now - ts) <= SUPERVISOR_CRASH_WINDOW_SECONDS]
            crash_times.append(now)

            if len(crash_times) >= SUPERVISOR_SAFE_MODE_AFTER_CRASHES and not safe_mode_next:
                safe_mode_next = True
                print(
                    f"[WATCHDOG] Enabling safe mode after {len(crash_times)} crashes in "
                    f"{SUPERVISOR_CRASH_WINDOW_SECONDS:.0f}s. "
                    "RealSense IR will be disabled and CUDA device visibility masked."
                )

            print(
                f"[WATCHDOG] camera_route child exited with code {exit_code}; "
                f"restarting in {backoff:.1f}s..."
            )
            if stop_requested.wait(backoff):
                return
            if len(crash_times) <= 1:
                backoff = 1.0
            else:
                backoff = min(SUPERVISOR_BACKOFF_MAX_SECONDS, backoff * 2.0)
    finally:
        child = child_ref.get("process")
        if child is not None:
            terminate_process_tree(child)
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass


if __name__ == "__main__":
    run_with_supervisor()
