"""Custom Kitty tab bar with network status panel.

Displays battery, ping latency, and Tailscale status in the right side
of the tab bar. Tabs are rendered using Kitty's built-in powerline style.

See PLAN.md for full design details.
"""

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from kitty.boss import get_boss
from kitty.fast_data_types import Screen, add_timer, get_options
from kitty.rgb import to_color
from kitty.tab_bar import (
    DrawData,
    ExtraData,
    Formatter,
    TabBarData,
    as_rgb,
    draw_attributed_string,
    draw_tab_with_powerline,
)

# ============================================================================
# Configuration
# ============================================================================

PING_TARGETS = ["1.1.1.1", "8.8.8.8"]
PING_INTERVAL = 2.0  # seconds between pings
PING_TIMEOUT = 2  # seconds before ping gives up
TAILSCALE_TTL = 10.0  # seconds between tailscale checks
BATTERY_TTL = 30.0  # seconds between battery checks
TAB_BAR_REDRAW = 2.0  # seconds between tab bar redraws

# Colors (hex)
COLOR_GREEN = "#a6e3a1"
COLOR_YELLOW = "#f9e2af"
COLOR_ORANGE = "#fab387"
COLOR_RED = "#f38ba8"
COLOR_GRAY = "#6c7086"
COLOR_BLUE = "#89b4fa"
COLOR_TEXT = "#cdd6f4"

# ============================================================================
# Ping Monitor (pure Python ICMP — no subprocess, no dependencies)
#
# Why not shell out to `ping`? Three reasons:
#   1. Spawning 4 processes every 2s for the lifetime of the terminal is
#      wasteful — this runs in a background thread with zero process overhead.
#   2. Parsing `ping` stdout is fragile across OS versions.
#   3. Third-party libs (icmplib) can't be installed into Kitty's bundled
#      Python without breaking on Kitty updates.
#
# We use SOCK_DGRAM + IPPROTO_ICMP which works unprivileged on macOS
# (no root/setuid needed). We build ICMP echo request packets directly
# and measure RTT with time.monotonic(). See PLAN.md for full rationale.
# ============================================================================

_ping_results: dict[str, float | None] = {t: None for t in PING_TARGETS}
_ping_lock = threading.Lock()
_ping_thread_started = False
_ping_seq = 0


def _icmp_checksum(data: bytes) -> int:
    """Calculate ICMP checksum (RFC 1071)."""
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _ping_host(target: str, timeout: float = 2.0) -> float | None:
    """Send a single ICMP echo request and return RTT in ms, or None.

    Uses SOCK_DGRAM + IPPROTO_ICMP which works unprivileged on macOS.
    """
    global _ping_seq
    _ping_seq = (_ping_seq + 1) & 0xFFFF

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        sock.settimeout(timeout)
    except OSError:
        return None

    try:
        # Build ICMP echo request: type=8, code=0, checksum=0, id, seq
        icmp_id = os.getpid() & 0xFFFF
        header = struct.pack("!BBHHH", 8, 0, 0, icmp_id, _ping_seq)
        payload = b"kittyping" + struct.pack("!d", time.time())
        checksum = _icmp_checksum(header + payload)
        header = struct.pack("!BBHHH", 8, 0, checksum, icmp_id, _ping_seq)
        packet = header + payload

        start = time.monotonic()
        sock.sendto(packet, (target, 0))

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                return None
            sock.settimeout(timeout - elapsed)
            data, _ = sock.recvfrom(1024)
            # SOCK_DGRAM strips the IP header, so data starts at ICMP
            if len(data) >= 8:
                resp_type, resp_code, _, resp_id, resp_seq = struct.unpack(
                    "!BBHHH", data[:8]
                )
                # Type 0 = echo reply; match our id and seq
                if resp_type == 0 and resp_id == icmp_id and resp_seq == _ping_seq:
                    rtt_ms = (time.monotonic() - start) * 1000
                    return rtt_ms
            # Not our packet — keep waiting
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


