"""Custom Kitty tab bar with network status panel.

Displays battery, ping latency, and Tailscale status in the right side
of the tab bar. Tabs are rendered using Kitty's built-in powerline style.

When run directly (``python3 tab_bar.py``), the file acts as the helper
process that polls battery and Tailscale status.  When imported by Kitty
(``__name__ != "__main__"``), it provides the ``draw_tab`` entry point.

See AGENTS.md for full design details.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

# ============================================================================
# Configuration
# ============================================================================

PING_TARGETS = ["1.1.1.1", "8.8.8.8"]
PING_INTERVAL = 2.0  # seconds between pings
PING_TIMEOUT = 2.0  # seconds before ping gives up
TAB_BAR_REDRAW = 2.0  # seconds between tab bar redraws

# ============================================================================
# Tailscale Discovery (shared by both modes)
# ============================================================================

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

# ============================================================================
# Helper Process Functions (shared by both modes)
#
# These run inside the helper process (when __name__ == "__main__") and
# are also importable by the Kitty side for path constants.
# ============================================================================


_STATUS_FILE = f"/tmp/kitty-status-tabbar.{os.getuid()}.json"
_HELPER_LOG_PATH = f"/tmp/kitty-status-tabbar.{os.getuid()}.log"


def _fetch_battery_status() -> dict | None:
    """Run pmset -g batt and return parsed battery info, or None."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None

        match = re.search(r"(\d+)%;\s*([\w ]+);", result.stdout)
        if not match:
            return None

        percent = int(match.group(1))
        charging = match.group(2) in ("charging", "charged", "finishing charge")
        return {"percent": percent, "charging": charging, "present": True}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _fetch_tailscale_status(tailscale_bin: str) -> dict | None:
    """Run tailscale status --json and return parsed state, or None."""
    try:
        result = subprocess.run(
            [tailscale_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {"state": "Error", "tailnet": ""}

        data = json.loads(result.stdout)
        state = data.get("BackendState", "Unknown")
        tailnet_name = ""

        if state == "Running":
            current_tailnet = data.get("CurrentTailnet", {})
            tailnet_name = current_tailnet.get("Name", "")
            if not tailnet_name:
                magic_dns = data.get("MagicDNSSuffix", "")
                tailnet_name = magic_dns.removesuffix(".ts.net") if magic_dns else ""

        return {"state": state, "tailnet": tailnet_name}
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        return {"state": "Error", "tailnet": ""}


def _write_status(
    output_path: str, battery: dict | None, tailscale: dict | None
) -> None:
    """Atomically write status to the output file."""
    import sys

    data = {
        "battery": battery,
        "tailscale": tailscale,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    tmp_path = output_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, output_path)
    except OSError as e:
        print(f"Failed to write status: {e}", file=sys.stderr)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ============================================================================
# Kitty-only code — everything below here requires Kitty's Python environment
# and only runs when the module is imported by Kitty (not when run directly).
# ============================================================================

if __name__ != "__main__":
    import random
    import signal
    import socket
    import struct
    import threading
    from dataclasses import dataclass
    from typing import NamedTuple

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

    class Cell(NamedTuple):
        """A single status cell to render in the tab bar."""

        icon: str
        color: int  # pre-converted as_rgb color for the icon
        text: str

    # Colors — Catppuccin Mocha palette, pre-converted to Kitty's as_rgb
    # format at import time so the draw loop pays zero conversion cost.

    def _hex_to_rgb(hex_color: str) -> int:
        """Convert a hex color string to Kitty's as_rgb format."""
        return as_rgb(int(to_color(hex_color)))

    COLOR_GREEN = _hex_to_rgb("#a6e3a1")
    COLOR_YELLOW = _hex_to_rgb("#f9e2af")
    COLOR_ORANGE = _hex_to_rgb("#fab387")
    COLOR_RED = _hex_to_rgb("#f38ba8")
    COLOR_GRAY = _hex_to_rgb("#6c7086")
    COLOR_TEXT = _hex_to_rgb("#cdd6f4")

    # ========================================================================
    # Ping Monitor (pure Python ICMP — no subprocess, no dependencies)
    #
    # Why not shell out to `ping`? Three reasons:
    #   1. Spawning 4 processes every 2s for the lifetime of the terminal is
    #      wasteful — this runs in a background thread with zero process
    #      overhead.
    #   2. Parsing `ping` stdout is fragile across OS versions.
    #   3. Third-party libs (icmplib) can't be installed into Kitty's bundled
    #      Python without breaking on Kitty updates.
    #
    # We use SOCK_DGRAM + IPPROTO_ICMP which works unprivileged on macOS
    # (no root/setuid needed). We build ICMP echo request packets directly
    # and measure RTT with time.monotonic(). See AGENTS.md for full
    # rationale.
    # ========================================================================

    # ICMP packet constants
    ICMP_ECHO_REQUEST = 8
    ICMP_ECHO_REPLY = 0
    ICMP_HEADER_FORMAT = "!BBHHH"  # type, code, checksum, id, sequence
    ICMP_HEADER_SIZE = struct.calcsize(ICMP_HEADER_FORMAT)

    # Generation counter: minted on each import and stored on the Boss
    # singleton (which survives module re-imports across config reloads).
    # The timer callback and ping threads capture their generation at
    # creation time and compare against the boss's current value — when
    # they differ, the callback becomes a no-op and threads exit.
    _generation = id(object())
    get_boss()._tab_bar_gen = _generation

    _ping_results: dict[str, float | None] = {t: None for t in PING_TARGETS}

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
            # IPv4 header present — IHL field gives header length in
            # 32-bit words
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
                    ICMP_HEADER_FORMAT,
                    data[offset : offset + ICMP_HEADER_SIZE],
                )
                if (
                    resp_type == ICMP_ECHO_REPLY
                    and resp_id == icmp_id
                    and resp_seq == seq
                ):
                    return (time.monotonic() - start) * 1000
        except (socket.timeout, OSError):
            return None
        finally:
            sock.close()

    def _ping_target_loop(target: str, gen: int) -> None:
        """Background thread: continuously ping a single target.

        Exits when the generation on the Boss singleton changes (i.e. the
        module was re-imported due to a config reload).
        """
        try:
            while gen == get_boss()._tab_bar_gen:
                rtt = _ping_host(target)
                _ping_results[target] = rtt
                time.sleep(PING_INTERVAL)
        except Exception:
            pass

    def _get_best_ping() -> float | None:
        """Return the best (lowest) RTT across all targets, or None if all failed."""
        rtts = [r for r in _ping_results.values() if r is not None]
        return min(rtts) if rtts else None

    def _start_background_workers() -> None:
        """Start ping threads and the external helper process."""
        for target in PING_TARGETS:
            t = threading.Thread(
                target=_ping_target_loop,
                args=(target, _generation),
                daemon=True,
            )
            t.start()
        _start_helper_process()

    # ====================================================================
    # Tailscale Monitor
    # ====================================================================

    @dataclass
    class TailscaleState:
        """Parsed Tailscale status."""

        backend_state: str = "Unknown"
        tailnet_name: str = ""

    # ====================================================================
    # External Helper Process
    #
    # Forking subprocesses from background threads inside Kitty deadlocks.
    # We spawn tab_bar.py itself (via __main__) as a standalone process
    # that polls battery and Tailscale, writing results to a JSON temp
    # file that draw_tab reads.
    # ====================================================================

    _status_cache: dict | None = None
    _status_mtime: float = 0.0

    def _read_status_file() -> dict | None:
        """Read and parse the status file, caching by mtime."""
        global _status_cache, _status_mtime
        try:
            mtime = os.path.getmtime(_STATUS_FILE)
            if mtime == _status_mtime and _status_cache is not None:
                return _status_cache
            with open(_STATUS_FILE) as f:
                _status_cache = json.load(f)
                _status_mtime = mtime
                return _status_cache
        except (OSError, json.JSONDecodeError):
            return _status_cache  # return stale data on error

    def _start_helper_process() -> None:
        """Spawn the external helper process for battery and Tailscale polling.

        Kills any previously running helper (identified by pid in the status
        file), then spawns tab_bar.py itself with __main__ arguments.
        """
        # Kill old helper if running
        try:
            with open(_STATUS_FILE) as f:
                old_data = json.load(f)
            old_pid = old_data.get("pid")
            if old_pid:
                os.kill(old_pid, signal.SIGTERM)
        except (OSError, json.JSONDecodeError, TypeError, ProcessLookupError):
            pass

        # Spawn self as helper process
        log_file = open(_HELPER_LOG_PATH, "w")
        subprocess.Popen(
            [
                "/usr/bin/python3",
                __file__,
                "--output",
                _STATUS_FILE,
                "--interval",
                str(PING_INTERVAL),
                "--parent-pid",
                str(os.getpid()),
                *(["--tailscale-bin", _tailscale_bin] if _tailscale_bin else []),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
        )
        log_file.close()

    def _get_tailscale_state(data: dict | None = None) -> TailscaleState | None:
        """Get current tailscale state, or None if tailscale isn't installed."""
        if data is None:
            data = _read_status_file()
        if not data or data.get("tailscale") is None:
            return None
        ts = data["tailscale"]
        return TailscaleState(
            backend_state=ts.get("state", "Unknown"),
            tailnet_name=ts.get("tailnet", ""),
        )

    # ====================================================================
    # Battery Monitor
    # ====================================================================

    @dataclass
    class BatteryState:
        """Parsed battery status."""

        percent: int = -1
        charging: bool = False
        present: bool = False

    def _get_battery_state(data: dict | None = None) -> BatteryState | None:
        """Get current battery state, or None if no battery detected."""
        if data is None:
            data = _read_status_file()
        if not data or data.get("battery") is None:
            return None
        b = data["battery"]
        if not b.get("present", False):
            return None
        return BatteryState(
            percent=b.get("percent", -1),
            charging=b.get("charging", False),
            present=True,
        )

    # ====================================================================
    # Cell Builders
    # ====================================================================

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

    def _build_tailscale_cell(status_data: dict | None = None) -> Cell | None:
        """Build the Tailscale status cell."""
        state = _get_tailscale_state(status_data)
        if state is None:
            return None

        bs = state.backend_state
        if bs == "Running":
            return Cell("󰒍 ", COLOR_GREEN, state.tailnet_name or "connected")
        elif bs == "NeedsLogin":
            return Cell("󰒎 ", COLOR_YELLOW, "needs login")
        elif bs == "Stopped":
            return Cell("󰒎 ", COLOR_GRAY, "stopped")
        elif bs == "Starting":
            return Cell("󰒍 ", COLOR_YELLOW, "connecting...")
        else:
            return Cell("󰒎 ", COLOR_RED, "unknown")

    class _BatteryTier(NamedTuple):
        min_percent: int
        icon_charging: str
        icon_discharging: str
        color_charging: int
        color_discharging: int

    _BATTERY_TIERS = [
        _BatteryTier(90, "󰂅 ", "󰂂 ", COLOR_GREEN, COLOR_GREEN),
        _BatteryTier(80, "󰂊 ", "󰂁 ", COLOR_GREEN, COLOR_GREEN),
        _BatteryTier(70, "󰢞 ", "󰂀 ", COLOR_YELLOW, COLOR_YELLOW),
        _BatteryTier(60, "󰂉 ", "󰁿 ", COLOR_YELLOW, COLOR_YELLOW),
        _BatteryTier(50, "󰢝 ", "󰁾 ", COLOR_YELLOW, COLOR_YELLOW),
        _BatteryTier(40, "󰂈 ", "󰁽 ", COLOR_ORANGE, COLOR_ORANGE),
        _BatteryTier(30, "󰂇 ", "󰁼 ", COLOR_ORANGE, COLOR_ORANGE),
        _BatteryTier(20, "󰂆 ", "󰁻 ", COLOR_ORANGE, COLOR_ORANGE),
        _BatteryTier(10, "󰢜 ", "󰁺 ", COLOR_RED, COLOR_RED),
        _BatteryTier(0, "󰢜 ", "󰁺 ", COLOR_RED, COLOR_RED),
    ]

    def _build_battery_cell(status_data: dict | None = None) -> Cell | None:
        """Build the battery status cell."""
        state = _get_battery_state(status_data)
        if state is None:
            return None

        for tier in _BATTERY_TIERS:
            if state.percent >= tier.min_percent:
                icon = tier.icon_charging if state.charging else tier.icon_discharging
                color = (
                    tier.color_charging if state.charging else tier.color_discharging
                )
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

    _HELPER_STALE_THRESHOLD = 10.0  # seconds before helper is considered stuck

    def _build_spinner_cell(status_data: dict | None = None) -> Cell:
        """Build a spinner cell. Red if the helper process appears stuck."""
        frame = _SPINNER_FRAMES[_spinner_index % len(_SPINNER_FRAMES)]
        updated_at = (status_data or {}).get("updated_at", 0)
        stale = (time.time() - updated_at) > _HELPER_STALE_THRESHOLD
        return Cell(frame + " ", COLOR_RED if stale else COLOR_GRAY, "")

    def _build_cells() -> list[Cell]:
        """Build all status cells, skipping any that return None.

        Reads the helper status file once and shares it across battery and
        tailscale cell builders to avoid redundant stat/parse calls.
        """
        status_data = _read_status_file()
        cells: list[Cell] = []
        for builder in (
            lambda: _build_battery_cell(status_data),
            _build_ping_cell,
            lambda: _build_tailscale_cell(status_data),
            lambda: _build_spinner_cell(status_data),
        ):
            cell = builder()
            if cell is not None:
                cells.append(cell)
        return cells

    # ====================================================================
    # Drawing
    # ====================================================================

    def _cell_width(cell: Cell) -> int:
        """Display width of a cell: icon + text + trailing gap."""
        return len(cell.icon) + len(cell.text) + 2

    def _draw_right_status(
        draw_data: DrawData, screen: Screen, cells: list[Cell]
    ) -> None:
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

    # ====================================================================
    # Main Entry Point
    # ====================================================================

    def _make_redraw_callback(gen: int):
        """Create a redraw callback bound to a specific generation.

        On config reload, Kitty re-imports the module but can't cancel old
        timers. The closure captures its generation at creation time; when
        it no longer matches the boss's current generation, it becomes a
        cheap no-op.
        """

        def _redraw_tab_bar(timer_id: int) -> None:
            if gen != get_boss()._tab_bar_gen:
                return
            _advance_spinner()
            for tm in get_boss().all_tab_managers:
                tm.mark_tab_bar_dirty()

        return _redraw_tab_bar

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

        Delegates tab rendering to powerline, then draws status cells on
        the last tab.
        """
        if not getattr(draw_tab, "_initialized", False):
            draw_tab._initialized = True
            add_timer(_make_redraw_callback(_generation), TAB_BAR_REDRAW, True)
            _start_background_workers()

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


# ============================================================================
# Helper Process Entry Point
#
# When run directly (python3 tab_bar.py), acts as the helper process that
# polls battery and Tailscale status, writing results to a JSON temp file.
# ============================================================================

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="kitty-status-tabbar helper")
    parser.add_argument("--tailscale-bin", default="", help="Path to tailscale binary")
    parser.add_argument(
        "--interval", type=float, default=2.0, help="Poll interval in seconds"
    )
    parser.add_argument("--output", required=True, help="Output JSON file path")
    parser.add_argument(
        "--parent-pid", type=int, default=0, help="Exit when this PID dies"
    )
    args = parser.parse_args()

    # Restrict file permissions: created files are 600
    os.umask(0o077)

    print(
        f"Helper started (pid={os.getpid()}, interval={args.interval}s)",
        file=sys.stderr,
    )

    try:
        while True:
            # Exit if parent process (Kitty) is gone
            if args.parent_pid and not _is_pid_alive(args.parent_pid):
                print("Parent process gone, exiting", file=sys.stderr)
                break

            battery = _fetch_battery_status()
            tailscale = (
                _fetch_tailscale_status(args.tailscale_bin)
                if args.tailscale_bin
                else None
            )
            _write_status(args.output, battery, tailscale)
            time.sleep(args.interval)
    finally:
        # Clean up temp files
        for path in (args.output, args.output + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass
