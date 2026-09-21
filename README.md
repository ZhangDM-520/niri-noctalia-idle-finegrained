# niri-noctalia-idle-finegrained

Fine-grained idle policy for **niri + Noctalia**, including the part most setups get wrong: **your
idle timers should know the difference between music and video.**

* **Dim at 50 s, screen off at 70 s, lock at 120 s** — or whatever timings you want.
* **Video playing → nothing fires.** No dim, no blank, no lock, while a film is on.
* **Music playing → the normal chain runs.** Music is not "media"; it must not keep your screen awake.
* Your policy lives in **one editable file in Noctalia's own syntax** — no bespoke format to learn.

Verified on niri 26.04 (v26.04-114), Noctalia 5.1.0, swayidle 1.9.0, Wayland.

---

## Install

```bash
git clone https://github.com/ZhangDM-520/niri-noctalia-idle-finegrained
cd niri-noctalia-idle-finegrained
./install.sh --dry-run     # see exactly what it will do
./install.sh               # do it
nri-idle status            # confirm what is now live
```

If your Noctalia config already has hand-written idle behaviours, `install.sh` will refuse rather than
guess, and tell you to re-run `./install.sh --replace-idle` (your config is backed up first).

Requirements: `python3` ≥ 3.11, `noctalia` on `PATH`, PipeWire (`pw-dump`). For the media bridge:
`python-dbus`, `python-gobject`. Optional: `playerctl` (handy for inspecting MPRIS), `brightnessctl`
(only if your fragment uses it, as the shipped one does).

## What it does to your system

| Path | What |
| :-- | :-- |
| `~/.config/nri-idle/idle.toml` | **Yours to edit.** The idle behaviours, in Noctalia syntax. Never overwritten by re-installing. |
| `~/.config/noctalia/config.toml` | Your config, with a delimited `nri-idle managed block` spliced in. Everything else — comments, ordering, your other keys — is left byte-for-byte alone. |
| `~/.local/bin/nri-idle` | The CLI (`render` · `install` · `status` · `uninstall`). |
| `~/.local/bin/media-idle-bridge` | The daemon that holds idle inhibitors while video plays. |
| `~/.config/media-idle-bridge/config.toml` | The media rules (which players count as video, which hosts count as music). Created only if absent. |
| `~/.config/systemd/user/media-idle-bridge.service` | Runs the bridge as part of your graphical session. |

Nothing else is touched, and every write is preceded by a `*.bak-<timestamp>` copy.

## Choosing your idle behaviour

Edit `~/.config/nri-idle/idle.toml`, then apply it:

```bash
nri-idle render            # preview the merged result, writes nothing
nri-idle install           # validate, write, reload the running shell
nri-idle status            # fragment vs. what the running shell actually resolved
nri-idle uninstall         # remove the managed block, keep everything else
```

Add a stage by adding a block; change timings by changing `timeout`; keep something around switched
off with `enabled = false`:

```toml
[idle.behavior.off]
timeout = 70
action = "screen_off"

[idle.behavior.suspend]
timeout = 900
action = "lock_and_suspend"
enabled = false
```

The full syntax — every key, every action, the zero/locked-timeout rules, and the traps — is in
**[MANUAL.md](MANUAL.md)**. You do not need to know any of it to use the shipped policy.

## Why a daemon is needed

No idle engine on Wayland can tell music from video. niri owns `org.freedesktop.ScreenSaver` and folds
that D-Bus `Inhibit` into the compositor's idle-notify state, so what reaches Noctalia (or swayidle) is
a single bit: *"an inhibitor exists"*. Every media-aware setup you have seen is really something
translating *"is this video?"* into that bit.

`media-idle-bridge` is that translator. While a **video** plays it holds two inhibitors at once —
logind `systemd-inhibit --what=idle --mode=block` (what Noctalia reads via `BlockInhibited`) and a niri
`ScreenSaver.Inhibit` cookie (belt and braces) — and releases both the moment playback stops. On
release the countdown **restarts from that moment**, so pausing a video never blanks the screen, and
after a long film you get a fresh 50 s.

## Verify it yourself

```bash
nri-idle status                                  # requested vs. running, plus drift
noctalia config export full | sed -n '/^\[idle/,/^\[[^i]/p'
journalctl --user -u media-idle-bridge -f        # "idle inhibited (mpris video: ...)" / "idle released"
tail -f ~/.cache/noctalia/noctalia.log | grep '\[idle\]'
```

Then play a video and watch nothing happen, and play music and watch the screen dim anyway.

## Tests

```bash
python3 -m unittest discover -s tests -v     # 32 tests, no display and no Noctalia required
./tests/bridge-tests.sh                      # end-to-end media classification (needs a live session)
```

The Python suite runs at the CLI's interface with the `noctalia` binary injected, so it needs neither a
running shell nor a compositor. The bridge suite drives a synthetic MPRIS player and asserts on logind's
`BlockInhibited` — a state signal, not a 10-second guess about whether the screen *would* have dimmed.

## Limitations, stated plainly

* **An app's own inhibitor cannot be overridden.** Some players inhibit idle themselves; the bridge can
  only *add* inhibitors, never remove someone else's. Music played in a browser that inhibits for
  audio-only playback will keep the screen awake — that is the app's choice, not this tool's.
* **`browser_default = "video"`.** A browser playing something with no `xesam:url` is treated as video
  (keeping the screen awake) because a missed video is worse than an extra stay-awake. Switch it to
  `"unknown"` in the rules file to invert that trade-off.
* **The `.NET`/Electron MPRIS mess is real.** Apps expose wildly different identities; the shipped
  tables cover the cases measured here (NetEase, Zen, mpv, VLC) and are meant to be edited.

## Layout

```
bin/nri-idle               the CLI: render · install · status · uninstall
bin/media-idle-bridge      the media → inhibitor translator
config/idle.toml           the fragment that gets injected (your interface)
config/media-idle-rules.toml   media classification rules
systemd/media-idle-bridge.service
tests/test_nri_idle.py     interface tests, Noctalia injected
tests/bridge-tests.sh      live media classification suite
docs/DESIGN.md             why the seams are where they are
MANUAL.md                  Noctalia idle syntax + media rules reference
```

## Licence

MIT — see [LICENSE](LICENSE).