def _ping_loop() -> None:
    """Background thread: continuously ping all targets."""
    while True:
        threads = []
        for target in PING_TARGETS:
            t = threading.Thread(target=_ping_one, args=(target,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=PING_TIMEOUT + 1)
        time.sleep(PING_INTERVAL)


def _ping_one(target: str) -> None:
    """Ping a single target and store the result."""
    rtt = _ping_host(target, timeout=PING_TIMEOUT)
    with _ping_lock:
        _ping_results[target] = rtt


def _get_best_ping() -> float | None:
    """Return the best (lowest) RTT across all targets, or None if all failed."""
    with _ping_lock:
        rtts = [r for r in _ping_results.values() if r is not None]
    return min(rtts) if rtts else None


def _start_ping_thread() -> None:
    """Start the background ping thread (once)."""
    global _ping_thread_started
    if not _ping_thread_started:
        _ping_thread_started = True
        t = threading.Thread(target=_ping_loop, daemon=True)
        t.start()


# ============================================================================
# Tailscale Monitor
# ============================================================================


@dataclass
class TailscaleState:
    """Parsed Tailscale status."""

    backend_state: str = "Unknown"
    tailnet_name: str = ""


_tailscale_cache: TailscaleState | None = None
_tailscale_last_check: float = 0.0
_tailscale_available: bool | None = None  # None = not yet checked


def _check_tailscale_available() -> bool:
    """Check if the tailscale binary exists."""
    return shutil.which("tailscale") is not None


def _fetch_tailscale_status() -> TailscaleState:
    """Run tailscale status --json and parse the result."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return TailscaleState(backend_state="Error")

        data = json.loads(result.stdout)
        state = data.get("BackendState", "Unknown")
        tailnet_name = ""

        if state == "Running":
            # Try CurrentTailnet.Name first, fall back to MagicDNSSuffix
            current_tailnet = data.get("CurrentTailnet", {})
            tailnet_name = current_tailnet.get("Name", "")
            if not tailnet_name:
                magic_dns = data.get("MagicDNSSuffix", "")
                if magic_dns.endswith(".ts.net"):
                    tailnet_name = magic_dns[: -len(".ts.net")]
                else:
                    tailnet_name = magic_dns

        return TailscaleState(backend_state=state, tailnet_name=tailnet_name)
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        return TailscaleState(backend_state="Error")


def _refresh_tailscale(timer_id: int) -> None:
    """Timer callback: refresh tailscale status in background."""
    global _tailscale_cache, _tailscale_last_check
    _tailscale_cache = _fetch_tailscale_status()
    _tailscale_last_check = time.time()


def _get_tailscale_state() -> TailscaleState | None:
    """Get current tailscale state, scheduling a refresh if stale."""
    global _tailscale_available, _tailscale_cache, _tailscale_last_check

    if _tailscale_available is None:
        _tailscale_available = _check_tailscale_available()
    if not _tailscale_available:
        return None

    now = time.time()
    if _tailscale_cache is None or (now - _tailscale_last_check) > TAILSCALE_TTL:
        add_timer(_refresh_tailscale, 0.1, False)
        if _tailscale_cache is None:
            _tailscale_cache = TailscaleState()
            _tailscale_last_check = now

    return _tailscale_cache


# ============================================================================
# Battery Monitor
# ============================================================================


@dataclass
class BatteryState:
    """Parsed battery status."""

    percent: int = -1
    charging: bool = False
    present: bool = False


_battery_cache: BatteryState | None = None
_battery_last_check: float = 0.0


def _fetch_battery_status() -> BatteryState:
    """Run pmset -g batt (macOS) and parse the result."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return BatteryState()

        output = result.stdout
        # Parse: "62%; discharging;" or "85%; charging;" or "100%; charged;"
        match = re.search(
            r"(\d+)%;\s*(charging|discharging|charged|finishing charge)", output
        )
        if not match:
            return BatteryState()

        percent = int(match.group(1))
        state = match.group(2)
        charging = state in ("charging", "charged", "finishing charge")
        return BatteryState(percent=percent, charging=charging, present=True)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return BatteryState()


def _refresh_battery(timer_id: int) -> None:
    """Timer callback: refresh battery status."""
    global _battery_cache, _battery_last_check
    _battery_cache = _fetch_battery_status()
    _battery_last_check = time.time()


def _get_battery_state() -> BatteryState | None:
    """Get current battery state, scheduling a refresh if stale."""
    global _battery_cache, _battery_last_check

    now = time.time()
    if _battery_cache is None or (now - _battery_last_check) > BATTERY_TTL:
        add_timer(_refresh_battery, 0.1, False)
        if _battery_cache is None:
            _battery_cache = _fetch_battery_status()
            _battery_last_check = now

    if _battery_cache and _battery_cache.present:
        return _battery_cache
    return None


# ============================================================================
# Cell Builders
# ============================================================================


def _build_ping_cell() -> dict | None:
    """Build the ping status cell."""
    rtt = _get_best_ping()
    if rtt is None:
        return {"icon": "󰤭 ", "color": COLOR_GRAY, "text": "offline"}
    elif rtt < 100:
        color = COLOR_GREEN
    elif rtt < 500:
        color = COLOR_YELLOW
    elif rtt < 2000:
        color = COLOR_ORANGE
    else:
        color = COLOR_RED

    if rtt < 1000:
        text = f"{rtt:.0f}ms"
    else:
        text = f"{rtt / 1000:.1f}s"

    return {"icon": "󰤨 ", "color": color, "text": text}


def _build_tailscale_cell() -> dict | None:
    """Build the Tailscale status cell."""
    state = _get_tailscale_state()
    if state is None:
        return None

    match state.backend_state:
        case "Running":
            name = state.tailnet_name or "connected"
            return {"icon": "󰒍 ", "color": COLOR_GREEN, "text": name}
        case "NeedsLogin":
            return {"icon": "󰒎 ", "color": COLOR_YELLOW, "text": "needs login"}
        case "Stopped":
            return {"icon": "󰒎 ", "color": COLOR_GRAY, "text": "stopped"}
        case "Starting":
            return {"icon": "󰒍 ", "color": COLOR_YELLOW, "text": "connecting..."}
        case _:
            return {"icon": "󰒎 ", "color": COLOR_RED, "text": "unknown"}


def _build_battery_cell() -> dict | None:
    """Build the battery status cell."""
    state = _get_battery_state()
    if state is None:
        return None

    pct = state.percent
    charging = state.charging

    if pct >= 80:
        icon = "󰂅 " if charging else "󰁹 "
        color = COLOR_GREEN
    elif pct >= 50:
        icon = "󰂉 " if charging else "󰂀 "
        color = COLOR_GREEN if charging else COLOR_YELLOW
    elif pct >= 20:
        icon = "󰂇 " if charging else "󰁾 "
        color = COLOR_YELLOW if charging else COLOR_ORANGE
    else:
        icon = "󰢜 " if charging else "󰁺 "
        color = COLOR_ORANGE if charging else COLOR_RED

    return {"icon": icon, "color": color, "text": f"{pct}%"}


def _build_cells() -> list[dict]:
    """Build all status cells. Order: battery, ping, tailscale."""
    cells = []
    for builder in (_build_battery_cell, _build_ping_cell, _build_tailscale_cell):
        cell = builder()
        if cell is not None:
            cells.append(cell)
    return cells


# ============================================================================
# Drawing
# ============================================================================


def _draw_right_status(draw_data: DrawData, screen: Screen, cells: list[dict]) -> None:
    """Draw right-aligned status cells on the tab bar."""
    if not cells:
        return

    draw_attributed_string(Formatter.reset, screen)
    default_bg = as_rgb(int(draw_data.default_bg))
    text_color = to_color(COLOR_TEXT)

    # Calculate total width needed
    total_width = sum(len(c["icon"]) + len(c["text"]) + 2 for c in cells)

    # Calculate padding to right-align
    padding = screen.columns - screen.cursor.x - total_width
    if padding < 0:
        # Not enough space — drop cells from the left until it fits
        while cells and padding < 0:
            dropped = cells.pop(0)
            padding += len(dropped["icon"]) + len(dropped["text"]) + 2
    if not cells:
        return

    if padding > 0:
        screen.cursor.bg = default_bg
        screen.draw(" " * padding)

    for cell in cells:
        icon_color = to_color(cell["color"])
        screen.cursor.bg = default_bg
        screen.cursor.fg = as_rgb(int(icon_color))
        screen.draw(cell["icon"])
        screen.cursor.fg = as_rgb(int(text_color))
        screen.draw(cell["text"])
        screen.draw("  ")


# ============================================================================
# Main Entry Point
# ============================================================================

_timer_id = None


def _redraw_tab_bar(timer_id: int) -> None:
    """Mark all tab bars as dirty, triggering a redraw."""
    for tm in get_boss().all_tab_managers:
        tm.mark_tab_bar_dirty()


def draw_tab(
    draw_data: DrawData,
    screen: Screen,
    tab: TabBarData,
    before: int,
    max_tab_length: int,
    index: int,
    is_last: bool,
    extra_data: ExtraData,
) -> int:
    """Main entry point — called by Kitty for each tab.

    Delegates tab rendering to powerline, then draws status cells on the
    last tab.
    """
    global _timer_id

    if _timer_id is None:
        _timer_id = add_timer(_redraw_tab_bar, TAB_BAR_REDRAW, True)
        _start_ping_thread()

    draw_tab_with_powerline(
        draw_data,
        screen,
        tab,
        before,
        max_tab_length,
        index,
        is_last,
        extra_data,
    )

    if is_last:
        cells = _build_cells()
        _draw_right_status(draw_data, screen, cells)

    return screen.cursor.x
