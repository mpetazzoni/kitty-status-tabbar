# kitty-status-tabbar

A custom Kitty tab bar that displays real-time network connectivity
status (ping latency + Tailscale + battery) in the right side of the
tab bar.

## How to work

* Build and document a plan
* Record this plan into this PLAN.md file
* Make a todo-list in PLAN.md
* Keep the plan, and the todo-list, updated in PLAN.md at every step of
  the way
* Make a Git repository, work locally directly in the main branch (this
  supersedes the instructions of my dev-workflow skill)

## Architecture

**This is a custom tab bar, not a kitten.** Kittens are interactive
programs that run in a kitty window. What we want is a persistent status
display embedded in the tab bar itself, achieved via `tab_bar_style
custom` and a `tab_bar.py` file in the kitty config directory.

### How it works

1. **`tab_bar.py`** is loaded by Kitty from its config directory. It
   exports a `draw_tab()` function called for each tab. On the *last*
   tab, we draw right-aligned status cells showing network info.

2. **Background ping threads** continuously ping `1.1.1.1` and
   `8.8.8.8` in parallel. We store the latest RTT for each target and
   display the best (minimum) result. If one target fails but the other
   succeeds, we're still online. Both fail = offline.

3. **Tailscale check** runs `tailscale status --json` periodically
   (cached with ~10s TTL via lazy-refresh timer) to check connection
   status. Gracefully skipped if Tailscale isn't installed.

4. **`add_timer()`** from Kitty's API triggers periodic tab bar redraws
   so the status stays current.

### Display format

Right side of the tab bar:

```
[tabs...]              󰁹 62%  󰤨  23ms  󰒍 my-tailnet
[tabs...]              󰁺 12%  󰤭  offline  󰒎 TS: down
```

When Tailscale is connected, we show the tailnet name (from
`CurrentTailnet.Name` or `MagicDNSSuffix`). When it's down, we show
"TS: down". Battery shows percentage with a color-coded icon that
reflects charge level and charging state.

### Latency color buckets

Airplane-wifi-friendly thresholds:

| Color  | Range      | Meaning  | Rationale                              |
|--------|------------|----------|----------------------------------------|
| Green  | <100ms     | Great    | Normal terrestrial connection           |
| Yellow | 100-500ms  | Okay     | Decent airplane wifi, congested network |
| Orange | 500ms-2s   | Slow     | Bad airplane wifi, but packets flowing  |
| Red    | >2s        | Bad      | Barely functional                       |
| Gray   | No reply   | Offline  | Dead connection                         |

### Ping strategy

- Ping both `1.1.1.1` (Cloudflare) and `8.8.8.8` (Google) in parallel
  using background threads
- Display the best (minimum) RTT from the two targets
- Ping interval: every 2 seconds, 2s timeout per ping
- Store results in a thread-safe shared dict

#### Why pure Python ICMP instead of shelling out to `ping`?

We considered three approaches:

1. **`subprocess.run(["ping", "-c", "1", ...])`** — Simple, but spawns
   4 processes per cycle (2 targets × 2s interval). Requires parsing
   stdout which varies across OS versions. Wasteful for something that
   runs continuously for the lifetime of the terminal.

2. **Third-party library (`icmplib`)** — Clean API, but Kitty uses its
   own bundled Python interpreter. Installing packages into Kitty's
   Python is fragile and breaks across Kitty updates. We want zero
   external dependencies.

3. **Pure Python ICMP sockets** ✅ — Uses `SOCK_DGRAM` + `IPPROTO_ICMP`
   which works **unprivileged on macOS** (no root/setuid needed). Builds
   ICMP echo request packets directly (~30 lines), measures RTT with
   `time.monotonic()`. Zero dependencies, no subprocess overhead, precise
   timing. The only downside is it's macOS-specific (Linux requires
   `net.ipv4.ping_group_range` sysctl or `CAP_NET_RAW`), but this
   project targets macOS anyway.

### Tailscale strategy

- Run `tailscale status --json` via subprocess
- Cache result with 10s TTL (lazy-refresh pattern)
- Parse JSON for `BackendState` field
- Display based on state:
  - `Running`: `󰒍 my-tailnet` (green) — extract name from
    `CurrentTailnet.Name` or `MagicDNSSuffix` with `.ts.net` stripped
  - `NeedsLogin`: `󰒎 needs login` (yellow)
  - `Stopped`: `󰒎 stopped` (gray)
  - `Starting`: `󰒍 connecting...` (yellow)
  - Error/other: `󰒎 unknown` (red)
- If `tailscale` binary not found, skip the cell entirely

### Battery strategy

- Run `pmset -g batt` (macOS) — instant, no dependencies
- Parse percentage and charging/discharging state
- Cache with 30s TTL (battery doesn't change fast)
- Color-coded icon based on level + charging state:
  - 80-100%: green (󰂅 charging / 󰁹 discharging)
  - 50-79%: green charging / yellow discharging
  - 20-49%: yellow charging / orange discharging
  - 0-19%: orange charging / red discharging
- If not on macOS or no battery detected, skip the cell

### Tab rendering

- Delegate to Kitty's built-in `draw_tab_with_powerline` for tab
  rendering (works with any powerline style: slanted, round, angled)
- Custom code only handles the right-aligned status cells
- This keeps the tab rendering battle-tested and our code focused

### Files

| File                 | Purpose                                    |
|----------------------|--------------------------------------------|
| `tab_bar.py`         | Custom tab bar with network status cells   |
| `kitty.conf.example` | Example kitty.conf snippet to enable it    |
| `PLAN.md`            | Living plan document                       |
| `README.md`          | Installation & usage instructions          |

## Todo

- [x] Research Kitty custom tab bar API
- [x] Write plan into PLAN.md
- [x] Implement `tab_bar.py` with ping monitoring
- [x] Add Tailscale status check
- [x] Add battery percentage display
- [x] Create `kitty.conf.example` with required settings
- [ ] Test & iterate
- [ ] Write README with installation instructions
