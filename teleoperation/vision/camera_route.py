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
from flask import Flask, Response, jsonify, render_template_string, request
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

DEFAULT_REALSENSE_ENABLED = True
DEFAULT_REALSENSE_STREAM_DEPTH = True
DEFAULT_REALSENSE_STREAM_IR = True
DEFAULT_REALSENSE_START_ATTEMPTS = 4

DEFAULT_STREAM_MAX_WIDTH = 960
DEFAULT_STREAM_MAX_HEIGHT = 540
DEFAULT_STREAM_JPEG_QUALITY = 72
DEFAULT_STREAM_TARGET_FPS = 30
DEFAULT_ROTATE_CLOCKWISE = True
DEFAULT_WEBRTC_TARGET_FPS = 24
DEFAULT_MPEGTS_TARGET_FPS = 24
DEFAULT_MPEGTS_JPEG_QUALITY = 60

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

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
MPEGTS_AVAILABLE = shutil.which(FFMPEG_BIN) is not None
peer_connections = set()
peer_connections_lock = Lock()


def log(message):
    if ui and UI_AVAILABLE:
        ui.log(message)
    else:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {message}")


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
    promote("camera_router.stream.rotate_clockwise", rotate_clockwise)

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
                        id="rotate_clockwise",
                        label="Rotate Clockwise",
                        path="camera_router.stream.rotate_clockwise",
                        value_type="bool",
                        default=DEFAULT_ROTATE_CLOCKWISE,
                        description="Rotate outgoing frame 90deg clockwise.",
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


# ---------------------------------------------------------------------------
# Authentication/session
# ---------------------------------------------------------------------------
def create_session():
    session_key = secrets.token_urlsafe(32)
    now = time.time()
    with sessions_lock:
        sessions[session_key] = {"created_at": now, "last_used": now}
    return session_key


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
    global tunnel_process, tunnel_last_error
    process = tunnel_process
    if not process:
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
        tunnel_last_error = "Tunnel stopped"


def start_cloudflared_tunnel(local_port):
    global tunnel_url, tunnel_process, tunnel_last_error
    cloudflared_path = get_cloudflared_path()
    if not os.path.exists(cloudflared_path):
        cloudflared_path = "cloudflared"

    with tunnel_url_lock:
        tunnel_url = None
    tunnel_last_error = ""

    cmd = [cloudflared_path, "tunnel", "--url", f"http://localhost:{local_port}"]
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
        global tunnel_url, tunnel_last_error
        found_url = False
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
                            tunnel_url = match.group(0)
                            found_url = True
                            tunnel_last_error = ""
                            log("")
                            log("=" * 60)
                            log(f"[TUNNEL] Camera Router URL: {tunnel_url}")
                            log("=" * 60)
                            log("")

        return_code = process.poll()
        if return_code not in (None, 0):
            if not found_url:
                tunnel_last_error = f"cloudflared exited before URL (code {return_code})"
                log(f"[ERROR] {tunnel_last_error}")
            else:
                tunnel_last_error = f"cloudflared exited after startup (code {return_code})"
                log(f"[WARN] {tunnel_last_error}")

    threading.Thread(target=monitor_output, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Camera feed abstraction
# ---------------------------------------------------------------------------
class FrameFeed:
    def __init__(self, camera_id, label):
        self.camera_id = camera_id
        self.label = label
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

    def publish(self, frame, options):
        prepared = prepare_frame(frame, options)
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
        with self.lock:
            return {
                "id": self.camera_id,
                "label": self.label,
                "online": self.online,
                "has_frame": self.latest_jpeg is not None,
                "frame_size": {"width": self.width, "height": self.height},
                "fps": round(self.fps, 2),
                "kbps": round(self.kbps, 2),
                "clients": self.client_count,
                "total_frames": self.total_frames,
                "last_error": self.last_error,
            }


def prepare_frame(frame, options):
    if frame is None:
        return None
    out = frame
    if options.get("rotate_clockwise", False):
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)

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


