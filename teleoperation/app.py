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
import threading
import time
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


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    label: str
    script_relpath: str
    args: Tuple[str, ...] = ()
    auto_start: bool = True

    def script_path(self, base_dir: pathlib.Path) -> pathlib.Path:
        return (base_dir / self.script_relpath).resolve()

    def working_dir(self, base_dir: pathlib.Path) -> pathlib.Path:
        return self.script_path(base_dir).parent


@dataclass
class ServiceRuntime:
    desired_enabled: bool = False
    restart_after_stop: bool = False
    state: str = "stopped"  # stopped | starting | running | stopping | error | missing
    pid: Optional[int] = None
    terminal_process: Optional[subprocess.Popen] = None
    stop_stage: int = 0
    stop_requested_at: float = 0.0
    next_restart_at: float = 0.0
    restart_backoff_seconds: float = RESTART_BACKOFF_INITIAL_SECONDS
    started_at: float = 0.0
    stopped_at: float = 0.0
    last_event: str = ""
    last_error: str = ""
    start_count: int = 0
    stop_count: int = 0
    crash_count: int = 0
    restart_count: int = 0


class WatchdogManager:
    def __init__(self, base_dir: pathlib.Path):
        self.base_dir = base_dir
        self.runtime_dir = self.base_dir / WATCHDOG_RUNTIME_DIR_NAME
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.runtime_dir / WATCHDOG_STATE_FILE_NAME
        self.lock_file = self.runtime_dir / WATCHDOG_LOCK_FILE_NAME

        self.services: List[ServiceSpec] = [
            ServiceSpec("adapter", "Adapter", "adapter/adapter.py"),
            ServiceSpec("camera", "Camera Router", "vision/camera_route.py"),
            ServiceSpec("depth", "Depth", "depth/depth.py"),
            ServiceSpec("router", "NKN Router", "router/router.py"),
            ServiceSpec("frontend", "Frontend", "frontend/app.py"),
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
                    runtime.state = "running"
                    runtime.started_at = now
                elif pid:
                    self._remove_pid_file(svc)
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
        if state == "starting":
            return 3
        if state == "stopping":
            return 4
        if state in ("error", "missing"):
            return 5
        return 1

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
        existing_pid = None
        if self.lock_file.exists():
            try:
                existing_pid = int(self.lock_file.read_text(encoding="utf-8").strip())
            except Exception:
                existing_pid = None
        if existing_pid and existing_pid != os.getpid() and self._is_pid_running(existing_pid):
            raise RuntimeError(f"Another watchdog instance is already running (pid {existing_pid})")
        try:
            self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
            self._instance_lock_acquired = True
        except Exception as exc:
            raise RuntimeError(f"Failed to acquire watchdog lock: {exc}") from exc

    def _release_instance_lock(self):
        if not self._instance_lock_acquired:
            return
        try:
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
            runtime.state = "missing"
            runtime.last_error = f"Missing: {svc.script_relpath}"
            runtime.desired_enabled = False
            return

        if self._os_name == "windows":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
            try:
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                cmdline = subprocess.list2cmdline(
                    [sys.executable, str(script_path)] + list(svc.args)
                )
                proc = subprocess.Popen(
                    [comspec, "/c", cmdline],
                    cwd=str(svc.working_dir(self.base_dir)),
                    creationflags=creationflags,
                )
                runtime.terminal_process = proc
                runtime.pid = proc.pid
                runtime.state = "running"
                runtime.started_at = now
                runtime.start_count += 1
                runtime.stop_stage = 0
                runtime.stop_requested_at = 0.0
                runtime.last_error = ""
                runtime.last_event = f"Started (pid {proc.pid})"
                runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
                self._write_pid_file(svc, proc.pid)
                self._log(f"[START] {svc.label} pid={proc.pid}")
                return
            except Exception as exc:
                runtime.state = "error"
                runtime.last_error = str(exc)
                runtime.last_event = "Launch failed"
                runtime.next_restart_at = now + runtime.restart_backoff_seconds
                runtime.restart_backoff_seconds = min(
                    RESTART_BACKOFF_MAX_SECONDS, runtime.restart_backoff_seconds * 2.0
                )
                self._log(f"[ERROR] {svc.label} launch failed: {exc}")
                return

        if self._os_name == "darwin":
            wrapped = self._build_wrapped_shell_command(svc)
            escaped = wrapped.replace("\\", "\\\\").replace('"', '\\"')
            apple_script = f'tell application "Terminal" to do script "bash -lc \\"{escaped}\\""'
            try:
                proc = subprocess.Popen(["osascript", "-e", apple_script])
                runtime.terminal_process = proc
                runtime.state = "starting"
                runtime.started_at = 0.0
                runtime.stop_stage = 0
                runtime.stop_requested_at = 0.0
                runtime.last_error = ""
                runtime.last_event = "Launching terminal"
                self._log(f"[START] {svc.label} launching in Terminal.app")
                return
            except Exception as exc:
                runtime.state = "error"
                runtime.last_error = str(exc)
                runtime.last_event = "Launch failed"
                runtime.next_restart_at = now + runtime.restart_backoff_seconds
                runtime.restart_backoff_seconds = min(
                    RESTART_BACKOFF_MAX_SECONDS, runtime.restart_backoff_seconds * 2.0
                )
                self._log(f"[ERROR] {svc.label} launch failed: {exc}")
                return

        wrapped = self._build_wrapped_shell_command(svc)
        terminal_cmd = self._build_terminal_command(f"Teleop - {svc.label}", wrapped)
        if not terminal_cmd:
            runtime.state = "error"
            runtime.last_error = "No terminal emulator found"
            runtime.last_event = "Launch failed"
            runtime.desired_enabled = False
            self._log(f"[ERROR] {svc.label}: no terminal emulator found")
            return

        try:
            proc = subprocess.Popen(terminal_cmd, cwd=str(self.base_dir))
            runtime.terminal_process = proc
            runtime.state = "starting"
            runtime.started_at = 0.0
            runtime.stop_stage = 0
            runtime.stop_requested_at = 0.0
            runtime.last_error = ""
            runtime.last_event = "Launching terminal"
            self._log(f"[START] {svc.label} launching in {self._terminal_emulator}")
        except Exception as exc:
            runtime.state = "error"
            runtime.last_error = str(exc)
            runtime.last_event = "Launch failed"
            runtime.next_restart_at = now + runtime.restart_backoff_seconds
            runtime.restart_backoff_seconds = min(
                RESTART_BACKOFF_MAX_SECONDS, runtime.restart_backoff_seconds * 2.0
            )
            self._log(f"[ERROR] {svc.label} launch failed: {exc}")

    # ------------------------------------------------------------------
    # Runtime synchronization
    # ------------------------------------------------------------------
    def _refresh_pid_state(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        if runtime.pid and not self._is_pid_running(runtime.pid):
            runtime.pid = None

        if runtime.pid is None:
            pid_from_file = self._read_pid_file(svc)
            if pid_from_file and self._is_pid_running(pid_from_file):
                runtime.pid = pid_from_file
                if runtime.started_at <= 0:
                    runtime.started_at = now
                if runtime.state != "running":
                    runtime.state = "running"
                    runtime.start_count += 1
                    runtime.last_event = f"Running (pid {pid_from_file})"
                    runtime.last_error = ""
                    runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
                    self._log(f"[RUNNING] {svc.label} pid={pid_from_file}")
            elif pid_from_file:
                self._remove_pid_file(svc)

    def _begin_stop(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        runtime.state = "stopping"
        runtime.stop_count += 1
        runtime.stop_stage = 1
        runtime.stop_requested_at = now
        runtime.last_event = "Stopping (SIGINT)"
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
        if not runtime.pid or not self._is_pid_running(runtime.pid):
            runtime.state = "stopped"
            runtime.pid = None
            runtime.stop_stage = 0
            runtime.stop_requested_at = 0.0
            runtime.started_at = 0.0
            runtime.stopped_at = now
            runtime.last_error = ""
            runtime.last_event = "Stopped"
            runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
            self._remove_pid_file(svc)
            self._log(f"[STOPPED] {svc.label}")
            return

        elapsed = now - runtime.stop_requested_at
        if runtime.stop_stage == 1 and elapsed >= STOP_SIGINT_GRACE_SECONDS:
            runtime.stop_stage = 2
            runtime.last_event = "Stopping (SIGTERM)"
            self._send_signal(runtime.pid, signal.SIGTERM)
            self._log(f"[STOP] {svc.label} SIGTERM pid={runtime.pid}")
            return

        if runtime.stop_stage == 2 and elapsed >= STOP_SIGTERM_GRACE_SECONDS:
            runtime.stop_stage = 3
            runtime.last_event = "Stopping (SIGKILL)"
            self._send_signal(runtime.pid, signal.SIGKILL)
            self._log(f"[STOP] {svc.label} SIGKILL pid={runtime.pid}")
            return

        if runtime.stop_stage == 3 and elapsed >= STOP_SIGKILL_GRACE_SECONDS:
            runtime.state = "error"
            runtime.last_error = "Could not stop process"
            runtime.last_event = "Stop escalation failed"

    def _sync_single_service(self, svc: ServiceSpec, runtime: ServiceRuntime, now: float):
        if runtime.state == "missing":
            return

        self._refresh_pid_state(svc, runtime, now)
        running = bool(runtime.pid and self._is_pid_running(runtime.pid))

        if not running and runtime.state == "running":
            runtime.pid = None
            runtime.started_at = 0.0
            runtime.stopped_at = now
            self._remove_pid_file(svc)
            if runtime.desired_enabled:
                runtime.state = "error"
                runtime.crash_count += 1
                runtime.last_error = "Exited unexpectedly"
                runtime.last_event = "Waiting to restart"
                runtime.next_restart_at = now + runtime.restart_backoff_seconds
                runtime.restart_backoff_seconds = min(
                    RESTART_BACKOFF_MAX_SECONDS, runtime.restart_backoff_seconds * 2.0
                )
                self._log(f"[WARN] {svc.label} exited unexpectedly; restarting soon")
            else:
                runtime.state = "stopped"
                runtime.last_error = ""
                runtime.last_event = "Stopped"
                runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS

        if not runtime.desired_enabled:
            if running and runtime.state != "stopping":
                self._begin_stop(svc, runtime, now)
            self._handle_stop_escalation(svc, runtime, now)
            if not running and runtime.restart_after_stop:
                runtime.restart_after_stop = False
                runtime.desired_enabled = True
                runtime.next_restart_at = now
                runtime.last_event = "Restart queued"
                self._save_desired_state()
            return

        if runtime.state == "stopping":
            # Desired on while stopping means a restart request.
            runtime.restart_after_stop = True
            self._handle_stop_escalation(svc, runtime, now)
            return

        if running:
            runtime.state = "running"
            runtime.last_error = ""
            runtime.next_restart_at = 0.0
            runtime.restart_backoff_seconds = RESTART_BACKOFF_INITIAL_SECONDS
            return

        if runtime.state == "starting":
            if runtime.terminal_process and runtime.terminal_process.poll() is not None:
                code = runtime.terminal_process.returncode
                runtime.state = "error"
                runtime.last_error = f"Terminal exited ({code})"
                runtime.last_event = "Launch failed"
                runtime.next_restart_at = now + runtime.restart_backoff_seconds
                runtime.restart_backoff_seconds = min(
                    RESTART_BACKOFF_MAX_SECONDS, runtime.restart_backoff_seconds * 2.0
                )
                self._log(f"[ERROR] {svc.label} terminal exited ({code})")
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

    def _build_exit_report(self, interrupted: bool = False) -> str:
        now = time.time()
        with self._lock:
            rows = []
            for svc in self.services:
                runtime = self.runtime_by_id[svc.service_id]
                if runtime.stopped_at > runtime.started_at > 0 and runtime.state != "running":
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
                        "detail": runtime.last_error if runtime.last_error else runtime.last_event,
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
            message = runtime.last_error if runtime.last_error else runtime.last_event
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
