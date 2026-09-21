# MANUAL

Everything this repo assumes about **Noctalia's idle syntax**, plus a reference for the **media
rules** the bridge reads. Written against Noctalia **v5.1.0** (`noctalia-dev/noctalia`), and every
statement marked *(measured)* was verified on a live niri session rather than copied from docs.

If you already know Noctalia's idle config, jump to [§7 Recipes](#7-recipes) and [§10 Media rules](#10-media-rules).

---

## 1. The mental model

An *idle behaviour* is three things:

| Piece | Meaning |
| :-- | :-- |
| a **name** | `[idle.behavior.dim]` — arbitrary; it is how you refer to the behaviour in `behavior_order` and in the logs |
| a **timeout** | seconds of *no input* before it fires. Fractional values allowed; `0` disables |
| an **action** | a native thing Noctalia does (`lock`, `screen_off`, …), or `command` to run your own |

Noctalia asks the compositor (via `ext_idle_notifier_v1`) to be told when the seat has been idle for
that long. Behaviours are **independent**: nothing chains, and nothing is cancelled by a *later*
behaviour firing. If `dim` is at 50 s and `screen-off` at 70 s, both timers start from the same
"last input" moment, so you see a dim at 50 s and monitors off 20 s later.

**Inhibitors beat everything.** While anything holds an idle inhibitor — a video player, a
presentation, or this repo's bridge — Noctalia's behaviours do not fire at all, no matter how long
the timeout. That single sentence is the reason the media bridge exists.

## 2. Where the config lives, and which file wins

| Path | Role |
| :-- | :-- |
| `~/.config/noctalia/config.toml` | The config file. **This is what `nri-idle` writes into.** |
| `~/.local/state/noctalia/settings.toml` | What the in-app Settings UI writes. **It overrides the config file.** |

Both are read; the Settings UI's values win where they overlap. *(measured)* If you configure idle
behaviours through the Settings UI and also in `config.toml`, the UI wins — so pick one. This repo
edits only `config.toml` (inside its managed block) and never touches `settings.toml`, which keeps a
single source of truth: yours to check with

```bash
noctalia config export full | sed -n '/^\[idle/,/^\[[^i]/p'   # what is actually in effect
```

Two commands matter day to day:

```bash
noctalia config validate            # parse the live config; exit non-zero on error
noctalia msg config-reload          # make the running shell re-read it
```

`noctalia config validate <path>` accepts a *candidate file*, which is how `nri-idle install` proves
a change is good **before** writing it.

## 3. `[idle]` — the two top-level keys

```toml
[idle]
behavior_order = ["dim", "screen-off", "lock"]
pre_action_fade_seconds = 0.0
```

| Key | Type | Default | Meaning |
| :-- | :-- | :-- | :-- |
| `behavior_order` | list of strings | — | The order behaviours are evaluated and listed in. Names not listed are appended; **unknown names are ignored**, so a typo here is silent — `nri-idle` warns about it. |
| `pre_action_fade_seconds` | number | `0` | Seconds of fade on a full-monitor overlay *before any behaviour's command runs*. Parsed 0–120; the Settings stepper offers 0–30. `0` runs the command the instant idle is reported. |

The fade is a layer-shell overlay covering the whole monitor (bar included) and is click-through: if
you touch the machine mid-fade, the pending action is **cancelled**.

> **The fade is global and it shifts every timing.** With `pre_action_fade_seconds = 2.0`, a 50/70/120
> config behaves as 52/72/122. `nri-idle` therefore ships `0.0` — if you want a fade before locking,
> either accept the shift or move the lock timeout 2 s earlier.

## 4. `[idle.behavior.<name>]` — the behaviour keys

```toml
[idle.behavior.dim]
timeout = 50
action = "command"
command = "brightnessctl -s && brightnessctl set 30%"
resume_command = "brightnessctl -r"
```

