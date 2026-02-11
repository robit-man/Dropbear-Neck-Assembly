#!/usr/bin/env python
"""
Curses-based terminal UI for Neck project.

Provides a full-featured dashboard with:
  - Status/metrics panel
  - Editable config settings with nested navigation
  - Live scrolling log
  - Type-aware editors (bool, int, float, string, secret)
  - Pending changes with save/discard
  - Vim-style navigation (j/k or arrow keys)
"""

import curses
import base64
import threading
import time
import json
import os
import shutil
import subprocess
import sys
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SettingSpec:
    """Specification for a single editable setting."""
    id: str
    label: str
    path: str                      # dot-notation path in config.json
    value_type: str = "str"        # bool, int, float, str, secret, enum
    description: str = ""
    default: Any = None
    choices: tuple = ()            # for enum type
    sensitive: bool = False
    restart_required: bool = False
    min_value: Any = None
    max_value: Any = None


@dataclass(frozen=True)
class CategorySpec:
    """A group of related settings."""
    id: str
    label: str
    settings: tuple


@dataclass(frozen=True)
class ConfigSpec:
    """Full config specification for a script."""
    label: str
    categories: tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_addstr(stdscr, row, col, text, attr=0):
    """Safely add a string to the screen, clipping to terminal bounds."""
    try:
        max_y, max_x = stdscr.getmaxyx()
        if row < 0 or row >= max_y or col >= max_x:
            return
        available = max_x - col - 1
        if available <= 0:
            return
        clipped = str(text)[:available]
        stdscr.addstr(row, col, clipped, attr)
    except curses.error:
        pass


def _mask_secret(value):
    """Mask a secret value for display."""
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _get_nested(d, path, default=None):
    """Get a value from a nested dict using dot-notation path."""
    keys = path.split(".")
    current = d
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return default
    return current


def _set_nested(d, path, value):
    """Set a value in a nested dict using dot-notation path."""
    keys = path.split(".")
    current = d
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


def _coerce_value(raw, spec):
    """Coerce a raw string to the correct type for a setting spec."""
    vtype = spec.value_type
    if vtype == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("true", "yes", "1", "on")
    elif vtype == "int":
        v = int(raw)
        if spec.min_value is not None and v < spec.min_value:
            raise ValueError(f"Minimum is {spec.min_value}")
        if spec.max_value is not None and v > spec.max_value:
            raise ValueError(f"Maximum is {spec.max_value}")
        return v
    elif vtype == "float":
        v = float(raw)
        if spec.min_value is not None and v < spec.min_value:
            raise ValueError(f"Minimum is {spec.min_value}")
        if spec.max_value is not None and v > spec.max_value:
            raise ValueError(f"Maximum is {spec.max_value}")
        return v
    elif vtype == "enum":
        if raw not in spec.choices:
            raise ValueError(f"Must be one of: {', '.join(spec.choices)}")
        return raw
    elif vtype == "secret":
        return str(raw)
    else:
        return str(raw)


# ---------------------------------------------------------------------------
# Terminal UI
# ---------------------------------------------------------------------------