def register_feed(camera_id, label):
    with camera_feeds_lock:
        feed = FrameFeed(camera_id, label)
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


def _build_gstreamer_capture_pipelines(device_path, width, height, fps):
    width = max(1, int(width))
    height = max(1, int(height))
    fps = max(1, int(fps))
    return [
        (
            "nvv4l2camerasrc "
            f"device={device_path} ! "
            "video/x-raw(memory:NVMM),format=(string)UYVY,"
            f"width=(int){width},height=(int){height},framerate=(fraction){fps}/1 ! "
            "nvvidconv ! video/x-raw,format=(string)BGRx ! "
            "videoconvert ! appsink drop=1 max-buffers=1 sync=false"
        ),
        (
            "v4l2src "
            f"device={device_path} io-mode=2 ! "
            "video/x-raw,"
            f"width=(int){width},height=(int){height},framerate=(fraction){fps}/1 ! "
            "videoconvert ! appsink drop=1 max-buffers=1 sync=false"
        ),
        (
            "v4l2src "
            f"device={device_path} io-mode=2 ! "
            "image/jpeg,"
            f"width=(int){width},height=(int){height},framerate=(fraction){fps}/1 ! "
            "jpegdec ! videoconvert ! appsink drop=1 max-buffers=1 sync=false"
        ),
    ]


def open_default_camera(device_path, width, height, fps):
    if os.name == "nt":
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
    else:
        backends = [cv2.CAP_V4L2, cv2.CAP_ANY]

    attempts = []
    for backend in backends:
        attempts.append(("path", device_path, backend))

    device_index = _video_device_index(device_path)
    if device_index is not None:
        for backend in backends:
            attempts.append(("index", device_index, backend))

    cap_gstreamer = getattr(cv2, "CAP_GSTREAMER", None)
    if os.name != "nt" and cap_gstreamer is not None:
        for pipeline in _build_gstreamer_capture_pipelines(device_path, width, height, fps):
            attempts.append(("gstreamer", pipeline, cap_gstreamer))

    for attempt_kind, source, backend in attempts:
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
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            cap.set(cv2.CAP_PROP_FPS, int(fps))

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

        return cap

    return None