| Key | Type | Default | Meaning |
| :-- | :-- | :-- | :-- |
| `action` | string | `"command"` | See [§5](#5-actions). Trimmed on read. |
| `timeout` | number | `0` | Seconds before it fires, from the same "last input" clock as every other behaviour. Fractions allowed (`0.3`). |
| `locked_timeout` | number | `0` | If the session is locked **and** this is `> 0`, it is used *instead of* `timeout`. |
| `enabled` | bool | `true` | Off switches the behaviour out without deleting it. |
| `command` | string | `""` | Only for `action = "command"`: a shell command run when idle. Use `noctalia msg …` when it needs to call Noctalia. |
| `resume_command` | string | `""` | Optional; runs when activity resumes. |
| `lock_before_suspend` | bool | `true` | For `action = "suspend"`. `false` suspends without locking. |

### Zero and locked-only timeouts *(measured against the source)*

Two rules interact, and they are worth knowing exactly because both are easy to get wrong:

* `timeout = 0` **and** `locked_timeout = 0` → the behaviour never runs. (`noctalia` logs
  `disabled by zero timeout`.)
* `timeout = 0` with `locked_timeout > 0` → it runs **only while the session is locked**. This is how
  you get "be gentler after locking" without a second idle owner.
* Negative or non-finite values are rejected with a warning and the behaviour is dropped.

## 5. Actions

| `action` | What happens | On return |
| :-- | :-- | :-- |
| `lock` | Locks the session (Noctalia's lock screen, via `ext_session_lock_v1`). | — |
| `screen_off` | Turns monitors off (DPMS). | Monitors come back automatically. `resume_command` runs *after* they are restored. |
| `suspend` | Suspends the system. | — |
| `lock_and_suspend` | Locks first, then suspends. | — |
| `command` | Runs `command` in a shell. | `resume_command` runs. |

Details that bite:

* `action = "suspend"` with `lock_before_suspend = true` is **normalised to `lock_and_suspend`** —
  the two spellings are literally equivalent.
* An action string that is not one of the above is treated as `command` (whatever `command` holds).
  A behaviour that resolves to *no* action is ignored with `ignored: needs an action`.
* `screen_off` restores monitor power even if the restore reports a failure, and its
  `resume_command` still runs.
* *(measured)* A `command` behaviour's `resume_command` does run after a **later** `screen_off`
  behaviour has blanked the monitors — the ordering is per behaviour, not first-come-wins.

## 6. What stops a behaviour from firing

1. **Activity** (any key or pointer movement) resets every timer.
2. **An idle inhibitor.** Wayland idle-inhibit surfaces and the D-Bus
   `org.freedesktop.ScreenSaver.Inhibit` interface both count. Noctalia reads logind's
   `BlockInhibited` and its own D-Bus screen-saver clients, and while an inhibitor is held the
   behaviours simply do not run.
3. **Release re-arms from the release moment.** *(measured, via smithay's `set_is_inhibited(false)`
   → `reinsert_timer`)*: when the inhibitor goes away the countdown **restarts**, so pausing a video
   never blanks the screen a tick later, and after a two-hour film you get a fresh 50 s.

Point 3 is why "just wrap the timeout command in a `playerctl` check" does not work: on most
compositors the skipped action never happens at all after the media stops, and the screen stays lit
until you press a key.

## 7. Recipes

**Dim → off → lock (what this repo ships)**

```toml
[idle]
behavior_order = ["dim", "screen-off", "lock"]
pre_action_fade_seconds = 0.0

[idle.behavior.dim]
timeout = 50
action = "command"
command = "brightnessctl -s && brightnessctl set 30% && brightnessctl --device='asus::kbd_backlight' -s && brightnessctl --device='asus::kbd_backlight' set 0"
resume_command = "brightnessctl -r && brightnessctl --device='asus::kbd_backlight' -r"

[idle.behavior.screen-off]
timeout = 70
action = "screen_off"

[idle.behavior.lock]
timeout = 120
action = "lock"
```

`brightnessctl -s` saves the current level and `-r` restores it, so dimming never clobbers a level
you set by hand. Drop the `asus::kbd_backlight` clauses on machines without that device.

**Suspend after 15 minutes, locking first**

```toml
[idle.behavior.suspend]
timeout = 900
action = "lock_and_suspend"
```

**A dim that only applies once locked**

```toml
[idle.behavior.locked-dim]
timeout = 0          # never while you are there
locked_timeout = 30  # 30 s after locking
action = "command"
command = "brightnessctl set 10%"
resume_command = "brightnessctl -r"
```

**Monitors off through an explicit IPC call** (instead of the native action)

```toml
[idle.behavior.off]
timeout = 660
action = "command"
command = "noctalia msg dpms-off"
resume_command = "noctalia msg dpms-on"
```

**A fade before locking** — remember it shifts *every* behaviour by the same amount:

```toml
[idle]
pre_action_fade_seconds = 2.0
```

## 8. How to check what is really happening

```bash
nri-idle status                 # fragment vs. what the running shell resolved, plus drift
noctalia config export full     # the parsed config, with defaults filled in
noctalia config validate        # is the file parseable at all?
tail -f ~/.cache/noctalia/noctalia.log | grep '\[idle\]'
```

The log is the authority. Registration and firing appear as:

```
[idle] registered idle behavior 'dim' timeout=50s locked_timeout=0s
[idle] idle behavior 'lock' triggered
[idle] idle behavior 'dim' resumed
```

**A file being correct is not the same claim as the running process being correct.** Editing
`config.toml` does nothing until `noctalia msg config-reload` runs — which is exactly what
`nri-idle install` does for you. If the log shows no new `registered idle behavior` line, your change
is on disk and not in effect.

## 9. Pitfalls, all measured

* **The fade is global** — see §3.
* **`settings.toml` overrides `config.toml`** — see §2.
* **`behavior_order` typos are silent** — unknown names are ignored, not rejected.
* **Disabled behaviours are absent from `noctalia config export full`.** Do not read "missing from the
  export" as "broken": a behaviour with `enabled = false`, or both timeouts `0`, is supposed to be
  absent. (`nri-idle status` only complains about drift in behaviours that *should* be running.)
* **Hand-written idle tables plus a managed block means a redefinition error.** TOML cannot define
  `[idle]` twice, so `nri-idle` refuses and tells you to use `--replace-idle` rather than producing a
  config Noctalia will reject.
* **niri owns `org.freedesktop.ScreenSaver`** and folds that D-Bus `Inhibit` into the Wayland
  idle-notify state, so a D-Bus inhibitor suppresses Noctalia's behaviours even though the two
  mechanisms look unrelated.
* **A permanently "running" audio stream is not evidence of playback.** Noctalia's own UI-sound stream
  sits in `state=running` forever, and a muted ALSA sink still reports `running`. Anything that
  decides "media is playing" from PipeWire must allow-list, never deny-list. (See §10.)

## 10. Media rules

The bridge (`bin/media-idle-bridge`) reads `~/.config/media-idle-bridge/config.toml` and answers one
question: *is a **video** playing right now?* If yes it holds an idle inhibitor; if no it releases it.
Music deliberately does **not** inhibit — it just lets the chain run.

Rules are evaluated in this order, first match wins:

1. A **Playing** MPRIS player whose Identity matches `music_players` → **music**.
2. A **Playing** MPRIS player whose Identity matches `video_players` → **video**.
3. A **Playing** browser (`browser_players`, matched on Identity or bus name):
   * `xesam:url` matches `music_hosts` → **music**;
   * otherwise → `browser_default`.
4. **PipeWire backstop**: a running `Stream/Output` node whose `application.process.binary` matches
   `video_binaries`, or whose `media.role` matches `video_roles`, and which does not match
   `deny_binaries` → **video**.

```toml
video_players   = ["mpv", "vlc", "celluloid", "haruna", "totem", "smplayer", "mplayer"]
music_players   = ["netease", "spotify", "cider", "feishin", "amberol", "gapless", "lollypop"]
browser_players = ["firefox", "zen", "librewolf", "waterfox", "nightly", "chromium", ...]
browser_default = "video"
music_hosts     = ["music.youtube.com", "open.spotify.com", "soundcloud.com", ...]
video_binaries  = ["mpv", "vlc", "celluloid", "haruna", "totem", "smplayer", "ffplay", "mplayer", "gst-play-1.0"]
video_roles     = ["video", "movie"]
deny_binaries   = ["noctalia", "easyeffects", "speech-dispatcher", "sd_dummy", ...]
```

### Measured identities, so you know what to match on

| App | MPRIS | PipeWire | Why it works |
| :-- | :-- | :-- | :-- |
| **NetEase Cloud Music** (Electron) | bus `org.mpris.MediaPlayer2.chromium.instance<pid>`, Identity contains `netease`, **`xesam:url` empty** | node `netease-cloud-music-web-player`, `bin=electron`, role empty | matched as *music* by identity — it must never reach rule 3, because with no URL it would default to `browser_default = "video"` |
| **Zen** (Firefox fork) | Identity `Mozilla zen-browser`, bus `firefox.instance_<pid>` | node `Nightly`, `bin=firefox` | rules 3–4: URL decides (`open.spotify.com` → music, `youtube.com/watch` → video). Browsers are deliberately **not** in `video_binaries` — they are ambiguous |
| **VLC** | Identity `VLC media player` | node `VLC media player (LibVLC …)`, `bin=vlc`, **`media.role="Movie"`** | rule 2 (`vlc` in `video_players`) |
| **mpv** | Identity `mpv` | `bin=mpv` | rule 2 |

### Traps

* **`media.role` for video is `Movie`, not `video`** — hence both values in `video_roles`.
* **Many clients omit `application.process.binary`** (`pw-play` exposes only `application.name` and
  `node.name`), so match across all three fields, not just the binary.
* **`browser_default = "video"`** means a browser playing *something* with no `xesam:url` keeps the
  screen awake. Set it to `"unknown"` if you'd rather a missed video than an extra "stay awake".
* **An app's own inhibitor cannot be overridden.** If a player inhibits idle itself, no policy can
  un-inhibit it; the bridge can only *add* inhibitors.
