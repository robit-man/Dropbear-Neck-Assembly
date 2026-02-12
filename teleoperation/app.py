#!/usr/bin/env python3
"""
Teleoperation watchdog manager.

Features:
- Creates its own venv and relaunches from it (no external packages required).
- Curses dashboard showing all managed teleoperation services.
- Auto-starts services in their own terminal windows.
- Per-service toggle with arrow keys + space bar.
- Graceful stop (SIGINT -> SIGTERM -> SIGKILL escalation) when disabling.
"""

import os
import sys
import subprocess


# ---------------------------------------------------------------------------
# Virtual environment bootstrap
# ---------------------------------------------------------------------------
WATCHDOG_VENV_DIR_NAME = "watchdog_venv"


def ensure_venv():
    script_dir = os.path.abspath(os.path.dirname(__file__))
    venv_dir = os.path.join(script_dir, WATCHDOG_VENV_DIR_NAME)
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(venv_dir)):
        return

    if os.name == "nt":
        python_path = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        python_path = os.path.join(venv_dir, "bin", "python")

    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment in '{WATCHDOG_VENV_DIR_NAME}'...")
        import venv

        venv.create(venv_dir, with_pip=True)

    print("Re-launching from venv...")
    os.execv(python_path, [python_path] + sys.argv)


ensure_venv()


# ---------------------------------------------------------------------------
# Imports after venv bootstrap
# ---------------------------------------------------------------------------
import curses
import datetime
import json
import pathlib
import platform
import shlex
import shutil
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple


WATCHDOG_RUNTIME_DIR_NAME = ".watchdog_runtime"
WATCHDOG_STATE_FILE_NAME = "service_state.json"
WATCHDOG_LOCK_FILE_NAME = "watchdog.lock"
MONITOR_INTERVAL_SECONDS = 0.25
STOP_SIGINT_GRACE_SECONDS = 5.0
STOP_SIGTERM_GRACE_SECONDS = 10.0
STOP_SIGKILL_GRACE_SECONDS = 14.0
RESTART_BACKOFF_INITIAL_SECONDS = 1.0
RESTART_BACKOFF_MAX_SECONDS = 20.0
LAUNCH_GRACE_SECONDS = 3.0
DEFAULT_ACTIVATION_TIMEOUT_SECONDS = 35.0
DEFAULT_ACTIVATION_STABILITY_SECONDS = 4.0
DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 2.0
DEFAULT_HEALTH_FAILURE_THRESHOLD = 4
HTTP_PROBE_TIMEOUT_SECONDS = 1.5
PORT_DISCOVERY_SCAN_LIMIT = 64
PORT_DISCOVERY_MAX_DELTA = 128


def _get_nested(data: dict, path: str, default=None):
    current = data
    for key in str(path or "").split("."):
        if not key:
            continue
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    label: str
    script_relpath: str
    args: Tuple[str, ...] = ()
    auto_start: bool = True
    health_mode: str = "process"  # process | tcp | http
    health_port: int = 0
    health_path: str = ""
    config_relpath: str = ""
    config_port_paths: Tuple[str, ...] = ()
    activation_timeout_seconds: float = DEFAULT_ACTIVATION_TIMEOUT_SECONDS
    activation_stability_seconds: float = DEFAULT_ACTIVATION_STABILITY_SECONDS
    health_check_interval_seconds: float = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    health_failure_threshold: int = DEFAULT_HEALTH_FAILURE_THRESHOLD

    def script_path(self, base_dir: pathlib.Path) -> pathlib.Path:
        return (base_dir / self.script_relpath).resolve()

    def working_dir(self, base_dir: pathlib.Path) -> pathlib.Path:
        return self.script_path(base_dir).parent

    def config_path(self, base_dir: pathlib.Path) -> Optional[pathlib.Path]:
        rel = str(self.config_relpath or "").strip()
        if not rel:
            return None
        return (base_dir / rel).resolve()

    def resolved_health_port(self, base_dir: pathlib.Path) -> int:
        cfg_path = self.config_path(base_dir)
        if cfg_path and cfg_path.exists():
            try:
                payload = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    for path in self.config_port_paths:
                        value = _get_nested(payload, path, None)
                        if isinstance(value, bool):
                            continue
                        try:
                            port = int(value)
                        except Exception:
                            continue
                        if 1 <= port <= 65535:
                            return port
            except Exception:
                pass
        try:
            port = int(self.health_port)
        except Exception:
            port = 0
        return port if 1 <= port <= 65535 else 0

    def resolved_health_target(self, base_dir: pathlib.Path) -> str:
        mode = str(self.health_mode or "process").strip().lower()
        if mode == "process":
            return "process"
        port = self.resolved_health_port(base_dir)
        if port <= 0:
            return ""
        if mode == "tcp":
            return f"127.0.0.1:{port}"
        path = str(self.health_path or "").strip()
        if not path:
            path = "/"
        if not path.startswith("/"):
            path = f"/{path}"
        return f"http://127.0.0.1:{port}{path}"


@dataclass
class ServiceRuntime:
    desired_enabled: bool = False
    restart_after_stop: bool = False
    state: str = "stopped"  # stopped | launching | activating | running | degraded | stopping | error | missing
    pid: Optional[int] = None
    terminal_process: Optional[subprocess.Popen] = None
    stop_stage: int = 0
    stop_requested_at: float = 0.0
    next_restart_at: float = 0.0
    restart_backoff_seconds: float = RESTART_BACKOFF_INITIAL_SECONDS
    launch_grace_until: float = 0.0
    started_at: float = 0.0
    stopped_at: float = 0.0
    last_event: str = ""
    last_error: str = ""
    last_state_change_at: float = 0.0
    start_count: int = 0
    stop_count: int = 0
    crash_count: int = 0
    restart_count: int = 0
    launch_attempts: int = 0
    launch_attempt_started_at: float = 0.0
    activation_deadline: float = 0.0
    activation_checks: int = 0
    activation_failures: int = 0
    activation_method: str = ""
    process_stable_since: float = 0.0
    health_checks: int = 0
    health_failures: int = 0
    consecutive_health_failures: int = 0
    last_health_probe_at: float = 0.0
    last_health_ok_at: float = 0.0
    last_health_error: str = ""
    resolved_health_port: int = 0
    last_port_discovery_at: float = 0.0


