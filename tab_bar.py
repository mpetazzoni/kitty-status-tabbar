"""Custom Kitty tab bar with network status panel.

Displays battery, ping latency, and Tailscale status in the right side
of the tab bar. Tabs are rendered using Kitty's built-in powerline style.

See PLAN.md for full design details.
"""

import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple

from kitty.boss import get_boss
from kitty.fast_data_types import Screen, add_timer
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
PING_TIMEOUT = 2.0  # seconds before ping gives up
TAILSCALE_TTL = 10.0  # seconds between tailscale checks
BATTERY_TTL = 30.0  # seconds between battery checks
TAB_BAR_REDRAW = 2.0  # seconds between tab bar redraws

# Colors — Catppuccin Mocha palette, pre-converted to Kitty's as_rgb
# format at import time so the draw loop pays zero conversion cost.
_HEX_GREEN = "#a6e3a1"
_HEX_YELLOW = "#f9e2af"
_HEX_ORANGE = "#fab387"
_HEX_RED = "#f38ba8"
_HEX_GRAY = "#6c7086"
_HEX_TEXT = "#cdd6f4"


def _hex_to_rgb(hex_color: str) -> int:
    """Convert a hex color string to Kitty's as_rgb format."""
    return as_rgb(int(to_color(hex_color)))


COLOR_GREEN = _hex_to_rgb(_HEX_GREEN)
COLOR_YELLOW = _hex_to_rgb(_HEX_YELLOW)
COLOR_ORANGE = _hex_to_rgb(_HEX_ORANGE)
COLOR_RED = _hex_to_rgb(_HEX_RED)
COLOR_GRAY = _hex_to_rgb(_HEX_GRAY)
COLOR_TEXT = _hex_to_rgb(_HEX_TEXT)


# ============================================================================
# Status Cell
# ============================================================================


class Cell(NamedTuple):
    """A single status cell to render in the tab bar."""

    icon: str
    color: int  # pre-converted as_rgb color for the icon
    text: str


# ============================================================================
# Cached Value Helper
# ============================================================================


class CachedValue[T]:
    """A value that refreshes via a callback when its TTL expires.

    On first access, fetches synchronously so we have something to show.
    Subsequent refreshes are scheduled via Kitty's add_timer to avoid
    blocking the UI thread.
    """

    def __init__(self, fetch: Callable[[], T], ttl: float) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._value: T | None = None
        self._last_refresh: float = 0.0

    def get(self) -> T | None:
        now = time.time()
        if self._value is None:
            self._value = self._fetch()
            self._last_refresh = now
        elif (now - self._last_refresh) > self._ttl:
            add_timer(self._timer_refresh, 0.1, False)
        return self._value

    def _timer_refresh(self, timer_id: int) -> None:
        self._value = self._fetch()
        self._last_refresh = time.time()


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

# ICMP packet constants
ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
ICMP_HEADER_FORMAT = "!BBHHH"  # type, code, checksum, id, sequence
ICMP_HEADER_SIZE = struct.calcsize(ICMP_HEADER_FORMAT)

_ping_results: dict[str, float | None] = {t: None for t in PING_TARGETS}
_ping_lock = threading.Lock()
_ping_started = False


