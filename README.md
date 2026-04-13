# kitty-status-tabbar

A custom [Kitty](https://sw.kovidgoyal.net/kitty/) tab bar that shows
live system status in the right side of the tab bar: network
connectivity with ping latency, Tailscale VPN status, and battery
level.

**Zero dependencies.** Single file. Just drop it in your Kitty config
directory.

## Features

- **Ping latency** — continuously pings `1.1.1.1` and `8.8.8.8` in
  the background, shows the best RTT with color-coded thresholds
  (airplane-wifi-friendly: green <100ms, yellow <500ms, orange <2s,
  red >2s, gray = offline)
- **Tailscale status** — shows your tailnet name when connected, or
  the specific state when not (needs login, stopped, connecting)
- **Battery** — percentage with charge-aware icon and color
- **Powerline tabs** — delegates tab rendering to Kitty's built-in
  powerline style
- **Graceful degradation** — Tailscale cell hidden if not installed;
  battery cell hidden on desktops; cells dropped if the window is too
  narrow

## Screenshot

```
 Tab 1   Tab 2                  󰁹 62%  󰤨 23ms  󰒍 my-tailnet 
```

*(The Nerd Font icons and powerline glyphs render properly inside Kitty)*

## Requirements

- [Kitty](https://sw.kovidgoyal.net/kitty/) terminal emulator
- A [Nerd Font](https://www.nerdfonts.com/) (for the status icons)
- macOS (uses unprivileged ICMP sockets and `pmset` for battery;
  see [Platform support](#platform-support))

## Installation

1. Download `tab_bar.py` from the
   [latest release](https://github.com/mpetazzoni/kitty-status-tabbar/releases/latest)
   into your Kitty config directory:

   ```sh
   curl -L https://github.com/mpetazzoni/kitty-status-tabbar/releases/latest/download/tab_bar.py \
     -o ~/.config/kitty/tab_bar.py
   ```

2. Add to your `~/.config/kitty/kitty.conf`:

   ```conf
   tab_bar_style custom
   tab_bar_min_tabs 1
   ```

3. **(Tailscale users on macOS)** The macOS App Store version of
   Tailscale doesn't put the CLI on your PATH. Symlinks don't work
   due to the App Store sandbox. Create a wrapper script instead:

   ```sh
   mkdir -p ~/.local/bin
   cat > ~/.local/bin/tailscale << 'EOF'
   #!/bin/sh
   exec /Applications/Tailscale.app/Contents/MacOS/Tailscale "$@"
   EOF
   chmod +x ~/.local/bin/tailscale
   ```

   Make sure `~/.local/bin` is in your PATH. If Tailscale isn't
   installed or not on PATH, the Tailscale cell is simply hidden.

4. Reload Kitty (`ctrl+shift+f5`) or restart it.

## Configuration

Edit the constants at the top of `tab_bar.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `PING_TARGETS` | `["1.1.1.1", "8.8.8.8"]` | Hosts to ping |
| `PING_INTERVAL` | `2.0` | Seconds between pings |
| `PING_TIMEOUT` | `2.0` | Seconds before a ping is considered lost |
| `TAILSCALE_TTL` | `10.0` | Seconds between Tailscale status checks |
| `BATTERY_TTL` | `30.0` | Seconds between battery checks |
| `TAB_BAR_REDRAW` | `2.0` | Seconds between tab bar redraws |

Colors use the [Catppuccin Mocha](https://catppuccin.com/) palette by
default. Change the `COLOR_*` constants to match your theme.

## How it works

- **Ping** uses pure Python ICMP sockets (`SOCK_DGRAM` +
  `IPPROTO_ICMP`) — no subprocess spawning, no dependencies. One
  background thread per target runs continuously. See
  [PLAN.md](PLAN.md) for the full rationale on why we chose this over
  shelling out to `ping` or using a third-party library.

- **Tailscale** runs `tailscale status --json` periodically, cached
  with a lazy-refresh pattern so it never blocks the UI.

- **Battery** parses `pmset -g batt` output, also lazy-cached.

## Platform support

Currently **macOS only**:

- ICMP `SOCK_DGRAM` sockets work unprivileged on macOS. On Linux,
  this requires `net.ipv4.ping_group_range` sysctl or `CAP_NET_RAW`.
- Battery monitoring uses `pmset`, a macOS-specific tool.

Contributions to add Linux support are welcome!

## License

Copyright 2026 Maxime Petazzoni

Licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE) for details.
