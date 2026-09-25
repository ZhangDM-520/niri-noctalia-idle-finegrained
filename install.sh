#!/usr/bin/env bash
# nri-idle bootstrap: put the tools in place, then hand over to `nri-idle install`.
#
# Interface: this script is the *only* entry point you need to remember.
#   ./install.sh                 install everything (idle behaviours + media bridge)
#   ./install.sh --no-bridge     idle behaviours only, no media awareness
#   ./install.sh --dry-run       rehearse the machine install; write nothing
#   ./install.sh --replace-idle  take over idle tables your config already defines
#
# Where anything lives is not decided here: `nri-idle paths` owns the installed layout and
# every location below comes from it. --dry-run rehearses the whole install — same checks,
# same refusals (remedy text and exit code included), zero writes — and prints every
# would-be write as "dry-run: would ...".
#
# It is idempotent: re-running it copies the same files, never overwrites a rules file you
# have edited, and re-splices the same managed block. Every decision about *which* idle
# behaviours you get is made by config/idle.toml and by `nri-idle` — not here.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NRI="$REPO/bin/nri-idle"
WITH_BRIDGE=1
DRY_RUN=0
REPLACE_IDLE=0

for arg in "$@"; do
  case "$arg" in
    --no-bridge) WITH_BRIDGE=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --replace-idle) REPLACE_IDLE=1 ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 64 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }
# One seam for every write: `act` prints the same sentence either as a fact (after doing
# it) or as "dry-run: would ..." (doing nothing at all). Same sentence minus the prefix is
# what makes a rehearsal comparable to a run, line for line.
act() {
  local desc="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then
    say "dry-run: would $desc"
  else
    "$@"
    say "$desc"
  fi
}

# ── the layout: owned by `nri-idle paths`, echoed here so the run is auditable ───────────
step "resolving the installed layout (nri-idle paths)"
if ! PATHS_OUT="$("$NRI" paths)"; then
  echo "could not run 'nri-idle paths' — python3 >= 3.11 is required to run nri-idle" >&2
  exit 1
fi
FRAGMENT=""; TARGET=""; RULES=""; BIN_DIR=""; UNIT=""; NOCTALIA=""
while IFS= read -r line; do
  key="${line%%=*}"
  value="${line#*=}"
  case "$key" in
    fragment) FRAGMENT="$value" ;;
    target)   TARGET="$value" ;;
    rules)    RULES="$value" ;;
    bin_dir)  BIN_DIR="$value" ;;
    unit)     UNIT="$value" ;;
    noctalia) NOCTALIA="$value" ;;
  esac
done <<< "$PATHS_OUT"
if [ -z "$FRAGMENT" ] || [ -z "$TARGET" ] || [ -z "$RULES" ] ||
   [ -z "$BIN_DIR" ] || [ -z "$UNIT" ] || [ -z "$NOCTALIA" ]; then
  echo "nri-idle paths did not report the whole installed layout" >&2
  exit 1
fi
say "fragment : $FRAGMENT"
say "target   : $TARGET"
say "rules    : $RULES"
say "bin_dir  : $BIN_DIR"
say "unit     : $UNIT"
say "noctalia : $NOCTALIA"
if [ "$BIN_DIR" != "$HOME/.local/bin" ]; then
  printf '  warning: the unit hardcodes %%h/.local/bin/media-idle-bridge but bin_dir is %s\n' "$BIN_DIR" >&2
  printf '  warning: the unit is not templated — the bridge would not start from bin_dir\n' >&2
fi
if [ "$FRAGMENT" = "$REPO/config/idle.toml" ]; then
  # Only reachable when NRI_IDLE_FRAGMENT (or the resolver) points at the shipped source
  # itself: then the "user fragment" is the checkout copy, and there is nothing to seed.
  say "fragment is the shipped copy in this checkout — nothing will be seeded"
fi

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

