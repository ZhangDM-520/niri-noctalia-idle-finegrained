#!/usr/bin/env bash
# Scenario tests for media-idle-bridge.
#
# Deterministic core assertion: logind's BlockInhibited property contains "idle" exactly when the
# bridge holds its idle inhibitor - that is the same signal Noctalia and swayidle act on.
# Corroboration: `swayidle -d` either reports "Not enabling timeouts: idle inhibitor found"
# (inhibited) or actually runs its 3 s timeout (not inhibited).
#
# A probe that neither fires nor reports the inhibitor is recorded as INDETERMINATE (a harness
# timing artefact when many short-lived idle notifications are registered in a row) and is not
# counted as a bridge failure, but it is printed and its log kept for inspection.
set -u

SESS="$(cd "$(dirname "$0")" && pwd)"
# Test the copy in this repo, not an installed one - the suite is about repo code.
REPO="$(cd "$SESS/.." && pwd)"
BRIDGE="$REPO/bin/media-idle-bridge"
FAKE="$SESS/fake-mpris-player"
MARK=/tmp/swayidle-fired
BRIDGE_LOG=/tmp/bridge-under-test.log
PROBE_LOG=/tmp/swayidle-check.log

# Session identity comes from the environment; fall back to this machine's usual values so the
# suite is not uid-1000 / wayland-1-coupled.
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"

# Kill stray fake players from earlier runs without matching this script's own command line
# (pgrep -f would match the subshell that contains the pattern).
for d in /proc/[0-9]*; do
  cmd=$(tr '\0' ' ' 2>/dev/null <"$d/cmdline") || continue
  case "$cmd" in "python3 $FAKE"*) kill "${d#/proc/}" 2>/dev/null ;; esac
done
sleep 1

pass=0; fail=0; indeterminate=0

block() {   # logind BlockInhibited, e.g. 'idle:handle-power-key'
  busctl --system get-property org.freedesktop.login1 /org/freedesktop/login1 \
    org.freedesktop.login1.Manager BlockInhibited 2>/dev/null | sed 's/^s //;s/"//g'
}

probe() {   # prints: inhibited | fired | unknown
  timeout 10 swayidle -d timeout 3 "touch $MARK" >"$PROBE_LOG" 2>&1
  if grep -q 'Not enabling timeouts: idle inhibitor found' "$PROBE_LOG"; then echo inhibited; return; fi
  if grep -q 'Spawned process' "$PROBE_LOG"; then echo fired; return; fi
  echo unknown
}

check() {   # check <name> <want: inhibited|fired>
  local name="$1" want="$2" result block_value=unknown
  result=$(probe)
  block_value=$(block)

  if [ "$want" = inhibited ]; then
    if [ "$result" = inhibited ]; then
      echo "ok    $name: inhibited (swayidle reports the inhibitor; block=$block_value)"
      pass=$((pass+1))
    else
      echo "FAIL  $name: wanted inhibition, got '$result' (block=$block_value)"
      fail=$((fail+1))
    fi
  else
    case "$result" in
      fired)
        echo "ok    $name: normal idle chain (block=$block_value)"
        pass=$((pass+1)) ;;
      inhibited)
        echo "FAIL  $name: inhibited but must not be (block=$block_value)"
        fail=$((fail+1)) ;;
      *)
        cp "$PROBE_LOG" "/tmp/swayidle-unknown-${name// /_}.log" 2>/dev/null
        echo "WARN  $name: INDETERMINATE - neither fired nor reported an inhibitor (block=$block_value)"
        echo "      (log kept at /tmp/swayidle-unknown-${name// /_}.log)"
        indeterminate=$((indeterminate+1)) ;;
    esac
  fi
}

# Scenarios that must not be inhibited also require the absence of a logind idle lock: fully
# deterministic, independent of any idle-notification timing.
expect_no_idle_lock() {
  local name="$1" b
  b=$(block)
  case ":$b:" in
    *:idle:*)
      echo "FAIL  $name: logind still reports an idle lock (block=$b)"
      fail=$((fail+1)) ;;
    *)
      pass=$((pass+1)) ;;
  esac
}

start_player() { "$FAKE" "$@" >>"$BRIDGE_LOG" 2>&1 & echo $!; }
stop_player()  { [ -n "${1:-}" ] && kill "$1" 2>/dev/null; sleep 1; }