def _icmp_checksum(data: bytes) -> int:
    """Calculate ICMP checksum per RFC 1071.

    Sums all 16-bit words, folds the carry bits back in, then inverts.
    Pads with a zero byte if the data length is odd.
    """
    if len(data) % 2:
        data += b"\x00"
    words = struct.unpack("!%dH" % (len(data) // 2), data)
    s = sum(words)
    s = (s >> 16) + (s & 0xFFFF)  # fold high 16 into low 16
    s += s >> 16  # fold again if that produced a carry
    return ~s & 0xFFFF


def _build_icmp_packet() -> tuple[bytes, int, int]:
    """Build an ICMP echo request packet.

    Returns (packet_bytes, icmp_id, sequence_number).
    """
    icmp_id = os.getpid() & 0xFFFF
    seq = random.randint(0, 0xFFFF)
    payload = struct.pack("!d", time.monotonic())

    # First pass: build with checksum=0 to calculate the real checksum
    header = struct.pack(ICMP_HEADER_FORMAT, ICMP_ECHO_REQUEST, 0, 0, icmp_id, seq)
    checksum = _icmp_checksum(header + payload)

    # Second pass: rebuild with the real checksum
    header = struct.pack(
        ICMP_HEADER_FORMAT, ICMP_ECHO_REQUEST, 0, checksum, icmp_id, seq
    )
    return header + payload, icmp_id, seq


def _icmp_offset(data: bytes) -> int:
    """Find the start of the ICMP header in a received packet.

    On macOS, SOCK_DGRAM + IPPROTO_ICMP includes the IP header in
    received packets (unlike Linux, which strips it). This is a known
    macOS kernel behavior. We detect the IP header by checking if the
    first nibble is 0x4 (IPv4) and use the IHL field to skip past it.
    See: https://lists.endsoftwarepatents.org/archive/html/qemu-devel/2018-08/msg02811.html
    """
    if len(data) > 0 and (data[0] & 0xF0) == 0x40:
        # IPv4 header present — IHL field gives header length in 32-bit words
        return (data[0] & 0x0F) * 4
    return 0


def _ping_host(target: str, timeout: float = PING_TIMEOUT) -> float | None:
    """Send one ICMP echo request and return RTT in ms, or None on failure.

    Uses SOCK_DGRAM + IPPROTO_ICMP (unprivileged on macOS).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        sock.settimeout(timeout)
    except OSError:
        return None

    try:
        packet, icmp_id, seq = _build_icmp_packet()
        start = time.monotonic()
        sock.sendto(packet, (target, 0))

        # Wait for our echo reply (ignoring other ICMP traffic)
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            data, _ = sock.recvfrom(1024)

            offset = _icmp_offset(data)
            if len(data) < offset + ICMP_HEADER_SIZE:
                continue
            resp_type, _, _, resp_id, resp_seq = struct.unpack(
                ICMP_HEADER_FORMAT, data[offset : offset + ICMP_HEADER_SIZE]
            )
            if resp_type == ICMP_ECHO_REPLY and resp_id == icmp_id and resp_seq == seq:
                return (time.monotonic() - start) * 1000
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


def _ping_target_loop(target: str) -> None:
    """Background thread: continuously ping a single target."""
    while True:
        rtt = _ping_host(target)
        with _ping_lock:
            _ping_results[target] = rtt
        time.sleep(PING_INTERVAL)


def _get_best_ping() -> float | None:
    """Return the best (lowest) RTT across all targets, or None if all failed."""
    with _ping_lock:
        rtts = [r for r in _ping_results.values() if r is not None]
    return min(rtts) if rtts else None


def _start_ping_threads() -> None:
    """Start one background thread per ping target (once)."""
    global _ping_started
    if _ping_started:
        return
    _ping_started = True
    for target in PING_TARGETS:
        t = threading.Thread(target=_ping_target_loop, args=(target,), daemon=True)
        t.start()


# ============================================================================
# Tailscale Monitor
# ============================================================================


@dataclass
class TailscaleState:
    """Parsed Tailscale status."""

    backend_state: str = "Unknown"
    tailnet_name: str = ""


_TAILSCALE_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/tailscale"),
    "/opt/homebrew/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
]


def _find_tailscale() -> str:
    """Find the tailscale binary. Returns path or empty string.

    Kitty GUI apps run with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin)
    that doesn't include user directories like ~/.local/bin or Homebrew.
    We check shutil.which first (in case it's on PATH), then fall back
    to known macOS locations.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    for path in _TAILSCALE_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


# Resolved once at import time — the binary location never changes.
_tailscale_bin = _find_tailscale()


def _fetch_tailscale_status() -> TailscaleState:
    """Run tailscale status --json and parse the result."""
    if not _tailscale_bin:
        return TailscaleState(backend_state="NotInstalled")

    try:
        result = subprocess.run(
            [_tailscale_bin, "status", "--json"],
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
                tailnet_name = magic_dns.removesuffix(".ts.net") if magic_dns else ""

        return TailscaleState(backend_state=state, tailnet_name=tailnet_name)
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        return TailscaleState(backend_state="Error")


_tailscale_cache = CachedValue(_fetch_tailscale_status, TAILSCALE_TTL)


def _get_tailscale_state() -> TailscaleState | None:
    """Get current tailscale state, or None if tailscale isn't installed."""
    state = _tailscale_cache.get()
    if state and state.backend_state == "NotInstalled":
        return None
    return state


# ============================================================================
# Battery Monitor
# ============================================================================


@dataclass
class BatteryState:
    """Parsed battery status."""

    percent: int = -1
    charging: bool = False
    present: bool = False


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

        # Parse: "62%; discharging;" or "85%; charging;" or "100%; charged;"
        match = re.search(
            r"(\d+)%;\s*(charging|discharging|charged|finishing charge)",
            result.stdout,
        )
        if not match:
            return BatteryState()

        percent = int(match.group(1))
        charging = match.group(2) in ("charging", "charged", "finishing charge")
        return BatteryState(percent=percent, charging=charging, present=True)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return BatteryState()


_battery_cache = CachedValue(_fetch_battery_status, BATTERY_TTL)


def _get_battery_state() -> BatteryState | None:
    """Get current battery state, or None if no battery detected."""
    state = _battery_cache.get()
    return state if state and state.present else None


# ============================================================================
# Cell Builders
# ============================================================================


def _build_ping_cell() -> Cell | None:
    """Build the ping status cell."""
    rtt = _get_best_ping()
    if rtt is None:
        return Cell("󰤭 ", COLOR_GRAY, "offline")

    if rtt < 100:
        color = COLOR_GREEN
    elif rtt < 500:
        color = COLOR_YELLOW
    elif rtt < 2000:
        color = COLOR_ORANGE
    else:
        color = COLOR_RED

    text = f"{rtt:.0f}ms" if rtt < 1000 else f"{rtt / 1000:.1f}s"
    return Cell("󰤨 ", color, text)


def _build_tailscale_cell() -> Cell | None:
    """Build the Tailscale status cell."""
    state = _get_tailscale_state()
    if state is None:
        return None

    match state.backend_state:
        case "Running":
            return Cell("󰒍 ", COLOR_GREEN, state.tailnet_name or "connected")
        case "NeedsLogin":
            return Cell("󰒎 ", COLOR_YELLOW, "needs login")
        case "Stopped":
            return Cell("󰒎 ", COLOR_GRAY, "stopped")
        case "Starting":
            return Cell("󰒍 ", COLOR_YELLOW, "connecting...")
        case _:
            return Cell("󰒎 ", COLOR_RED, "unknown")


class _BatteryTier(NamedTuple):
    min_percent: int
    icon_charging: str
    icon_discharging: str
    color_charging: int
    color_discharging: int


_BATTERY_TIERS = [
    _BatteryTier(80, "󰂅 ", "󰁹 ", COLOR_GREEN, COLOR_GREEN),
    _BatteryTier(50, "󰂉 ", "󰂀 ", COLOR_GREEN, COLOR_YELLOW),
    _BatteryTier(20, "󰂇 ", "󰁾 ", COLOR_YELLOW, COLOR_ORANGE),
    _BatteryTier(0, "󰢜 ", "󰁺 ", COLOR_ORANGE, COLOR_RED),
]


def _build_battery_cell() -> Cell | None:
    """Build the battery status cell."""
    state = _get_battery_state()
    if state is None:
        return None

    for tier in _BATTERY_TIERS:
        if state.percent >= tier.min_percent:
            icon = tier.icon_charging if state.charging else tier.icon_discharging
            color = tier.color_charging if state.charging else tier.color_discharging
            return Cell(icon, color, f"{state.percent}%")

    # Should never reach here, but just in case
    return Cell("󰂎 ", COLOR_RED, f"{state.percent}%")


# Spinner: advances once per redraw timer tick (not per draw_tab call).
# The timer callback increments the counter; draw_tab just reads it.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_spinner_index = 0


def _advance_spinner() -> None:
    """Called by the redraw timer to advance the spinner one frame."""
    global _spinner_index
    _spinner_index += 1


def _build_spinner_cell() -> Cell:
    """Build a spinner cell. Proof of life for the redraw timer."""
    frame = _SPINNER_FRAMES[_spinner_index % len(_SPINNER_FRAMES)]
    return Cell(frame + " ", COLOR_GRAY, "")


# Status cells in display order: battery, ping, tailscale, spinner
_CELL_BUILDERS = (
    _build_battery_cell,
    _build_ping_cell,
    _build_tailscale_cell,
    _build_spinner_cell,
)


def _build_cells() -> list[Cell]:
    """Build all status cells, skipping any that return None."""
    return [cell for b in _CELL_BUILDERS if (cell := b()) is not None]


# ============================================================================
# Drawing
# ============================================================================


def _cell_width(cell: Cell) -> int:
    """Display width of a cell: icon + text + trailing gap."""
    return len(cell.icon) + len(cell.text) + 2


def _draw_right_status(draw_data: DrawData, screen: Screen, cells: list[Cell]) -> None:
    """Draw right-aligned status cells on the tab bar."""
    if not cells:
        return

    draw_attributed_string(Formatter.reset, screen)
    default_bg = as_rgb(int(draw_data.default_bg))

    # Drop cells from the left if there's not enough space
    total_width = sum(_cell_width(c) for c in cells)
    padding = screen.columns - screen.cursor.x - total_width
    while cells and padding < 0:
        padding += _cell_width(cells.pop(0))
    if not cells:
        return

    if padding > 0:
        screen.cursor.bg = default_bg
        screen.draw(" " * padding)

    for cell in cells:
        screen.cursor.bg = default_bg
        screen.cursor.fg = cell.color
        screen.draw(cell.icon)
        screen.cursor.fg = COLOR_TEXT
        screen.draw(cell.text)
        screen.draw("  ")


# ============================================================================
# Main Entry Point
# ============================================================================

_timer_id = None


def _redraw_tab_bar(timer_id: int) -> None:
    """Mark all tab bars as dirty, triggering a redraw."""
    _advance_spinner()
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
        _start_ping_threads()

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
        _draw_right_status(draw_data, screen, _build_cells())

    return screen.cursor.x