class WatchdogManager:
    def __init__(self, base_dir: pathlib.Path):
        self.base_dir = base_dir
        self.runtime_dir = self.base_dir / WATCHDOG_RUNTIME_DIR_NAME
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.runtime_dir / WATCHDOG_STATE_FILE_NAME
        self.lock_file = self.runtime_dir / WATCHDOG_LOCK_FILE_NAME

        self.services: List[ServiceSpec] = [
            ServiceSpec(
                "adapter",
                "Adapter",
                "adapter/adapter.py",
                health_mode="http",
                health_port=5160,
                health_path="/health",
                config_relpath="adapter/config.json",
                config_port_paths=("adapter.network.listen_port", "listen_port", "port"),
                activation_timeout_seconds=30.0,
            ),
            ServiceSpec(
                "camera",
                "Camera Router",
                "vision/camera_route.py",
                health_mode="http",
                health_port=8080,
                health_path="/health",
                config_relpath="vision/config.json",
                config_port_paths=("camera_router.network.listen_port", "listen_port", "port"),
                activation_timeout_seconds=50.0,
            ),
            ServiceSpec(
                "depth",
                "Depth",
                "depth/depth.py",
                health_mode="tcp",
                health_port=8080,
                activation_timeout_seconds=45.0,
            ),
            ServiceSpec(
                "router",
                "NKN Router",
                "router/router.py",
                health_mode="http",
                health_port=5070,
                health_path="/health",
                config_relpath="router/config.json",
                config_port_paths=("router.network.listen_port", "listen_port", "router_port"),
                activation_timeout_seconds=35.0,
            ),
            ServiceSpec(
                "frontend",
                "Frontend",
                "frontend/app.py",
                health_mode="http",
                health_port=5000,
                health_path="/tunnel_info",
                config_relpath="frontend/config.json",
                config_port_paths=("app.server.port", "listen_port", "port"),
                activation_timeout_seconds=30.0,
            ),
        ]
        self.service_by_id: Dict[str, ServiceSpec] = {svc.service_id: svc for svc in self.services}
        self.runtime_by_id: Dict[str, ServiceRuntime] = {}

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._logs: Deque[str] = deque(maxlen=300)
        self._selected_index = 0
        self._run_started_at = time.time()
        self._instance_lock_acquired = False
        self._instance_lock_handle = None

        self._os_name = platform.system().lower()
        self._terminal_emulator = self._detect_terminal_emulator()
        if self._os_name not in ("windows", "darwin") and not self._terminal_emulator:
            self._log("[WARN] No terminal emulator detected; Linux service launch will fail")

        self._acquire_instance_lock()

        desired_overrides = self._load_desired_state()
        now = time.time()
        for svc in self.services:
            desired_enabled = desired_overrides.get(svc.service_id)
            if desired_enabled is None:
                desired_enabled = svc.auto_start
            runtime = ServiceRuntime(desired_enabled=bool(desired_enabled), state="stopped")
            script_path = svc.script_path(self.base_dir)
            if not script_path.exists():
                runtime.desired_enabled = False
                runtime.state = "missing"
                runtime.last_error = f"Missing: {svc.script_relpath}"
            else:
                pid = self._read_pid_file(svc)
                if pid and self._is_pid_running(pid):
                    runtime.pid = pid
                    runtime.state = "activating"
                    runtime.started_at = now
                    runtime.launch_attempt_started_at = now
                    runtime.activation_deadline = now + svc.activation_timeout_seconds
                    runtime.process_stable_since = now
                    runtime.last_event = f"Recovered existing pid={pid}; probing health"
                elif pid:
                    self._remove_pid_file(svc)
            runtime.last_state_change_at = now
            self.runtime_by_id[svc.service_id] = runtime

        self._log("Watchdog initialized")
        self._log("Keys: Up/Down select, Space toggle, R restart, A toggle all, Q quit")

    def _load_desired_state(self) -> Dict[str, bool]:
        if not self.state_file.exists():
            return {}
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log(f"[WARN] Failed to read watchdog state: {exc}")
            return {}

        if isinstance(payload, dict):
            raw_services = payload.get("services", payload)
        else:
            raw_services = {}
        if not isinstance(raw_services, dict):
            return {}

        service_ids = set(self.service_by_id.keys())
        loaded = {}
        for service_id, value in raw_services.items():
            if service_id in service_ids and isinstance(value, bool):
                loaded[service_id] = value
        return loaded

    def _save_desired_state(self):
        payload = {
            "version": 1,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "services": {},
        }
        for svc in self.services:
            runtime = self.runtime_by_id.get(svc.service_id)
            if runtime is None:
                continue
            payload["services"][svc.service_id] = bool(runtime.desired_enabled)

        tmp_path = self.state_file.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception as exc:
            self._log(f"[WARN] Failed to persist watchdog state: {exc}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------
    def _log(self, message: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._logs.append(f"[{ts}] {message}")

    def _state_color(self, state: str) -> int:
        if state == "running":
            return 2
        if state in ("launching", "activating", "degraded"):
            return 3
        if state == "stopping":
            return 4
        if state in ("error", "missing"):
            return 5
        return 1

    def _set_state(
        self,
        svc: ServiceSpec,
        runtime: ServiceRuntime,
        new_state: str,
        now: float,
        event: Optional[str] = None,
        error: Optional[str] = None,
    ):
        previous = runtime.state
        runtime.state = str(new_state or runtime.state)
        runtime.last_state_change_at = now
        if event is not None:
            runtime.last_event = event
        if error is not None:
            runtime.last_error = error
        if previous != runtime.state:
            self._log(f"[STATE] {svc.label}: {previous} -> {runtime.state}")

    def _clear_runtime_timers(self, runtime: ServiceRuntime):
        runtime.launch_grace_until = 0.0
        runtime.launch_attempt_started_at = 0.0
        runtime.activation_deadline = 0.0
        runtime.process_stable_since = 0.0
        runtime.last_health_probe_at = 0.0
        runtime.last_health_ok_at = 0.0
        runtime.last_port_discovery_at = 0.0

    def _reset_runtime_health(self, runtime: ServiceRuntime):
        runtime.consecutive_health_failures = 0
        runtime.last_health_error = ""

    def _health_probe(self, svc: ServiceSpec) -> Tuple[bool, str]:
        return self._health_probe_with_runtime(svc, None)

    def _build_health_target(self, svc: ServiceSpec, port_override: int = 0) -> str:
        mode = str(svc.health_mode or "process").strip().lower()
        if mode == "process":
            return "process"
        port = int(port_override or 0)
        if port <= 0:
            port = svc.resolved_health_port(self.base_dir)
        if port <= 0:
            return ""
        if mode == "tcp":
            return f"127.0.0.1:{port}"
        path = str(svc.health_path or "").strip()
        if not path:
            path = "/"
        if not path.startswith("/"):
            path = f"/{path}"
        return f"http://127.0.0.1:{port}{path}"

    def _probe_target(self, mode: str, target: str) -> Tuple[bool, str]:
        mode = str(mode or "process").strip().lower()
        if mode == "process":
            return True, "process-only probe"
        if not target:
            return False, "missing probe target"
        if mode == "tcp":
            try:
                host, port_text = target.rsplit(":", 1)
                port = int(port_text)
            except Exception:
                return False, f"invalid tcp target {target!r}"
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(HTTP_PROBE_TIMEOUT_SECONDS)
                try:
                    ok = sock.connect_ex((host, port)) == 0
                except Exception as exc:
                    return False, str(exc)
            return (ok, "tcp open" if ok else "tcp closed")

        # Default HTTP probe.
        request = urllib.request.Request(target, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_PROBE_TIMEOUT_SECONDS) as response:
                code = int(getattr(response, "status", 200) or 200)
            if 200 <= code < 400:
                return True, f"http {code}"
            return False, f"http {code}"
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            # 401/403 still means the service is up; activation should be considered successful.
            if code in (401, 403):
                return True, f"http {code} (auth required)"
            return False, f"http {code}"
        except Exception as exc:
            return False, str(exc)

    def _list_listening_ports_for_pid(self, pid: int) -> List[int]:
        if not pid or pid <= 0:
            return []
        ports = set()
        if self._os_name == "windows":
            try:
                result = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                    timeout=2.5,
                )
                for raw in (result.stdout or "").splitlines():
                    line = raw.strip()
                    if not line or not line.upper().startswith("TCP"):
                        continue
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    state = str(parts[3]).strip().upper()
                    if state != "LISTENING":
                        continue
                    try:
                        line_pid = int(parts[4])
                    except Exception:
                        continue
                    if line_pid != int(pid):
                        continue
                    local = str(parts[1]).strip()
                    if local.startswith("[") and "]:" in local:
                        port_text = local.rsplit("]:", 1)[-1]
                    else:
                        port_text = local.rsplit(":", 1)[-1]
                    try:
                        port = int(port_text)
                    except Exception:
                        continue
                    if 1 <= port <= 65535:
                        ports.add(port)
            except Exception:
                return []
            return sorted(ports)

        # POSIX fallback via lsof when available.
        if shutil.which("lsof"):
            try:
                result = subprocess.run(
                    ["lsof", "-Pan", "-p", str(int(pid)), "-iTCP", "-sTCP:LISTEN"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                    timeout=2.5,
                )
                for raw in (result.stdout or "").splitlines():
                    match = re.search(r":(\d+)\s+\(LISTEN\)", raw)
                    if not match:
                        continue
                    try:
                        port = int(match.group(1))
                    except Exception:
                        continue
                    if 1 <= port <= 65535:
                        ports.add(port)
            except Exception:
                pass
        return sorted(ports)

    def _discover_runtime_health_port(
        self,
        svc: ServiceSpec,
        runtime: Optional[ServiceRuntime],
        attempted_port: int,
    ) -> int:
        if runtime is None:
            return 0
        pid = int(runtime.pid or 0)
        if pid <= 0 or not self._is_pid_running(pid):
            return 0

        discovered_ports = self._list_listening_ports_for_pid(pid)
        if not discovered_ports:
            return 0

        preferred = int(svc.resolved_health_port(self.base_dir) or 0)
        current = int(runtime.resolved_health_port or 0)
        attempted = int(attempted_port or 0)

        def _rank(port_value: int):
            delta = abs(port_value - preferred) if preferred else 0
            # Favor previously discovered/current port, then default/config-adjacent ports.
            return (
                0 if current and port_value == current else 1,
                0 if preferred and port_value == preferred else 1,
                0 if preferred and delta <= PORT_DISCOVERY_MAX_DELTA else 1,
                delta,
                port_value,
            )

        mode = str(svc.health_mode or "process").strip().lower()
        for port in sorted(discovered_ports, key=_rank)[:PORT_DISCOVERY_SCAN_LIMIT]:
            if port == attempted:
                continue
            target = self._build_health_target(svc, port_override=port)
            ok, _detail = self._probe_target(mode, target)
            if ok:
                return port
        return 0

    def _health_probe_with_runtime(
        self,
        svc: ServiceSpec,
        runtime: Optional[ServiceRuntime],
    ) -> Tuple[bool, str]:
        mode = str(svc.health_mode or "process").strip().lower()
        if mode == "process":
            return True, "process-only probe"

        preferred_port = int(svc.resolved_health_port(self.base_dir) or 0)
        active_port = int((runtime.resolved_health_port if runtime else 0) or preferred_port or 0)
        target = self._build_health_target(svc, port_override=active_port)
        ok, detail = self._probe_target(mode, target)
        if ok:
            if runtime is not None and active_port > 0:
                runtime.resolved_health_port = active_port
            return True, detail

        discovered_port = self._discover_runtime_health_port(svc, runtime, active_port)
        if discovered_port > 0 and discovered_port != active_port:
            discovered_target = self._build_health_target(svc, port_override=discovered_port)
            discovered_ok, discovered_detail = self._probe_target(mode, discovered_target)
            if discovered_ok:
                if runtime is not None:
                    runtime.resolved_health_port = discovered_port
                    runtime.last_port_discovery_at = time.time()
                self._log(f"[DISCOVER] {svc.label} health probe realigned to port {discovered_port}")
                return True, f"{discovered_detail} (port {discovered_port})"

        return False, detail

    def _schedule_restart(self, runtime: ServiceRuntime, now: float):
        runtime.next_restart_at = now + runtime.restart_backoff_seconds
        runtime.restart_backoff_seconds = min(
            RESTART_BACKOFF_MAX_SECONDS, runtime.restart_backoff_seconds * 2.0
        )

    def _mark_launch_failure(
        self,
        svc: ServiceSpec,
        runtime: ServiceRuntime,
        now: float,
        reason: str,
        increment_crash: bool = False,
    ):
        runtime.activation_failures += 1
        runtime.health_failures += 1
        runtime.last_health_error = reason
        if increment_crash:
            runtime.crash_count += 1
        runtime.pid = None
        runtime.terminal_process = None
        runtime.resolved_health_port = 0
        runtime.started_at = 0.0
        runtime.stopped_at = now
        self._clear_runtime_timers(runtime)
        self._set_state(svc, runtime, "error", now, event="Waiting to restart", error=reason)
        self._schedule_restart(runtime, now)

    # ------------------------------------------------------------------
    # PID and signaling helpers
    # ------------------------------------------------------------------
    def _pid_file(self, svc: ServiceSpec) -> pathlib.Path:
        return self.runtime_dir / f"{svc.service_id}.pid"

    def _read_pid_file(self, svc: ServiceSpec) -> Optional[int]:
        path = self._pid_file(svc)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8").strip()
            pid = int(raw)
            return pid if pid > 0 else None
        except Exception:
            return None

    def _write_pid_file(self, svc: ServiceSpec, pid: int):
        path = self._pid_file(svc)
        try:
            path.write_text(str(int(pid)), encoding="utf-8")
        except Exception:
            pass

    def _remove_pid_file(self, svc: ServiceSpec):
        path = self._pid_file(svc)
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def _acquire_instance_lock(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        handle = None
        existing_pid = None
        try:
            handle = open(self.lock_file, "a+", encoding="utf-8")
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    handle.seek(0)
                    raw = handle.read().strip()
                    try:
                        existing_pid = int(raw) if raw else None
                    except Exception:
                        existing_pid = None
                    suffix = f" (pid {existing_pid})" if existing_pid else ""
                    raise RuntimeError(
                        f"Another watchdog instance is already running{suffix}"
                    )
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.seek(0)
                    raw = handle.read().strip()
                    try:
                        existing_pid = int(raw) if raw else None
                    except Exception:
                        existing_pid = None
                    suffix = f" (pid {existing_pid})" if existing_pid else ""
                    raise RuntimeError(
                        f"Another watchdog instance is already running{suffix}"
                    )

            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            self._instance_lock_acquired = True
            self._instance_lock_handle = handle
        except RuntimeError:
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
            raise RuntimeError(f"Failed to acquire watchdog lock: {exc}") from exc

    def _release_instance_lock(self):
        if not self._instance_lock_acquired:
            return
        try:
            handle = self._instance_lock_handle
            if handle:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass
                self._instance_lock_handle = None

            if not self.lock_file.exists():
                return
            raw = self.lock_file.read_text(encoding="utf-8").strip()
            if str(os.getpid()) == raw:
                self.lock_file.unlink()
        except Exception:
            pass
        finally:
            self._instance_lock_acquired = False

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        if not pid or pid <= 0:
            return False
        if os.name == "nt":
            try:
                import ctypes

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                STILL_ACTIVE = 259
                handle = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
                )
                if not handle:
                    return False
                try:
                    exit_code = ctypes.c_ulong()
                    if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return False
                    return int(exit_code.value) == STILL_ACTIVE
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                try:
                    result = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        check=False,
                    )
                    output = (result.stdout or "").strip().lower()
                    return bool(output) and "no tasks are running" not in output
                except Exception:
                    return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _send_signal(self, pid: int, sig: int):
        if not pid:
            return
        if self._os_name == "windows":
            if sig == signal.SIGINT:
                try:
                    os.kill(pid, signal.CTRL_BREAK_EVENT)
                    return
                except Exception:
                    pass
            if sig == signal.SIGKILL:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                return
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return

        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            return
        except Exception:
            pass

        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except Exception:
            return

    # ------------------------------------------------------------------
    # Launch helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_process_handle_running(process: Optional[subprocess.Popen]) -> bool:
        if process is None:
            return False
        try:
            return process.poll() is None
        except Exception:
            return False

    def _detect_terminal_emulator(self) -> str:
        if self._os_name == "windows":
            return "windows-console"
        if self._os_name == "darwin":
            return "osascript"

        env_terminal = os.environ.get("TERMINAL", "").strip()
        if env_terminal and shutil.which(env_terminal):
            return env_terminal

        for candidate in (
            "gnome-terminal",
            "konsole",
            "xfce4-terminal",
            "x-terminal-emulator",
            "lxterminal",
            "xterm",
        ):
            if shutil.which(candidate):
                return candidate
        return ""

    def _build_service_shell_command(self, svc: ServiceSpec) -> str:
        workdir = shlex.quote(str(svc.working_dir(self.base_dir)))
        python_cmd = shlex.quote(sys.executable)
        script_cmd = shlex.quote(str(svc.script_path(self.base_dir)))
        args = " ".join(shlex.quote(arg) for arg in svc.args)
        core = f"cd {workdir}; {python_cmd} {script_cmd}"
        if args:
            core += f" {args}"
        return core

    def _build_wrapped_shell_command(self, svc: ServiceSpec) -> str:
        pidfile = shlex.quote(str(self._pid_file(svc)))
        core = self._build_service_shell_command(svc)
        # Keep terminal open with the service in foreground while tracking the real child PID.
        return (
            f"{core} & child=$!; "
            f"echo $child > {pidfile}; "
            f"wait $child; "
            f"rc=$?; "
            f"rm -f {pidfile}; "
            f"exit $rc"
        )

    def _build_terminal_command(self, title: str, wrapped_cmd: str) -> Optional[List[str]]:
        term = self._terminal_emulator
        if not term:
            return None

        if term == "gnome-terminal":
            return [term, "--title", title, "--", "bash", "-lc", wrapped_cmd]
        if term == "konsole":
            return [term, "--new-tab", "-p", f"tabtitle={title}", "-e", "bash", "-lc", wrapped_cmd]
        if term == "xfce4-terminal":
            return [term, "--title", title, "--command", f"bash -lc {shlex.quote(wrapped_cmd)}"]
        if term == "xterm":
            return [term, "-T", title, "-e", "bash", "-lc", wrapped_cmd]
        if term == "x-terminal-emulator":
            return [term, "-e", "bash", "-lc", wrapped_cmd]
        if term == "lxterminal":
            return [term, "-t", title, "-e", f"bash -lc {shlex.quote(wrapped_cmd)}"]

        return [term, "-e", "bash", "-lc", wrapped_cmd]

    def _launch_service(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        script_path = svc.script_path(self.base_dir)
        if not script_path.exists():
            self._set_state(
                svc,
                runtime,
                "missing",
                now,
                event="Service script missing",
                error=f"Missing: {svc.script_relpath}",
            )
            runtime.desired_enabled = False
            return

        runtime.launch_attempts += 1
        runtime.activation_method = ""
        runtime.activation_checks = 0
        runtime.resolved_health_port = 0
        runtime.launch_attempt_started_at = now
        runtime.activation_deadline = now + max(5.0, float(svc.activation_timeout_seconds))
        runtime.launch_grace_until = now + LAUNCH_GRACE_SECONDS
        runtime.process_stable_since = 0.0
        runtime.last_health_error = ""
        self._set_state(
            svc,
            runtime,
            "launching",
            now,
            event=f"Launch attempt #{runtime.launch_attempts}",
            error="",
        )

        child_env = os.environ.copy()
        # app.py already supervises restarts; disable nested camera self-supervision.
        if svc.service_id == "camera":
            child_env["CAMERA_ROUTE_SUPERVISE"] = "0"
        if svc.service_id == "adapter":
            # Avoid blocking prompts under watchdog; keep adapter API booting even without serial.
            child_env["ADAPTER_DISABLE_INTERACTIVE_PROMPTS"] = "1"

        if self._os_name == "windows":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(script_path)] + list(svc.args),
                    cwd=str(svc.working_dir(self.base_dir)),
                    creationflags=creationflags,
                    env=child_env,
                )
                runtime.terminal_process = proc
                runtime.pid = proc.pid
                runtime.process_stable_since = now
                runtime.stop_stage = 0
                runtime.stop_requested_at = 0.0
                self._set_state(
                    svc,
                    runtime,
                    "activating",
                    now,
                    event=f"Spawned pid={proc.pid}; waiting for activation probe",
                    error="",
                )
                runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
                self._write_pid_file(svc, proc.pid)
                self._log(f"[LAUNCH] {svc.label} attempt={runtime.launch_attempts} pid={proc.pid}")
                return
            except Exception as exc:
                self._mark_launch_failure(svc, runtime, now, f"Launch failed: {exc}")
                self._log(f"[ERROR] {svc.label} launch failed on attempt={runtime.launch_attempts}: {exc}")
                return

        if self._os_name == "darwin":
            wrapped = self._build_wrapped_shell_command(svc)
            escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"')
            apple_script = f'tell application "Terminal" to do script "bash -lc \\"{escaped}\\""'
            try:
                proc = subprocess.Popen(["osascript", "-e", apple_script], env=child_env)
                runtime.terminal_process = proc
                runtime.stop_stage = 0
                runtime.stop_requested_at = 0.0
                self._set_state(
                    svc,
                    runtime,
                    "launching",
                    now,
                    event="Launching terminal host (awaiting child pid)",
                    error="",
                )
                self._log(f"[LAUNCH] {svc.label} attempt={runtime.launch_attempts} via Terminal.app")
                return
            except Exception as exc:
                self._mark_launch_failure(svc, runtime, now, f"Launch failed: {exc}")
                self._log(f"[ERROR] {svc.label} launch failed on attempt={runtime.launch_attempts}: {exc}")
                return

        wrapped = self._build_wrapped_shell_command(svc)
        terminal_cmd = self._build_terminal_command(f"Teleop - {svc.label}", wrapped)
        if not terminal_cmd:
            self._set_state(
                svc,
                runtime,
                "error",
                now,
                event="Launch failed",
                error="No terminal emulator found",
            )
            runtime.desired_enabled = False
            self._clear_runtime_timers(runtime)
            self._log(f"[ERROR] {svc.label}: no terminal emulator found")
            return

        try:
            proc = subprocess.Popen(terminal_cmd, cwd=str(self.base_dir), env=child_env)
            runtime.terminal_process = proc
            runtime.stop_stage = 0
            runtime.stop_requested_at = 0.0
            self._set_state(
                svc,
                runtime,
                "launching",
                now,
                event="Launching terminal host (awaiting child pid)",
                error="",
            )
            self._log(f"[LAUNCH] {svc.label} attempt={runtime.launch_attempts} via {self._terminal_emulator}")
        except Exception as exc:
            self._mark_launch_failure(svc, runtime, now, f"Launch failed: {exc}")
            self._log(f"[ERROR] {svc.label} launch failed on attempt={runtime.launch_attempts}: {exc}")

    # ------------------------------------------------------------------
    # Runtime synchronization
    # ------------------------------------------------------------------
    def _refresh_pid_state(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        if runtime.terminal_process and runtime.terminal_process.poll() is not None:
            runtime.terminal_process = None

        if runtime.pid and not self._is_pid_running(runtime.pid):
            runtime.pid = None

        if (
            runtime.pid is None
            and self._os_name == "windows"
            and self._is_process_handle_running(runtime.terminal_process)
        ):
            runtime.pid = int(runtime.terminal_process.pid)

        if runtime.pid is None:
            pid_from_file = self._read_pid_file(svc)
            if pid_from_file and self._is_pid_running(pid_from_file):
                runtime.pid = pid_from_file
            elif pid_from_file:
                self._remove_pid_file(svc)

        if runtime.pid:
            if runtime.process_stable_since <= 0:
                runtime.process_stable_since = now
            if runtime.state == "launching":
                self._set_state(
                    svc,
                    runtime,
                    "activating",
                    now,
                    event=f"Captured child pid={runtime.pid}; activation probe pending",
                )
        else:
            runtime.process_stable_since = 0.0

    def _begin_stop(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        self._set_state(svc, runtime, "stopping", now, event="Stopping (SIGINT)", error="")
        runtime.stop_count += 1
        runtime.stop_stage = 1
        runtime.stop_requested_at = now
        if runtime.pid:
            self._send_signal(runtime.pid, signal.SIGINT)
            self._log(f"[STOP] {svc.label} SIGINT pid={runtime.pid}")
        elif runtime.terminal_process and runtime.terminal_process.poll() is None:
            try:
                runtime.terminal_process.terminate()
                self._log(f"[STOP] {svc.label} terminal terminate")
            except Exception:
                pass

    def _handle_stop_escalation(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        if runtime.state != "stopping":
            return
        terminal_running = self._is_process_handle_running(runtime.terminal_process)
        pid_running = bool(runtime.pid and self._is_pid_running(runtime.pid))
        if not pid_running and not terminal_running:
            self._set_state(svc, runtime, "stopped", now, event="Stopped", error="")
            runtime.pid = None
            runtime.terminal_process = None
            runtime.resolved_health_port = 0
            runtime.stop_stage = 0
            runtime.stop_requested_at = 0.0
            self._clear_runtime_timers(runtime)
            self._reset_runtime_health(runtime)
            runtime.started_at = 0.0
            runtime.stopped_at = now
            runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
            self._remove_pid_file(svc)
            self._log(f"[STOPPED] {svc.label}")
            return

        if not runtime.pid and terminal_running and runtime.terminal_process:
            runtime.pid = int(runtime.terminal_process.pid)

        elapsed = now - runtime.stop_requested_at
        if runtime.stop_stage == 1 and elapsed >= STOP_SIGINT_GRACE_SECONDS:
            runtime.stop_stage = 2
            runtime.last_event = "Stopping (SIGTERM)"
            if runtime.pid:
                self._send_signal(runtime.pid, signal.SIGTERM)
                self._log(f"[STOP] {svc.label} SIGTERM pid={runtime.pid}")
            return

        if runtime.stop_stage == 2 and elapsed >= STOP_SIGTERM_GRACE_SECONDS:
            runtime.stop_stage = 3
            runtime.last_event = "Stopping (SIGKILL)"
            if runtime.pid:
                self._send_signal(runtime.pid, signal.SIGKILL)
                self._log(f"[STOP] {svc.label} SIGKILL pid={runtime.pid}")
            return

        if runtime.stop_stage == 3 and elapsed >= STOP_SIGKILL_GRACE_SECONDS:
            self._set_state(
                svc,
                runtime,
                "error",
                now,
                event="Stop escalation failed",
                error="Could not stop process",
            )

    def _sync_single_service(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        if runtime.state == "missing":
            return

        self._refresh_pid_state(svc, runtime, now)
        terminal_running = self._is_process_handle_running(runtime.terminal_process)
        if (
            not runtime.pid
            and self._os_name == "windows"
            and terminal_running
            and runtime.terminal_process
        ):
            runtime.pid = int(runtime.terminal_process.pid)
        pid_running = bool(runtime.pid and self._is_pid_running(runtime.pid))
        running = bool(pid_running or (self._os_name == "windows" and terminal_running))

        if not runtime.desired_enabled:
            should_stop = running or (
                runtime.state in ("launching", "activating", "running", "degraded")
                and terminal_running
            )
            if should_stop and runtime.state != "stopping":
                self._begin_stop(svc, runtime, now)
            self._handle_stop_escalation(svc, runtime, now)
            if not should_stop and runtime.restart_after_stop:
                runtime.restart_after_stop = False
                runtime.desired_enabled = True
                runtime.next_restart_at = now
                runtime.last_event = "Restart queued"
                self._save_desired_state()
            elif not should_stop and not runtime.restart_after_stop:
                if runtime.state not in ("stopped", "missing"):
                    self._set_state(svc, runtime, "stopped", now, event="Stopped", error="")
                    runtime.started_at = 0.0
                    runtime.stopped_at = now
                    self._clear_runtime_timers(runtime)
                    self._reset_runtime_health(runtime)
            return

        if not running and runtime.state in ("running", "degraded"):
            self._remove_pid_file(svc)
            self._mark_launch_failure(
                svc,
                runtime,
                now,
                "Exited unexpectedly",
                increment_crash=True,
            )
            self._log(f"[WARN] {svc.label} exited unexpectedly; waiting to restart")
            return

        if runtime.state == "stopping":
            # Desired on while stopping means a restart request.
            runtime.restart_after_stop = True
            self._handle_stop_escalation(svc, runtime, now)
            return

        if runtime.state == "launching":
            if not running:
                if terminal_running and now < runtime.launch_grace_until:
                    return
                if now < runtime.launch_grace_until:
                    return
                if runtime.terminal_process and runtime.terminal_process.poll() is not None:
                    code = runtime.terminal_process.returncode
                    self._mark_launch_failure(
                        svc, runtime, now, f"Terminal exited before child activation ({code})"
                    )
                    self._log(f"[ERROR] {svc.label} terminal host exited ({code})")
                    return
            # Wait for pid capture before activation.
            if not runtime.pid:
                if now > runtime.activation_deadline:
                    self._mark_launch_failure(
                        svc, runtime, now, "Activation timed out waiting for child pid"
                    )
                    self._log(f"[ERROR] {svc.label} activation timeout (no child pid captured)")
                return
            self._set_state(
                svc,
                runtime,
                "activating",
                now,
                event=f"Child pid={runtime.pid} captured; probing readiness",
            )

        if runtime.state == "activating":
            if not running:
                self._mark_launch_failure(
                    svc, runtime, now, "Process exited during activation", increment_crash=True
                )
                self._log(f"[ERROR] {svc.label} exited during activation")
                return

            runtime.activation_checks += 1
            probe_ok, probe_detail = self._health_probe_with_runtime(svc, runtime)
            if probe_ok:
                runtime.start_count += 1
                runtime.started_at = now
                runtime.next_restart_at = 0.0
                runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
                runtime.activation_method = f"probe:{probe_detail}"
                runtime.last_health_ok_at = now
                runtime.last_health_probe_at = now
                self._reset_runtime_health(runtime)
                self._set_state(
                    svc,
                    runtime,
                    "running",
                    now,
                    event=f"Activated ({probe_detail})",
                    error="",
                )
                self._log(
                    f"[RUNNING] {svc.label} pid={runtime.pid} activated via {probe_detail} "
                    f"attempt={runtime.launch_attempts}"
                )
                return

            runtime.last_health_error = probe_detail
            alive_for = now - (runtime.process_stable_since or now)
            if alive_for >= max(1.5, float(svc.activation_stability_seconds)):
                runtime.start_count += 1
                runtime.started_at = now
                runtime.next_restart_at = 0.0
                runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
                runtime.activation_method = f"stable-process:{alive_for:.1f}s"
                self._set_state(
                    svc,
                    runtime,
                    "degraded",
                    now,
                    event=f"Activated by process stability; health probe failing ({probe_detail})",
                    error="",
                )
                self._log(
                    f"[RUNNING] {svc.label} pid={runtime.pid} treated active after "
                    f"{alive_for:.1f}s stability (probe={probe_detail})"
                )
                return

            if now > runtime.activation_deadline:
                self._mark_launch_failure(
                    svc, runtime, now, f"Activation timeout ({probe_detail})"
                )
                self._log(f"[ERROR] {svc.label} activation timeout: {probe_detail}")
                return

            runtime.last_event = f"Activating... probe={probe_detail}"
            return

        if runtime.state in ("running", "degraded"):
            if not running:
                self._mark_launch_failure(
                    svc, runtime, now, "Exited unexpectedly", increment_crash=True
                )
                self._log(f"[WARN] {svc.label} exited unexpectedly; waiting to restart")
                return

            interval = max(0.6, float(svc.health_check_interval_seconds))
            if runtime.last_health_probe_at and (now - runtime.last_health_probe_at) < interval:
                return

            runtime.health_checks += 1
            runtime.last_health_probe_at = now
            probe_ok, probe_detail = self._health_probe_with_runtime(svc, runtime)
            if probe_ok:
                runtime.last_health_ok_at = now
                runtime.consecutive_health_failures = 0
                runtime.last_health_error = ""
                if runtime.state != "running":
                    self._set_state(
                        svc,
                        runtime,
                        "running",
                        now,
                        event=f"Health restored ({probe_detail})",
                        error="",
                    )
                else:
                    runtime.last_event = f"Healthy ({probe_detail})"
                return

            runtime.health_failures += 1
            runtime.consecutive_health_failures += 1
            runtime.last_health_error = probe_detail
            failure_limit = max(1, int(svc.health_failure_threshold))
            if runtime.consecutive_health_failures >= failure_limit:
                self._set_state(
                    svc,
                    runtime,
                    "degraded",
                    now,
                    event=f"Health degraded ({runtime.consecutive_health_failures} fails)",
                    error="",
                )
            else:
                runtime.last_event = (
                    f"Probe soft-fail {runtime.consecutive_health_failures}/{failure_limit}: {probe_detail}"
                )
            return

        if runtime.state == "error" and runtime.next_restart_at > now:
            return

        if runtime.next_restart_at > now:
            return

        self._launch_service(svc, runtime, now)

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            now = time.time()
            with self._lock:
                for svc in self.services:
                    runtime = self.runtime_by_id[svc.service_id]
                    self._sync_single_service(svc, runtime, now)
            time.sleep(MONITOR_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------
    def start(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def request_shutdown(self):
        with self._lock:
            for svc in self.services:
                runtime = self.runtime_by_id[svc.service_id]
                runtime.desired_enabled = False
                runtime.restart_after_stop = False
                if runtime.state not in ("missing",):
                    runtime.last_event = "Shutdown requested"
        self._stop_event.set()

    def await_shutdown(self, timeout: float = 15.0):
        end = time.time() + timeout
        while time.time() < end:
            all_stopped = True
            with self._lock:
                now = time.time()
                for svc in self.services:
                    runtime = self.runtime_by_id[svc.service_id]
                    self._sync_single_service(svc, runtime, now)
                    if runtime.state not in ("stopped", "missing"):
                        all_stopped = False
            if all_stopped:
                break
            time.sleep(0.25)

        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)

    def toggle_selected(self):
        svc = self.services[self._selected_index]
        runtime = self.runtime_by_id[svc.service_id]
        if runtime.state == "missing":
            return
        if runtime.desired_enabled:
            runtime.desired_enabled = False
            runtime.restart_after_stop = False
            runtime.last_event = "Disabled by user"
            self._log(f"[USER] Disabled {svc.label}")
        else:
            runtime.desired_enabled = True
            runtime.next_restart_at = time.time()
            runtime.last_event = "Enabled by user"
            self._log(f"[USER] Enabled {svc.label}")
        self._save_desired_state()

    def restart_selected(self):
        svc = self.services[self._selected_index]
        runtime = self.runtime_by_id[svc.service_id]
        if runtime.state == "missing":
            return
        runtime.restart_count += 1
        runtime.restart_after_stop = True
        runtime.desired_enabled = False
        runtime.last_event = "Restart requested"
        self._log(f"[USER] Restart {svc.label}")

    def toggle_all(self):
        with self._lock:
            any_disabled = any(
                (rt.state != "missing" and not rt.desired_enabled)
                for rt in self.runtime_by_id.values()
            )
            target_enabled = any_disabled
            for svc in self.services:
                runtime = self.runtime_by_id[svc.service_id]
                if runtime.state == "missing":
                    continue
                runtime.restart_after_stop = False
                runtime.desired_enabled = target_enabled
                runtime.last_event = "Enabled all" if target_enabled else "Disabled all"
            self._log(f"[USER] {'Enabled' if target_enabled else 'Disabled'} all services")
            self._save_desired_state()

    # ------------------------------------------------------------------
    # Curses UI
    # ------------------------------------------------------------------
    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds <= 0:
            return "-"
        total = int(seconds)
        hrs = total // 3600
        mins = (total % 3600) // 60
        secs = total % 60
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    def _runtime_detail(self, runtime: ServiceRuntime, now: float) -> str:
        base = runtime.last_error if runtime.last_error else runtime.last_event
        tags = []
        if runtime.launch_attempts > 0:
            tags.append(f"a{runtime.launch_attempts}")
        if runtime.state in ("launching", "activating") and runtime.activation_deadline > 0:
            remaining = max(0.0, runtime.activation_deadline - now)
            tags.append(f"t-{remaining:.0f}s")
        if runtime.health_checks > 0:
            tags.append(f"h{runtime.health_checks}/{runtime.health_failures}")
        if runtime.consecutive_health_failures > 0:
            tags.append(f"cf{runtime.consecutive_health_failures}")
        if runtime.resolved_health_port > 0:
            tags.append(f"p{runtime.resolved_health_port}")
        if tags:
            return f"{base} [{' '.join(tags)}]".strip()
        return base

    def _build_exit_report(self, interrupted: bool = False) -> str:
        now = time.time()
        with self._lock:
            rows = []
            for svc in self.services:
                runtime = self.runtime_by_id[svc.service_id]
                if runtime.stopped_at > runtime.started_at > 0 and runtime.state not in (
                    "running",
                    "degraded",
                    "activating",
                    "launching",
                ):
                    active_for = runtime.stopped_at - runtime.started_at
                elif runtime.started_at > 0:
                    active_for = now - runtime.started_at
                else:
                    active_for = 0.0
                rows.append(
                    {
                        "label": svc.label,
                        "desired": "ON" if runtime.desired_enabled else "OFF",
                        "state": runtime.state.upper(),
                        "pid": runtime.pid or "-",
                        "uptime": self._format_duration(active_for),
                        "starts": runtime.start_count,
                        "stops": runtime.stop_count,
                        "restarts": runtime.restart_count,
                        "crashes": runtime.crash_count,
                        "detail": self._runtime_detail(runtime, now),
                    }
                )
            recent_logs = list(self._logs)[-30:]

        run_seconds = max(0.0, now - self._run_started_at)
        header = (
            "Service         Desired  State      PID      Uptime    Starts  Stops  Restarts  Crashes  Last Event / Error"
        )
        lines = []
        lines.append("=" * len(header))
        lines.append("Teleoperation Watchdog Exit Summary")
        lines.append(f"Ended: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Interrupted: {'yes' if interrupted else 'no'}")
        lines.append(f"Session runtime: {self._format_duration(run_seconds)}")
        lines.append("-" * len(header))
        lines.append(header)
        lines.append("-" * len(header))
        for row in rows:
            lines.append(
                f"{row['label'][:14]:14}  "
                f"{row['desired'][:3]:>3}      "
                f"{row['state'][:9]:9}  "
                f"{str(row['pid'])[:8]:8}  "
                f"{row['uptime']:8}  "
                f"{int(row['starts']):6}  "
                f"{int(row['stops']):5}  "
                f"{int(row['restarts']):8}  "
                f"{int(row['crashes']):7}  "
                f"{row['detail']}"
            )
        lines.append("-" * len(header))
        lines.append("Recent Watchdog Logs:")
        if recent_logs:
            lines.extend(recent_logs)
        else:
            lines.append("(no logs)")
        lines.append("=" * len(header))
        return "\n".join(lines)

    def _draw_ui(self, stdscr):
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        now = time.time()

        title = "Teleoperation Watchdog Manager"
        info = "Up/Down: select | Space: toggle | R: restart | A: toggle all | Q: quit"
        stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, info, width - 1)

        header = "Sel  Service         Desired  State      PID      Uptime    Last Event / Error"
        stdscr.addnstr(3, 0, header, width - 1, curses.A_UNDERLINE)

        row = 4
        for idx, svc in enumerate(self.services):
            runtime = self.runtime_by_id[svc.service_id]
            selected = ">" if idx == self._selected_index else " "
            desired = "ON " if runtime.desired_enabled else "OFF"
            pid_text = str(runtime.pid) if runtime.pid else "-"
            uptime = self._format_duration(now - runtime.started_at) if runtime.started_at > 0 else "-"
            state_text = runtime.state.upper()
            message = self._runtime_detail(runtime, now)
            line = (
                f"{selected:1}    "
                f"{svc.label[:14]:14}  "
                f"{desired:>3}      "
                f"{state_text[:9]:9}  "
                f"{pid_text[:8]:8}  "
                f"{uptime:8}  "
                f"{message}"
            )
            color = self._state_color(runtime.state)
            attrs = curses.color_pair(color)
            if idx == self._selected_index:
                attrs |= curses.A_REVERSE
            stdscr.addnstr(row, 0, line, width - 1, attrs)
            row += 1
            if row >= height - 6:
                break

        log_top = row + 1
        if log_top < height - 2:
            stdscr.addnstr(log_top, 0, "Logs", width - 1, curses.A_UNDERLINE)
            visible_log_rows = max(0, height - (log_top + 2))
            logs = list(self._logs)[-visible_log_rows:]
            for i, line in enumerate(logs):
                stdscr.addnstr(log_top + 1 + i, 0, line, width - 1)

        clock = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stdscr.addnstr(height - 1, max(0, width - len(clock) - 1), clock, len(clock))
        stdscr.refresh()

    def run_curses(self):
        self.start()
        interrupted = False

        def _inner(stdscr):
            curses.curs_set(0)
            stdscr.nodelay(True)
            stdscr.keypad(True)
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_MAGENTA, -1)
            curses.init_pair(5, curses.COLOR_RED, -1)

            while True:
                with self._lock:
                    self._draw_ui(stdscr)

                key = stdscr.getch()
                if key == -1:
                    time.sleep(0.06)
                    continue

                with self._lock:
                    if key in (ord("q"), ord("Q")):
                        self._log("[USER] Quit requested")
                        return
                    if key in (curses.KEY_UP, ord("k"), ord("K")):
                        self._selected_index = max(0, self._selected_index - 1)
                    elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                        self._selected_index = min(len(self.services) - 1, self._selected_index + 1)
                    elif key == ord(" "):
                        self.toggle_selected()
                    elif key in (ord("r"), ord("R")):
                        self.restart_selected()
                    elif key in (ord("a"), ord("A")):
                        self.toggle_all()

                time.sleep(0.02)

        try:
            curses.wrapper(_inner)
        except KeyboardInterrupt:
            interrupted = True
            with self._lock:
                self._log("[USER] Ctrl+C interrupt received")
        finally:
            self.request_shutdown()
            self.await_shutdown()
            self._release_instance_lock()
            report = self._build_exit_report(interrupted=interrupted)
            print("\n" + report, flush=True)


def main():
    try:
        manager = WatchdogManager(pathlib.Path(__file__).resolve().parent)
    except RuntimeError as exc:
        print(f"[WATCHDOG] {exc}", flush=True)
        return 1
    manager.run_curses()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
