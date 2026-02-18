"""
Pick a safe serial port for ESP32 uploads.

Why this exists:
- Windows can expose Bluetooth SPP ports as COM devices.
- PlatformIO auto-detection may choose a Bluetooth COM port instead of USB UART,
  which causes upload failures such as "Access is denied" or serial write timeout.
- On Linux/macOS, serial device paths are case-sensitive (e.g. /dev/ttyUSB0).

Behavior:
- Only runs for targets that need a serial port (upload/monitor/program).
- Prefers connected USB serial ports and ignores Bluetooth ports.
- Allows manual override with environment variables:
  - PIO_UPLOAD_PORT
  - ESP32_UPLOAD_PORT
"""

import os
import platform
import re
import subprocess

from SCons.Script import COMMAND_LINE_TARGETS, Exit

Import("env")


TARGETS_NEEDING_SERIAL = {
    "upload",
    "uploadfs",
    "program",
    "monitor",
}

# Common USB bridge vendors used by ESP32 boards (CP210x, CH340, FTDI, native USB CDC)
KNOWN_ESP32_USB_IDS = (
    "VID_10C4",  # Silicon Labs CP210x
    "VID_1A86",  # QinHeng CH340/CH9102
    "VID_0403",  # FTDI
    "VID_303A",  # Espressif native USB
)


def needs_serial_port():
    targets = {t.lower() for t in COMMAND_LINE_TARGETS}
    return bool(targets & TARGETS_NEEDING_SERIAL)


def _is_windows():
    return platform.system().lower() == "windows"


def _normalize_port_name(device):
    port = str(device or "").strip()
    if not port:
        return ""
    return port.upper() if _is_windows() else port


def _dedupe_key(device):
    port = str(device or "").strip()
    if _is_windows():
        return port.upper()
    return port


def is_bluetooth_port(instance_id, description):
    text = f"{instance_id} {description}".upper()
    return "BTHENUM\\" in text or "BLUETOOTH" in text


def is_usb_port(instance_id, description):
    text = f"{instance_id} {description}".upper()
    return "USB\\" in text or "USB " in text or "VID_" in text or "VID:PID" in text


def port_priority(instance_id, description):
    text = f"{instance_id} {description}".upper()
    if any(token in text for token in KNOWN_ESP32_USB_IDS):
        return 0
    return 1


def connected_ports_windows():
    try:
        result = subprocess.run(
            ["pnputil", "/enum-devices", "/connected", "/class", "Ports"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []

    if result.returncode != 0:
        return []

    ports = []
    current_instance = ""

    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("Instance ID:"):
            current_instance = line.split(":", 1)[1].strip()
            continue

        if line.startswith("Device Description:"):
            desc = line.split(":", 1)[1].strip()
            match = re.search(r"\((COM\d+)\)", desc, re.IGNORECASE)
            if match:
                ports.append(
                    {
                        "device": _normalize_port_name(match.group(1)),
                        "description": desc,
                        "instance_id": current_instance,
                    }
                )

    return ports


def connected_ports_pyserial():
    try:
        from serial.tools import list_ports
    except Exception:
        return []

    ports = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": _normalize_port_name(port.device),
                "description": port.description or "",
                "instance_id": port.hwid or "",
            }
        )
    return ports


def dedupe_by_port_prefer_usb(ports):
    selected = {}
    for item in ports:
        key = _dedupe_key(item.get("device", ""))
        if key not in selected:
            selected[key] = item
            continue

        current = selected[key]
        current_bt = is_bluetooth_port(current["instance_id"], current["description"])
        item_bt = is_bluetooth_port(item["instance_id"], item["description"])

        # Prefer a non-Bluetooth entry when duplicate COM mappings exist.
        if current_bt and not item_bt:
            selected[key] = item

    return list(selected.values())


def print_ports(ports):
    if not ports:
        print("No serial ports found.")
        return

    print("Detected serial ports:")
    for item in sorted(ports, key=lambda p: p["device"]):
        print(f"  - {item['device']}: {item['description']} [{item['instance_id']}]")


def choose_port():
    override = os.getenv("PIO_UPLOAD_PORT") or os.getenv("ESP32_UPLOAD_PORT")
    normalized_override = _normalize_port_name(override) if override else ""
    if normalized_override:
        if _is_windows():
            return normalized_override, "manual override"

        # On POSIX, allow automatic recovery if the manual path is stale.
        if os.path.exists(normalized_override):
            return normalized_override, "manual override"
        print(
            f"Manual upload port {normalized_override} does not exist; "
            "falling back to auto-detection."
        )

    ports = []
    if _is_windows():
        ports = connected_ports_windows()
    if not ports:
        ports = connected_ports_pyserial()

    ports = dedupe_by_port_prefer_usb(ports)
    usb_ports = [
        p
        for p in ports
        if is_usb_port(p["instance_id"], p["description"])
        and not is_bluetooth_port(p["instance_id"], p["description"])
    ]

    if not usb_ports:
        print_ports(ports)
        print(
            "No connected USB serial port is available for ESP32 upload. "
            "Connect the board via data USB and avoid Bluetooth COM ports."
        )
        Exit(1)

    usb_ports.sort(key=lambda p: (port_priority(p["instance_id"], p["description"]), p["device"]))
    chosen = usb_ports[0]
    return chosen["device"], chosen["description"]


if needs_serial_port():
    port, reason = choose_port()
    env.Replace(UPLOAD_PORT=port)
    env.Replace(MONITOR_PORT=port)
    print(f"Using serial port: {port} ({reason})")