def default_camera_worker(feed, device_path):
    publish_interval = 1.0 / float(max(1, int(stream_options["target_fps"])))
    open_failures = 0
    open_retry_delay = DEFAULT_DEFAULT_CAMERA_OPEN_RETRY_INITIAL_SECONDS

    while service_running.is_set():
        cap = open_default_camera(
            device_path,
            source_options["camera_capture_width"],
            source_options["camera_capture_height"],
            source_options["camera_capture_fps"],
        )
        if cap is None:
            open_failures += 1
            feed.mark_error(f"Unable to open {device_path}")
            if open_failures in (1, 3) or open_failures % 10 == 0:
                log(
                    f"[WARN] Failed to open camera {device_path}; "
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
        log(f"[OK] Camera worker started: {device_path}")
        next_emit = 0.0
        read_failures = 0
        while service_running.is_set():
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
            feed.publish(frame, stream_options)

        cap.release()
        time.sleep(DEFAULT_DEFAULT_CAMERA_DISCONNECT_RETRY_SECONDS)

    feed.mark_offline()


def _is_realsense_ir_profile_error(exc):
    text = str(exc).lower()
    return (
        "failed to resolve the request" in text
        or ("y8i" in text and "y8" in text)
        or ("infrared" in text and "format" in text)
    )


def realsense_worker(rs_ids):
    if not REALSENSE_AVAILABLE:
        return

    publish_interval = 1.0 / float(max(1, int(stream_options["target_fps"])))
    include_ir = bool(source_options["realsense_stream_ir"])
    start_retry_delay = 1.0

    while service_running.is_set():
        cap = RealsenseCapture(stream_ir=include_ir)
        try:
            cap.start(max_retries=DEFAULT_REALSENSE_START_ATTEMPTS)
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
                feed.publish(color, stream_options)

            if source_options["realsense_stream_depth"]:
                feed = get_feed(rs_ids["depth"])
                if feed:
                    feed.publish(depth_vis, stream_options)

            if include_ir and source_options["realsense_stream_ir"]:
                feed = get_feed(rs_ids["ir_left"])
                if feed:
                    feed.publish(ir_left, stream_options)
                feed = get_feed(rs_ids["ir_right"])
                if feed:
                    feed.publish(ir_right, stream_options)

        try:
            cap.release()
        except Exception:
            pass
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
        for index, device in enumerate(devices):
            cam_id = f"default_{index}"
            feed = register_feed(cam_id, f"Default Camera ({device})")
            thread = threading.Thread(target=default_camera_worker, args=(feed, device), daemon=True)
            thread.start()
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
        register_feed(rs_ids["color"], "RealSense D455 - Color")
        if source_options["realsense_stream_depth"]:
            register_feed(rs_ids["depth"], "RealSense D455 - Depth")
        if source_options["realsense_stream_ir"]:
            register_feed(rs_ids["ir_left"], "RealSense D455 - IR Left")
            register_feed(rs_ids["ir_right"], "RealSense D455 - IR Right")
        thread = threading.Thread(target=realsense_worker, args=(rs_ids,), daemon=True)
        thread.start()
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
      .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 1rem; }
      .card { background: #1b1b1b; border: 1px solid #333; border-radius: 10px; padding: 0.8rem; }
      .meta { opacity: 0.85; font-size: 0.85rem; margin: 0.3rem 0; }
      .ok { color: #00d08a; }
      .bad { color: #ff5c5c; }
      img { width: 100%; border-radius: 8px; background: #000; }
      code { color: #ffcc66; }
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
        </div>
        <div id="statusLine" class="meta">Not authenticated.</div>
        <div class="meta">Tip: append <code>?session_key=...</code> to <code>/video/&lt;camera_id&gt;</code> for OpenCV clients.</div>
      </div>
      <div class="panel">
        <h3 style="margin-top:0">/health</h3>
        <pre id="healthOut" class="meta">loading...</pre>
      </div>
      <div id="cards" class="cards"></div>
    </div>
    <script>
      let sessionKey = localStorage.getItem("camera_router_session_key") || "";

      function withSession(path) {
        if (!sessionKey) return path;
        const sep = path.includes("?") ? "&" : "?";
        return `${path}${sep}session_key=${encodeURIComponent(sessionKey)}`;
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
        await refreshList();
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

      function renderCards(cameras) {
        const root = document.getElementById("cards");
        root.innerHTML = "";
        cameras.forEach((cam) => {
          const streamUrl = withSession(cam.video_url);
          const snapUrl = withSession(cam.snapshot_url);
          const card = document.createElement("div");
          card.className = "card";
          card.innerHTML = `
            <h3 style="margin-top:0">${cam.label}</h3>
            <div class="meta ${cam.online ? "ok" : "bad"}">status: ${cam.online ? "online" : "offline"}</div>
            <div class="meta">id: ${cam.id}</div>
            <div class="meta">fps: ${cam.fps} | kbps: ${cam.kbps} | clients: ${cam.clients}</div>
            <img src="${streamUrl}" alt="${cam.label}">
            <div class="meta"><a href="${snapUrl}" target="_blank">snapshot</a> | <a href="${streamUrl}" target="_blank">stream</a></div>
          `;
          root.appendChild(card);
        });
      }

      async function refreshList() {
        try {
          const res = await fetch(withSession("/list"));
          const data = await res.json();
          if (!res.ok || data.status !== "success") {
            document.getElementById("statusLine").textContent = `List failed: ${data.message || res.status}`;
            if (res.status === 401) {
              sessionKey = "";
              localStorage.removeItem("camera_router_session_key");
            }
            return;
          }
          document.getElementById("statusLine").textContent = `Loaded ${data.cameras.length} feeds`;
          renderCards(data.cameras);
        } catch (err) {
          document.getElementById("statusLine").textContent = `List error: ${err}`;
        }
      }

      document.getElementById("connectBtn").addEventListener("click", authenticate);
      document.getElementById("refreshBtn").addEventListener("click", refreshList);
      refreshHealth();
      setInterval(refreshHealth, 3000);
      refreshList();
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


@app.route("/health", methods=["GET"])
def health():
    statuses = all_feed_statuses()
    online_count = sum(1 for s in statuses if s["online"])
    clients = sum(s["clients"] for s in statuses)
    with sessions_lock:
        sessions_active = len(sessions)
    with tunnel_url_lock:
        current_tunnel = tunnel_url
    tunnel_running = tunnel_process is not None and tunnel_process.poll() is None
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
    with tunnel_url_lock:
        current_tunnel = tunnel_url
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
                "health": "/health",
                "list": "/list",
                "imu": "/imu",
                "snapshot": "/camera/<camera_id>",
                "jpeg": "/jpeg/<camera_id>",
                "stream": "/video/<camera_id>",
                "mjpeg": "/mjpeg/<camera_id>",
                "mpegts": "/mpegts/<camera_id>",
                "webrtc_offer": "/webrtc/offer/<camera_id>",
                "webrtc_player": "/webrtc/player/<camera_id>",
                "stream_options": "/stream_options/<camera_id>",
                "router_info": "/router_info",
            },
        }
    )


@app.route("/stream_options/<camera_id>", methods=["GET"])
@require_session
def stream_options_for_camera(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return jsonify({"status": "error", "message": "Camera not found"}), 404
    return jsonify(
        {
            "status": "success",
            "camera_id": camera_id,
            "protocols": stream_protocol_capabilities(),
            "modes": camera_mode_urls(camera_id),
        }
    )


@app.route("/imu", methods=["GET"])
@require_session
def imu_endpoint():
    with imu_lock:
        data = dict(imu_state)
    return jsonify(data)


@app.route("/camera/<camera_id>")
@require_session
def snapshot(camera_id):
    feed = get_feed(camera_id)
    if not feed:
        return Response(b"Camera not found", status=404)
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
        if tunnel_url:
            return jsonify(
                {
                    "status": "success",
                    "tunnel_url": tunnel_url,
                    "running": process_running,
                    "message": "Tunnel URL available",
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
        current_tunnel = tunnel_url
        current_error = tunnel_last_error

    listen_port = int(network_runtime.get("listen_port", DEFAULT_LISTEN_PORT))
    listen_host = str(network_runtime.get("listen_host", DEFAULT_LISTEN_HOST))
    local_base = f"http://127.0.0.1:{listen_port}"
    tunnel_state = "active" if current_tunnel else ("starting" if process_running else "inactive")
    if current_error and not process_running and not current_tunnel:
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
            if tunnel_url:
                ui.update_metric("Tunnel URL", tunnel_url)
                ui.update_metric("Tunnel", "Active")
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


def main():
    global ui, SESSION_TIMEOUT

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
            "rotate_clockwise": settings["rotate_clockwise"],
        }
    )
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

    while True:
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
        try:
            exit_code = child.wait()
        except KeyboardInterrupt:
            terminate_process_tree(child)
            return

        # Ensure lingering child processes (e.g., cloudflared) are torn down.
        terminate_process_tree(child)

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
        time.sleep(backoff)
        if len(crash_times) <= 1:
            backoff = 1.0
        else:
            backoff = min(SUPERVISOR_BACKOFF_MAX_SECONDS, backoff * 2.0)


if __name__ == "__main__":
    run_with_supervisor()
