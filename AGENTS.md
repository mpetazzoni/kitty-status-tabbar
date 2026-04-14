# AGENTS.md

## Project overview

Custom Kitty terminal tab bar (`tab_bar.py`) that displays real-time
system status — ping latency, Tailscale VPN, and battery — right-aligned
in the tab bar. macOS only, zero external dependencies. Single-file
Python project loaded by Kitty's `tab_bar_style custom` mechanism.

## Code style

- Python, single file (`tab_bar.py`)
- No external dependencies — runs inside Kitty's bundled Python
- Type hints throughout
- `@dataclass` and `NamedTuple` for structured data
- Nerd Font icons for status display

## Build and test

There is no build step or test suite. The file is loaded directly by
Kitty. To test, copy `tab_bar.py` to `~/.config/kitty/` and reload
Kitty (`ctrl+shift+f5` or relaunch).

## Workflow

- Work directly on `main` — no feature branches
- Conventional commit messages: `feat:`, `fix:`, `refactor:`, `docs:`, etc.

## Releasing

1. Commit changes and push to `main`
2. Tag: `git tag v<major>.<minor>.<patch>`
3. Push the tag: `git push --tags`
4. Create a GitHub release **with assets**:
   ```sh
   gh release create v<version> --title "v<version>" --notes "<description>"
   gh release upload v<version> tab_bar.py install.sh
   ```
   Both `tab_bar.py` and `install.sh` must be uploaded as release assets.
   The install script fetches from `releases/latest/download/`, so
   missing assets break installation.

## Architecture

**This is a custom tab bar, not a kitten.** Kittens are interactive
programs in a Kitty window. This is a persistent status display in the
tab bar itself, via `tab_bar_style custom`.

### How it works

1. `tab_bar.py` exports `draw_tab()`, called by Kitty for each tab. On
   the last tab, we draw right-aligned status cells.
2. **Ping** runs in background threads (pure Python ICMP sockets,
   unprivileged on macOS). Two targets (`1.1.1.1`, `8.8.8.8`) pinged in
   parallel; best RTT displayed.
3. **Tailscale and battery** are polled by a standalone helper process
   that writes to a temp file; `draw_tab` reads the file (no forking
   inside Kitty's process).
4. `add_timer()` from Kitty's API triggers periodic redraws.

### Display format

```
[tabs...]              󰁹 62%  󰤨  23ms  󰒍 my-tailnet
[tabs...]              󰁺 12%  󰤭  offline  󰒎 stopped
```

### Key design decisions

- **Pure Python ICMP** instead of shelling out to `ping` — no subprocess
  overhead, no output parsing, uses `SOCK_DGRAM + IPPROTO_ICMP`
  (unprivileged on macOS). See code comments for full rationale.
- **External helper process** for battery/Tailscale — forking from
  background threads inside Kitty deadlocks, so we spawn a standalone
  Python script that polls and writes results to a JSON temp file.
  `draw_tab` reads the file, which is instant and fork-free.
- **Powerline delegation** — tab rendering uses Kitty's built-in
  `draw_tab_with_powerline`; custom code only handles status cells.
- **PATH workaround** — Kitty GUI apps get minimal PATH; we search
  `~/.local/bin`, Homebrew paths, and the macOS app bundle for Tailscale.

### Latency color thresholds

| Color  | Range    | Meaning |
|--------|----------|---------|
| Green  | <100ms   | Great   |
| Yellow | 100-500ms| Okay    |
| Orange | 500ms-2s | Slow    |
| Red    | >2s      | Bad     |
| Gray   | No reply | Offline |

### Files

| File                 | Purpose                                  |
|----------------------|------------------------------------------|
| `tab_bar.py`         | Custom tab bar with status cells         |
| `install.sh`         | One-step install script                  |
| `kitty.conf.example` | Example kitty.conf snippet               |
| `AGENTS.md`          | This file — architecture & agent guide   |
| `README.md`          | Installation & usage for humans          |
