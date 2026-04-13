#!/bin/sh
# kitty-status-tabbar installer
# https://github.com/mpetazzoni/kitty-status-tabbar
#
# Usage:
#   curl -sL https://github.com/mpetazzoni/kitty-status-tabbar/releases/latest/download/install.sh | sh

set -e

KITTY_CONF_DIR="${HOME}/.config/kitty"
KITTY_CONF="${KITTY_CONF_DIR}/kitty.conf"
TAB_BAR="${KITTY_CONF_DIR}/tab_bar.py"
DOWNLOAD_URL="https://github.com/mpetazzoni/kitty-status-tabbar/releases/latest/download/tab_bar.py"

TAILSCALE_APP="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TAILSCALE_WRAPPER="${HOME}/.local/bin/tailscale"

info() { printf '  \033[1;34m→\033[0m %s\n' "$1"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$1"; }

echo ""
echo "  kitty-status-tabbar installer"
echo "  ─────────────────────────────"
echo ""

# ── Download tab_bar.py ─────────────────────────────────────────────

mkdir -p "${KITTY_CONF_DIR}"

if [ -f "${TAB_BAR}" ]; then
    warn "Overwriting existing ${TAB_BAR}"
fi

info "Downloading tab_bar.py..."
curl -fsSL "${DOWNLOAD_URL}" -o "${TAB_BAR}"
ok "Installed tab_bar.py to ${TAB_BAR}"

# ── Configure kitty.conf ────────────────────────────────────────────

if [ ! -f "${KITTY_CONF}" ]; then
    info "Creating ${KITTY_CONF}..."
    touch "${KITTY_CONF}"
fi

add_setting() {
    key="$1"
    value="$2"
    if grep -q "^${key}\b" "${KITTY_CONF}" 2>/dev/null; then
        current=$(grep "^${key}\b" "${KITTY_CONF}" | head -1 | awk '{print $2}')
        if [ "${current}" = "${value}" ]; then
            ok "${key} already set to ${value}"
        else
            warn "${key} is set to '${current}', changing to '${value}'"
            # Use a temp file for portability (sed -i varies across platforms)
            sed "s/^${key}[[:space:]].*/${key} ${value}/" "${KITTY_CONF}" > "${KITTY_CONF}.tmp"
            mv "${KITTY_CONF}.tmp" "${KITTY_CONF}"
            ok "${key} ${value}"
        fi
    else
        printf '\n%s %s\n' "${key}" "${value}" >> "${KITTY_CONF}"
        ok "Added ${key} ${value}"
    fi
}

add_setting "tab_bar_style" "custom"
add_setting "tab_bar_min_tabs" "1"

# ── Tailscale wrapper (macOS) ───────────────────────────────────────

if [ "$(uname)" = "Darwin" ] && [ -x "${TAILSCALE_APP}" ]; then
    # Check if tailscale is already findable
    if command -v tailscale >/dev/null 2>&1; then
        ok "Tailscale CLI already available ($(command -v tailscale))"
    elif [ -x "${TAILSCALE_WRAPPER}" ]; then
        ok "Tailscale wrapper already exists at ${TAILSCALE_WRAPPER}"
    else
        info "Tailscale app found but CLI not on PATH"
        info "Creating wrapper script at ${TAILSCALE_WRAPPER}..."
        mkdir -p "$(dirname "${TAILSCALE_WRAPPER}")"
        cat > "${TAILSCALE_WRAPPER}" << 'EOF'
#!/bin/sh
exec /Applications/Tailscale.app/Contents/MacOS/Tailscale "$@"
EOF
        chmod +x "${TAILSCALE_WRAPPER}"
        ok "Tailscale wrapper installed at ${TAILSCALE_WRAPPER}"
    fi
fi

# ── Done ────────────────────────────────────────────────────────────

echo ""
echo "  Done! Reload Kitty to activate:"
echo "    • Press ctrl+shift+f5, or"
echo "    • Restart Kitty"
echo ""