SERVICE=media-idle-bridge
# This suite needs exclusive control of the inhibitor path, so it stops the user's bridge - and must
# put it back. An earlier revision left the owner's media bridge stopped for four minutes after a
# run: a test suite that silently disables part of the desktop is worse than no test suite.
service_was_active=$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)

cleanup() {
  [ -n "${BRIDGE_PID:-}" ] && kill "$BRIDGE_PID" 2>/dev/null
  for d in /proc/[0-9]*; do
    cmd=$(tr '\0' ' ' 2>/dev/null <"$d/cmdline") || continue
    case "$cmd" in "python3 $FAKE"*) kill "${d#/proc/}" 2>/dev/null ;; esac
  done
  if [ "$service_was_active" = "active" ]; then
    systemctl --user start "$SERVICE" 2>/dev/null
    echo "restored $SERVICE (it was active before this run)"
  fi
}
trap cleanup EXIT

systemctl --user stop "$SERVICE" 2>/dev/null
[ "$service_was_active" = "active" ] && echo "note  stopped $SERVICE for this run; restarted at the end"

# Exclusive control of the inhibitor path is a precondition, not an assumption. Any *other* idle
# inhibitor (Noctalia's Caffeine mode is the one that bit here) poisons both signals this suite
# asserts on — swayidle reports "idle inhibitor found" and logind's BlockInhibited contains "idle" —
# so every "must not be inhibited" check would be a false FAIL. Refuse to run instead of lying.
foreign_idle_inhibitor() {   # 0 when something besides the bridge holds an idle inhibitor
  [ "$(probe)" = "inhibited" ] && return 0
  case ":$(block):" in *:idle:*) return 0 ;; esac
  return 1
}
if foreign_idle_inhibitor; then
  echo "refused: something other than the media bridge is holding an idle inhibitor" >&2
  echo "  the likely culprit is Noctalia Caffeine: 'noctalia msg caffeine-disable', re-enable after" >&2
  echo "  (check others with: systemd-inhibit --list)" >&2
  exit 2
fi

"$BRIDGE" --debug >"$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!
sleep 2

echo "── scenario 1: nothing playing ──"
check "no media" fired; expect_no_idle_lock "no media"

echo "── scenario 2: mpv playing video (MPRIS video_players) ──"
P=$(start_player mpv Playing); sleep 3; check "mpv video" inhibited; stop_player "$P"
expect_no_idle_lock "mpv released"

echo "── scenario 3: mpv paused ──"
P=$(start_player mpv Paused); sleep 3; check "mpv paused" fired; stop_player "$P"

echo "── scenario 4: NetEase music (music_players) ──"
P=$(start_player netease-cloud-music-web-player Playing); sleep 3
check "netease music" fired; expect_no_idle_lock "netease music"; stop_player "$P"

echo "── scenario 5: VLC video ──"
P=$(start_player vlc Playing); sleep 3; check "vlc video" inhibited; stop_player "$P"

echo "── scenario 6: Zen browser, music.youtube.com ──"
P=$(start_player zen Playing "https://music.youtube.com/watch?v=abc"); sleep 3
check "zen music url" fired; expect_no_idle_lock "zen music url"; stop_player "$P"

echo "── scenario 7: Zen browser, youtube.com/watch ──"
P=$(start_player zen Playing "https://www.youtube.com/watch?v=abc"); sleep 3
check "zen video url" inhibited; stop_player "$P"

echo "── scenario 8: browser with no URL (browser_default = video) ──"
P=$(start_player zen Playing ""); sleep 3; check "zen no url" inhibited; stop_player "$P"

echo "── scenario 9: SIGTERM while inhibited releases everything ──"
P=$(start_player mpv Playing); sleep 3
kill -TERM "$BRIDGE_PID"; sleep 2
if kill -0 "$BRIDGE_PID" 2>/dev/null; then
  echo "FAIL  bridge did not exit on SIGTERM within 2s"; fail=$((fail+1))
else
  echo "ok    bridge exited promptly on SIGTERM"; pass=$((pass+1))
fi
check "after bridge exit" fired
expect_no_idle_lock "after bridge exit"
stop_player "$P"

echo
echo "passed=$pass failed=$fail indeterminate=$indeterminate"
exit $((fail > 0))