class TerminalUI:
    """Full-featured curses dashboard."""

    def __init__(self, title, config_spec=None, config_path="config.json", refresh_interval_ms=200):
        self.title = title
        self.config_spec = config_spec
        self.config_path = config_path
        self.log_buffer = deque(maxlen=1000)
        self.metrics = OrderedDict()
        self.running = False
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.pending_changes = {}    # path → new_value
        self.status_message = ""
        self.status_time = 0
        self._on_save_callback = None
        self._on_restart_callback = None
        self._click_regions = []
        try:
            self.refresh_interval_ms = max(50, int(refresh_interval_ms))
        except (TypeError, ValueError):
            self.refresh_interval_ms = 200

    # -- Public API --

    def log(self, message):
        """Thread-safe log append."""
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log_buffer.append(f"[{ts}] {message}")

    def update_metric(self, key, value):
        """Thread-safe metric update."""
        with self.lock:
            self.metrics[key] = value

    def on_save(self, callback):
        """Register a callback for when config is saved. callback(config_dict)."""
        self._on_save_callback = callback

    def on_restart(self, callback):
        """Register a callback for restart requests."""
        self._on_restart_callback = callback

    def get_uptime(self):
        elapsed = int(time.time() - self.start_time)
        return str(timedelta(seconds=elapsed))

    def set_status(self, msg):
        self.status_message = msg
        self.status_time = time.time()

    def _register_click_region(self, row, col_start, col_end, action, payload):
        self._click_regions.append(
            {
                "row": int(row),
                "col_start": int(min(col_start, col_end)),
                "col_end": int(max(col_start, col_end)),
                "action": str(action),
                "payload": payload,
            }
        )

    def _copy_to_clipboard(self, payload):
        text = str(payload or "").strip()
        if not text or text == "N/A":
            return False, "value is empty"

        commands = []
        if os.name == "nt":
            clip_bin = shutil.which("clip.exe") or shutil.which("clip")
            if clip_bin:
                commands.append(([clip_bin], "clip.exe"))
            powershell_bin = shutil.which("powershell.exe") or shutil.which("powershell")
            if powershell_bin:
                commands.append(
                    (
                        [powershell_bin, "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                        "powershell",
                    )
                )
        else:
            wl_copy = shutil.which("wl-copy")
            if wl_copy:
                commands.append(([wl_copy], "wl-copy"))
            xclip = shutil.which("xclip")
            if xclip:
                commands.append(([xclip, "-selection", "clipboard"], "xclip"))
            xsel = shutil.which("xsel")
            if xsel:
                commands.append(([xsel, "--clipboard", "--input"], "xsel"))
            pbcopy = shutil.which("pbcopy")
            if pbcopy:
                commands.append(([pbcopy], "pbcopy"))

        for command, label in commands:
            try:
                subprocess.run(command, input=text, text=True, capture_output=True, check=True)
                return True, label
            except Exception:
                continue

        # Last fallback for terminals that support OSC52 clipboard control.
        try:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            sys.__stdout__.write(f"\033]52;c;{encoded}\a")
            sys.__stdout__.flush()
            return True, "OSC52"
        except Exception as exc:
            return False, str(exc) or "no clipboard backend available"

    def _copy_metric_value(self, metric_name):
        with self.lock:
            value = str(self.metrics.get(metric_name, "")).strip()
        ok, method = self._copy_to_clipboard(value)
        if ok:
            self.set_status(f"Copied {metric_name} via {method}")
        else:
            self.set_status(f"Copy failed: {method}")

    def _handle_mouse_event(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return False

        click_mask = (
            getattr(curses, "BUTTON1_CLICKED", 0)
            | getattr(curses, "BUTTON1_RELEASED", 0)
            | getattr(curses, "BUTTON1_PRESSED", 0)
            | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
        )
        if click_mask and not (bstate & click_mask):
            return False

        for region in reversed(self._click_regions):
            if my != region["row"]:
                continue
            if not (region["col_start"] <= mx <= region["col_end"]):
                continue
            if region["action"] == "copy_metric":
                self._copy_metric_value(region["payload"])
                return True
        return False

    def start(self):
        """Start the curses UI (blocking)."""
        try:
            curses.wrapper(self._main_loop)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False

    def stop(self):
        self.running = False

    # -- Config helpers --

    def _load_config(self):
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except:
            return {}

    def _save_config(self, cfg):
        try:
            with open(self.config_path, "w") as f:
                json.dump(cfg, f, indent=4)
            return True
        except Exception as e:
            self.set_status(f"Save failed: {e}")
            return False

    def _resolve_value(self, spec, config=None):
        """Resolve current value: pending → config → default."""
        if spec.path in self.pending_changes:
            return self.pending_changes[spec.path], "pending"
        if config is None:
            config = self._load_config()
        val = _get_nested(config, spec.path)
        if val is not None:
            return val, "config"
        if spec.default is not None:
            return spec.default, "default"
        return "", "unset"

    def _format_value(self, value, spec):
        """Format a value for display."""
        if spec.sensitive or spec.value_type == "secret":
            return _mask_secret(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        if value == "" or value is None:
            return "(unset)"
        return str(value)

    # -- Curses primitives --

    def _draw_hline(self, stdscr, row, col, length, char="─"):
        for i in range(length):
            _safe_addstr(stdscr, row, col + i, char)

    def _prompt_text(self, stdscr, prompt, initial="", body_lines=None):
        """Text input prompt with optional body text above."""
        max_y, max_x = stdscr.getmaxyx()
        stdscr.clear()

        row = 1
        _safe_addstr(stdscr, row, 2, prompt, curses.A_BOLD)
        row += 1

        if body_lines:
            for line in body_lines:
                row += 1
                _safe_addstr(stdscr, row, 4, line, curses.A_DIM)

        row += 2
        _safe_addstr(stdscr, row, 2, "> ")

        curses.echo()
        curses.curs_set(1)
        stdscr.move(row, 4)

        # Pre-fill
        buf = list(initial)
        _safe_addstr(stdscr, row, 4, initial)
        stdscr.move(row, 4 + len(buf))
        stdscr.refresh()

        stdscr.timeout(-1)  # blocking for text input
        while True:
            ch = stdscr.getch()
            if ch in (curses.KEY_ENTER, 10, 13):
                break
            elif ch == 27:  # Esc
                curses.noecho()
                curses.curs_set(0)
                stdscr.timeout(self.refresh_interval_ms)
                return None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                    _safe_addstr(stdscr, row, 4, " " * (max_x - 6))
                    _safe_addstr(stdscr, row, 4, "".join(buf))
                    stdscr.move(row, 4 + len(buf))
            elif 32 <= ch < 127:
                buf.append(chr(ch))
                _safe_addstr(stdscr, row, 4, "".join(buf))
                stdscr.move(row, 4 + len(buf))
            stdscr.refresh()

        curses.noecho()
        curses.curs_set(0)
        stdscr.timeout(self.refresh_interval_ms)
        return "".join(buf)

    def _prompt_bool(self, stdscr, label, current):
        """Toggle a boolean value."""
        return not current

    def _select_option(self, stdscr, title, options, current_idx=0):
        """Let user pick from a list of options. Returns index or -1."""
        sel = max(0, min(current_idx, len(options) - 1))
        stdscr.timeout(self.refresh_interval_ms)

        while True:
            stdscr.clear()
            _safe_addstr(stdscr, 1, 2, title, curses.A_BOLD)
            _safe_addstr(stdscr, 2, 2, "↑/↓ select  Enter confirm  Esc cancel", curses.A_DIM)

            for i, opt in enumerate(options):
                row = 4 + i
                attr = curses.A_REVERSE if i == sel else 0
                _safe_addstr(stdscr, row, 4, f"  {opt}  ", attr)

            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord('k')):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord('j')):
                sel = min(len(options) - 1, sel + 1)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return sel
            elif ch == 27:
                return -1

    # -- Screen sections --

    def _draw_header(self, stdscr, max_x):
        """Draw title bar."""
        title_text = f" {self.title} "
        pad = max_x - 2
        bar = "═" * ((pad - len(title_text)) // 2)
        full = f"╔{bar}{title_text}{bar}{'═' if len(title_text) % 2 else ''}╗"
        _safe_addstr(stdscr, 0, 0, full[:max_x], curses.A_BOLD | curses.color_pair(1))

    def _draw_metrics(self, stdscr, start_row, max_x):
        """Draw metrics section. Returns next row."""
        with self.lock:
            items = list(self.metrics.items())

        row = start_row

        # Two-column layout
        col1_w = max_x // 2 - 2
        for i in range(0, len(items), 2):
            key1, val1 = items[i]
            label1 = f"  {key1}:"
            val1_str = f" {val1}"
            _safe_addstr(stdscr, row, 1, label1, curses.A_BOLD)
            val1_col = 1 + len(label1)
            val1_attr = 0
            if key1 == "Address" and str(val1).strip() and str(val1).strip() != "N/A":
                val1_attr = curses.color_pair(4) | curses.A_UNDERLINE
                self._register_click_region(row, 1, val1_col + len(val1_str) - 1, "copy_metric", "Address")
            _safe_addstr(stdscr, row, val1_col, val1_str, val1_attr)

            if i + 1 < len(items):
                key2, val2 = items[i + 1]
                label2 = f"{key2}:"
                val2_str = f" {val2}"
                _safe_addstr(stdscr, row, col1_w + 2, label2, curses.A_BOLD)
                val2_col = col1_w + 2 + len(label2)
                val2_attr = 0
                if key2 == "Address" and str(val2).strip() and str(val2).strip() != "N/A":
                    val2_attr = curses.color_pair(4) | curses.A_UNDERLINE
                    self._register_click_region(row, col1_w + 2, val2_col + len(val2_str) - 1, "copy_metric", "Address")
                _safe_addstr(stdscr, row, val2_col, val2_str, val2_attr)

            row += 1

        # Uptime
        _safe_addstr(stdscr, row, 1, "  Uptime:", curses.A_BOLD)
        _safe_addstr(stdscr, row, 11, f" {self.get_uptime()}")
        row += 1

        return row

    def _draw_config_panel(self, stdscr, start_row, max_x, config, selected_cat, selected_setting):
        """Draw the config editing panel. Returns next row."""
        if not self.config_spec:
            return start_row

        row = start_row
        cats = self.config_spec.categories

        pending_count = len(self.pending_changes)
        header = f"─ Configuration "
        if pending_count:
            header += f"({pending_count} pending) "
        header += "─" * max(0, max_x - len(header) - 4)
        _safe_addstr(stdscr, row, 1, header, curses.A_BOLD | curses.color_pair(1))
        row += 1

        # Controls line
        controls = "↑/↓ navigate  Enter edit  c copy address  s save  d discard  r reset  q quit"
        _safe_addstr(stdscr, row, 2, controls, curses.A_DIM)
        row += 1

        # Category tabs
        tab_col = 2
        for ci, cat in enumerate(cats):
            label = f" {cat.label} "
            if ci == selected_cat:
                _safe_addstr(stdscr, row, tab_col, label, curses.A_REVERSE | curses.A_BOLD)
            else:
                _safe_addstr(stdscr, row, tab_col, label)
            tab_col += len(label) + 1
        row += 1

        # Separator
        self._draw_hline(stdscr, row, 1, max_x - 2)
        row += 1

        # Settings for selected category
        if selected_cat < len(cats):
            cat = cats[selected_cat]
            # Column headers
            _safe_addstr(stdscr, row, 3, "Setting", curses.A_DIM)
            _safe_addstr(stdscr, row, 28, "Value", curses.A_DIM)
            _safe_addstr(stdscr, row, 56, "Source", curses.A_DIM)
            _safe_addstr(stdscr, row, 68, "Type", curses.A_DIM)
            row += 1

            for si, spec in enumerate(cat.settings):
                val, source = self._resolve_value(spec, config)
                display_val = self._format_value(val, spec)

                is_selected = si == selected_setting
                is_pending = spec.path in self.pending_changes

                marker = "*" if is_pending else " "
                attr = curses.A_REVERSE if is_selected else 0

                # Row background
                _safe_addstr(stdscr, row, 1, " " * (max_x - 2), attr)
                _safe_addstr(stdscr, row, 1, f" {marker}", curses.color_pair(2) | attr if is_pending else attr)
                _safe_addstr(stdscr, row, 3, spec.label[:24], attr | curses.A_BOLD)

                # Value - color by source
                val_attr = attr
                if source == "pending":
                    val_attr |= curses.color_pair(2)
                elif source == "default":
                    val_attr |= curses.A_DIM
                _safe_addstr(stdscr, row, 28, display_val[:26], val_attr)
                _safe_addstr(stdscr, row, 56, source[:10], attr | curses.A_DIM)
                _safe_addstr(stdscr, row, 68, spec.value_type[:8], attr | curses.A_DIM)
                row += 1

            # Description of selected setting
            if selected_setting < len(cat.settings):
                spec = cat.settings[selected_setting]
                row += 1
                _safe_addstr(stdscr, row, 3, spec.description[:max_x - 6], curses.A_DIM)
                if spec.restart_required:
                    row += 1
                    _safe_addstr(stdscr, row, 3, "(requires restart)", curses.color_pair(3))
                row += 1

        return row

    def _draw_log(self, stdscr, start_row, max_y, max_x):
        """Draw the log panel at bottom."""
        row = start_row
        header = "─ Log " + "─" * max(0, max_x - 8)
        _safe_addstr(stdscr, row, 1, header, curses.A_BOLD | curses.color_pair(1))
        row += 1

        with self.lock:
            entries = list(self.log_buffer)

        avail = max_y - row - 2
        display = entries[-avail:] if len(entries) > avail else entries

        for entry in display:
            if row >= max_y - 1:
                break
            _safe_addstr(stdscr, row, 2, entry[:max_x - 4])
            row += 1

    def _draw_footer(self, stdscr, max_y, max_x):
        """Draw status bar at very bottom."""
        # Status message (fades after 5 seconds)
        msg = ""
        if self.status_message and time.time() - self.status_time < 5:
            msg = self.status_message
        else:
            msg = "Click Address or press c to copy  |  Ctrl+C to exit"

        bar = " " * (max_x - 1)
        _safe_addstr(stdscr, max_y - 1, 0, bar, curses.A_REVERSE)
        _safe_addstr(stdscr, max_y - 1, 1, msg[:max_x - 2], curses.A_REVERSE)

    # -- Editing --

    def _edit_setting(self, stdscr, spec, current_value):
        """Edit a single setting. Returns new value or None if cancelled."""
        vtype = spec.value_type

        if vtype == "bool":
            return self._prompt_bool(stdscr, spec.label, current_value)

        elif vtype == "enum":
            choices = list(spec.choices)
            try:
                cur_idx = choices.index(str(current_value))
            except ValueError:
                cur_idx = 0
            idx = self._select_option(stdscr, f"Select {spec.label}", choices, cur_idx)
            if idx >= 0:
                return choices[idx]
            return None

        elif vtype == "int":
            body = [spec.description]
            if spec.min_value is not None or spec.max_value is not None:
                body.append(f"Range: {spec.min_value or '...'} to {spec.max_value or '...'}")
            result = self._prompt_text(stdscr, f"Enter {spec.label} (integer):",
                                       str(current_value), body)
            if result is not None:
                try:
                    return _coerce_value(result, spec)
                except ValueError as e:
                    self.set_status(f"Invalid: {e}")
            return None

        elif vtype == "float":
            body = [spec.description]
            if spec.min_value is not None or spec.max_value is not None:
                body.append(f"Range: {spec.min_value or '...'} to {spec.max_value or '...'}")
            result = self._prompt_text(stdscr, f"Enter {spec.label} (number):",
                                       str(current_value), body)
            if result is not None:
                try:
                    return _coerce_value(result, spec)
                except ValueError as e:
                    self.set_status(f"Invalid: {e}")
            return None

        elif vtype == "secret":
            result = self._prompt_text(stdscr, f"Enter {spec.label} (hidden on save):",
                                       str(current_value), [spec.description])
            return result

        else:  # str
            result = self._prompt_text(stdscr, f"Enter {spec.label}:",
                                       str(current_value), [spec.description])
            return result

    def _do_save(self, stdscr):
        """Save all pending changes to config.json."""
        if not self.pending_changes:
            self.set_status("No pending changes")
            return

        config = self._load_config()
        needs_restart = False

        # Apply all pending changes
        for path, value in self.pending_changes.items():
            _set_nested(config, path, value)
            # Check if any need restart
            if self.config_spec:
                for cat in self.config_spec.categories:
                    for spec in cat.settings:
                        if spec.path == path and spec.restart_required:
                            needs_restart = True

        if self._save_config(config):
            count = len(self.pending_changes)
            self.pending_changes.clear()
            self.log(f"Saved {count} change(s) to {self.config_path}")
            self.set_status(f"Saved {count} change(s)")

            # Notify callback
            if self._on_save_callback:
                try:
                    self._on_save_callback(config)
                except Exception as e:
                    self.log(f"Save callback error: {e}")

            if needs_restart:
                self.set_status("Saved - restart required for some changes")
                self.log("Some changes require a restart to take effect")

    # -- Main loop --

    def _main_loop(self, stdscr):
        """Main curses event loop."""
        self.running = True

        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(self.refresh_interval_ms)

        # Colors
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)     # headers/accent
        curses.init_pair(2, curses.COLOR_GREEN, -1)       # pending/good
        curses.init_pair(3, curses.COLOR_RED, -1)         # warnings
        curses.init_pair(4, curses.COLOR_CYAN, -1)        # info
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass

        selected_cat = 0
        selected_setting = 0

        while self.running:
            try:
                max_y, max_x = stdscr.getmaxyx()
                if max_y < 12 or max_x < 40:
                    stdscr.clear()
                    _safe_addstr(stdscr, 0, 0, "Terminal too small")
                    stdscr.refresh()
                    time.sleep(0.5)
                    continue

                stdscr.clear()
                self._click_regions = []

                config = self._load_config()

                # Ensure selected indices are valid
                if self.config_spec:
                    cats = self.config_spec.categories
                    selected_cat = min(selected_cat, len(cats) - 1)
                    if selected_cat >= 0 and selected_cat < len(cats):
                        n_settings = len(cats[selected_cat].settings)
                        selected_setting = min(selected_setting, max(0, n_settings - 1))

                # Draw sections
                self._draw_header(stdscr, max_x)
                row = self._draw_metrics(stdscr, 2, max_x)
                row += 1

                if self.config_spec:
                    config_end = self._draw_config_panel(
                        stdscr, row, max_x, config,
                        selected_cat, selected_setting
                    )
                    row = config_end + 1

                self._draw_log(stdscr, row, max_y, max_x)
                self._draw_footer(stdscr, max_y, max_x)

                stdscr.refresh()

                # Handle input
                ch = stdscr.getch()
                if ch == -1:
                    continue

                if ch == curses.KEY_MOUSE:
                    self._handle_mouse_event()
                    continue

                if ch in (ord('q'), ord('Q')):
                    if self.pending_changes:
                        self.set_status("Unsaved changes! Press 'd' to discard, 's' to save, or 'q' again")
                        stdscr.refresh()
                        ch2 = stdscr.getch()
                        if ch2 in (ord('q'), ord('Q')):
                            self.pending_changes.clear()
                            self.running = False
                        elif ch2 == ord('s'):
                            self._do_save(stdscr)
                        elif ch2 == ord('d'):
                            self.pending_changes.clear()
                            self.set_status("Changes discarded")
                            self.running = False
                    else:
                        self.running = False
                    continue

                # Navigation
                if ch in (curses.KEY_UP, ord('k')):
                    selected_setting = max(0, selected_setting - 1)
                elif ch in (curses.KEY_DOWN, ord('j')):
                    if self.config_spec and selected_cat < len(self.config_spec.categories):
                        n = len(self.config_spec.categories[selected_cat].settings)
                        selected_setting = min(n - 1, selected_setting + 1)
                elif ch in (curses.KEY_LEFT, ord('h')):
                    if self.config_spec:
                        selected_cat = max(0, selected_cat - 1)
                        selected_setting = 0
                elif ch in (curses.KEY_RIGHT, ord('l')):
                    if self.config_spec:
                        selected_cat = min(len(self.config_spec.categories) - 1, selected_cat + 1)
                        selected_setting = 0
                elif ch == 9:  # Tab
                    if self.config_spec:
                        selected_cat = (selected_cat + 1) % len(self.config_spec.categories)
                        selected_setting = 0

                # Copy current Address metric value
                elif ch in (ord('c'), ord('C')):
                    self._copy_metric_value("Address")

                # Edit
                elif ch in (curses.KEY_ENTER, 10, 13):
                    if self.config_spec and selected_cat < len(self.config_spec.categories):
                        cat = self.config_spec.categories[selected_cat]
                        if selected_setting < len(cat.settings):
                            spec = cat.settings[selected_setting]
                            current, _ = self._resolve_value(spec, config)
                            new_val = self._edit_setting(stdscr, spec, current)
                            if new_val is not None:
                                self.pending_changes[spec.path] = new_val
                                self.set_status(f"Changed {spec.label}")

                # Save
                elif ch == ord('s'):
                    self._do_save(stdscr)

                # Discard
                elif ch == ord('d'):
                    if self.pending_changes:
                        self.pending_changes.clear()
                        self.set_status("All pending changes discarded")
                    else:
                        self.set_status("No pending changes")

                # Reset to default
                elif ch == ord('r'):
                    if self.config_spec and selected_cat < len(self.config_spec.categories):
                        cat = self.config_spec.categories[selected_cat]
                        if selected_setting < len(cat.settings):
                            spec = cat.settings[selected_setting]
                            if spec.default is not None:
                                self.pending_changes[spec.path] = spec.default
                                self.set_status(f"Reset {spec.label} to default")
                            else:
                                self.set_status(f"No default for {spec.label}")

            except curses.error:
                pass
            except KeyboardInterrupt:
                self.running = False
                break
