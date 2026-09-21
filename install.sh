#!/usr/bin/env bash
# nri-idle bootstrap: put the tools in place, then hand over to `nri-idle install`.
#
# Interface: this script is the *only* entry point you need to remember.
#   ./install.sh                 install everything (idle behaviours + media bridge)
#   ./install.sh --no-bridge     idle behaviours only, no media awareness
#   ./install.sh --dry-run       say what would happen; write nothing
#   ./install.sh --replace-idle  take over idle tables your config already defines
#
# It is idempotent: re-running it copies the same files, never overwrites a rules file you have
# edited, and re-splices the same managed block. Every decision about *which* idle behaviours you
# get is made by config/idle.toml and by `nri-idle` — not here.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
RULES_DIR="$HOME/.config/media-idle-bridge"
UNIT_DIR="$HOME/.config/systemd/user"
WITH_BRIDGE=1
DRY_RUN=0
REPLACE_IDLE=0

for arg in "$@"; do
  case "$arg" in
    --no-bridge) WITH_BRIDGE=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --replace-idle) REPLACE_IDLE=1 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 64 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then say "dry-run: $*"; else "$@"; fi; }

step "checking dependencies"
PY=""
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || { echo "python3 >= 3.11 is required (tomllib)" >&2; exit 1; }
say "python: $($PY --version 2>&1)"

command -v noctalia >/dev/null 2>&1 || { echo "noctalia not found on PATH" >&2; exit 1; }
say "noctalia: $(noctalia --version 2>&1 | head -1)"

if [ "$WITH_BRIDGE" = 1 ]; then
  missing=""
  for mod in dbus gi; do
    "$PY" -c "import $mod" 2>/dev/null || missing="$missing $mod"
  done
  if [ -n "$missing" ]; then
    echo "the media bridge needs python-dbus and python-gobject:$missing" >&2
    echo "  Arch: sudo pacman -S python-dbus python-gobject" >&2
    exit 1
  fi
  say "python-dbus + python-gobject: present"
  if command -v playerctl >/dev/null 2>&1; then
    say "playerctl: present (optional, only for poking MPRIS by hand)"
  else
    say "playerctl: absent (optional)"
  fi
  command -v pw-dump >/dev/null 2>&1 || { echo "pw-dump not found (pipewire)" >&2; exit 1; }
  if grep -q 'brightnessctl' "$REPO/config/idle.toml" && ! command -v brightnessctl >/dev/null 2>&1; then
    echo "note: config/idle.toml uses brightnessctl, which is not installed" >&2
  fi
fi

step "installing the tools"
run install -d "$BIN" "$RULES_DIR" "$UNIT_DIR" "$HOME/.config/nri-idle"
run install -m 0755 "$REPO/bin/nri-idle" "$BIN/nri-idle"
say "$BIN/nri-idle"

# The fragment is the interface the user edits, so it lives in their config dir and is never
# overwritten: their edits are the point. A diff is reported instead.
FRAGMENT="$HOME/.config/nri-idle/idle.toml"
if [ -f "$FRAGMENT" ]; then
  if cmp -s "$FRAGMENT" "$REPO/config/idle.toml"; then
    say "$FRAGMENT is up to date"
  else
    say "$FRAGMENT left alone (yours differs from the shipped one — compare with:)"
    say "    diff -u $FRAGMENT $REPO/config/idle.toml"
  fi
else
  run install -m 0644 "$REPO/config/idle.toml" "$FRAGMENT"
  say "$FRAGMENT (edit this to change your idle policy)"
fi

if [ "$WITH_BRIDGE" = 1 ]; then
  run install -m 0755 "$REPO/bin/media-idle-bridge" "$BIN/media-idle-bridge"
  run install -m 0644 "$REPO/systemd/media-idle-bridge.service" "$UNIT_DIR/media-idle-bridge.service"
  if [ -f "$RULES_DIR/config.toml" ]; then
    say "$RULES_DIR/config.toml exists — left alone (see config/media-idle-rules.toml for updates)"
  else
    run install -m 0644 "$REPO/config/media-idle-rules.toml" "$RULES_DIR/config.toml"
    say "$RULES_DIR/config.toml"
  fi
fi

step "installing the idle behaviours"
# Run the repo copy of the tool, not the one just installed: in --dry-run nothing has been
# copied yet. The installed copy is exercised at the end, so a broken install still shows up.
NRI="$REPO/bin/nri-idle"
if [ "$DRY_RUN" = 1 ]; then
  "$NRI" install --dry-run --target "$HOME/.config/noctalia/config.toml"
else
  # Default to the safe path: if a hand-written idle config is already there, `nri-idle` refuses
  # and says exactly what to do, rather than deleting the user's tables on a blind first run.
  extra=""
  [ "$REPLACE_IDLE" = 1 ] && extra="--replace-idle"
  if ! "$NRI" install ${extra:+"$extra"} --target "$HOME/.config/noctalia/config.toml"; then
    cat >&2 <<'EOF'

Re-run with --replace-idle to have this tool take over the idle tables that are already in your
config (they are backed up first):

    ./install.sh --replace-idle
EOF
    exit 2
  fi
fi

if [ "$WITH_BRIDGE" = 1 ]; then
  step "enabling the media bridge"
  run systemctl --user daemon-reload
  run systemctl --user enable --now media-idle-bridge.service
fi

step "done"
if [ "$DRY_RUN" = 0 ]; then
  # Prove the *installed* copy runs, not just the repo one.
  if "$BIN/nri-idle" status >/dev/null 2>&1; then
    say "installed copy verified: $BIN/nri-idle status"
  else
    printf '  warning: the installed copy did not report status — run %s status\n' "$BIN/nri-idle" >&2
  fi
fi
say "check the whole picture with: nri-idle status"
[ "$WITH_BRIDGE" = 1 ] && say "watch the bridge with:    journalctl --user -u media-idle-bridge -f"