command -v "$NOCTALIA" >/dev/null 2>&1 || { echo "noctalia not found on PATH" >&2; exit 1; }
say "noctalia: $("$NOCTALIA" --version 2>&1 | head -1)"

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
act "create dirs: $BIN_DIR $(dirname "$FRAGMENT") $(dirname "$RULES") $(dirname "$UNIT")" \
  install -d "$BIN_DIR" "$(dirname "$FRAGMENT")" "$(dirname "$RULES")" "$(dirname "$UNIT")"
act "install $REPO/bin/nri-idle -> $BIN_DIR/nri-idle" \
  install -m 0755 "$REPO/bin/nri-idle" "$BIN_DIR/nri-idle"

# The fragment is the interface the user edits, so it is never overwritten: their edits are
# the point. A diff is reported instead.
if [ -f "$FRAGMENT" ]; then
  if cmp -s "$FRAGMENT" "$REPO/config/idle.toml"; then
    say "$FRAGMENT is up to date"
  else
    say "$FRAGMENT left alone (yours differs from the shipped one — compare with:)"
    say "    diff -u $FRAGMENT $REPO/config/idle.toml"
  fi
else
  act "create $FRAGMENT (from the shipped config — edit this to change your idle policy)" \
    install -m 0644 "$REPO/config/idle.toml" "$FRAGMENT"
fi

if [ "$WITH_BRIDGE" = 1 ]; then
  act "install $REPO/bin/media-idle-bridge -> $BIN_DIR/media-idle-bridge" \
    install -m 0755 "$REPO/bin/media-idle-bridge" "$BIN_DIR/media-idle-bridge"
  act "install $REPO/systemd/media-idle-bridge.service -> $UNIT" \
    install -m 0644 "$REPO/systemd/media-idle-bridge.service" "$UNIT"
  if [ -f "$RULES" ]; then
    say "$RULES exists — left alone (see config/media-idle-rules.toml for updates)"
  else
    act "create $RULES (from the shipped rules — see config/media-idle-rules.toml for updates)" \
      install -m 0644 "$REPO/config/media-idle-rules.toml" "$RULES"
  fi
fi

step "installing the idle behaviours"
# One argument list for both the rehearsal and the run: --replace-idle and --dry-run ride
# along or not, and nothing else differs. The repo copy of the tool is the one driven here;
# the installed copy is exercised at the end, so a broken install still shows up.
INSTALL_ARGS=(install --target "$TARGET" --noctalia "$NOCTALIA")
if [ "$REPLACE_IDLE" = 1 ]; then
  INSTALL_ARGS+=(--replace-idle)
fi
if [ "$DRY_RUN" = 1 ]; then
  INSTALL_ARGS+=(--dry-run)
fi
# The refusal handler is the same on both paths: `nri-idle` says what is wrong, the remedy
# below says how to proceed, and the verdict (its exit code) is kept.
rc=0
"$NRI" "${INSTALL_ARGS[@]}" || rc=$?
if [ "$rc" -eq 2 ]; then
  cat >&2 <<'EOF'

Re-run with --replace-idle to have this tool take over the idle tables that are already in your
config (they are backed up first):

    ./install.sh --replace-idle
EOF
  exit 2
elif [ "$rc" -ne 0 ]; then
  # Not a refusal (a FAILED reload, for one, is exit 1): the tool already said why.
  exit "$rc"
fi

if [ "$WITH_BRIDGE" = 1 ]; then
  step "enabling the media bridge"
  act "systemctl --user daemon-reload" systemctl --user daemon-reload
  act "systemctl --user enable --now media-idle-bridge.service" \
    systemctl --user enable --now media-idle-bridge.service
fi

step "done"
if [ "$DRY_RUN" = 1 ]; then
  say "installed-copy verification: skipped (dry run)"
else
  # Prove the *installed* copy runs, not just the repo one.
  if "$BIN_DIR/nri-idle" status >/dev/null 2>&1; then
    say "installed-copy verification: $BIN_DIR/nri-idle status — ok"
  else
    printf '  warning: the installed copy did not report status — run %s status\n' "$BIN_DIR/nri-idle" >&2
  fi
fi
say "check the whole picture with: nri-idle status"
if [ "$WITH_BRIDGE" = 1 ]; then
  say "watch the bridge with:    journalctl --user -u media-idle-bridge -f"
fi
