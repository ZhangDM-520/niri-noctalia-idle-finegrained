# Project memory

Durable knowledge for anyone maintaining this toolset: facts that were *measured*, decisions that
should not be re-litigated without new evidence, and the traps that cost time. Read this before
changing `bin/`, `tests/`, or the shipped configs. The design rationale is in [DESIGN.md](DESIGN.md);
the user-facing syntax reference is [../MANUAL.md](../MANUAL.md); the chronological work journal is in
[NOTE.md](NOTE.md).

## 1. The two interfaces, and what each one is allowed to do

| Interface | Implementations | Rule |
| :-- | :-- | :-- |
| `nri-idle`: `render` · `install` · `status` · `uninstall` | the CLI + its flags | The idle policy is a **native Noctalia fragment** (`config/idle.toml`). The CLI injects it; it never re-encodes it. |
| `media-idle-bridge`: `--config`, `--once`, `--debug`, SIGTERM + the rules file | the daemon | The rules file answers exactly one question: *is a **video** playing?* Music never inhibits. |

Everything else — marker strings, splice internals, TOML validation, MPRIS/PipeWire plumbing — is
implementation and may change freely as long as the two interfaces keep their promises.

## 2. Facts, measured — do not "simplify" these away

* **The managed block is spliced as text, never parsed and re-serialised.** A TOML round-trip deletes
  every comment in the user's `config.toml`. This is the single most likely way to make someone hate a
  config tool.
* **Validate the *candidate* before writing.** `noctalia config validate <path>` takes a file path, so
  `install` renders to a temp file and validates that first. The failure mode must be "nothing
  happened", never "config is broken".
* **Refuse rather than guess.** Half-present markers, or `[idle]` tables that would be defined twice,
  are refused with the remedy; deleting the user's own idle text happens only under `--replace-idle`.
* **`~/.local/state/noctalia/settings.toml` overrides `config.toml`.** The Settings UI wins where they
  overlap. This toolset writes only `config.toml`, so a behaviour that "does not work" may simply be
  overridden there.
* **A behaviour absent from `noctalia config export full` is not necessarily broken.** Both timeouts
  `0`, or `enabled = false`, means deliberately absent — `status` must not report drift for those.
* **DPMS state is the only objective idle signal:** `/sys/class/drm/<connector>/dpms`. Noctalia's log
  wording can look reassuring while the panel is on; kernel DRM state cannot be faked.
* **niri folds D-Bus `org.freedesktop.ScreenSaver.Inhibit` into Wayland idle state** and owns that bus
  name, so a D-Bus inhibitor suppresses Noctalia's behaviours even though the mechanisms look
  unrelated. That is why the bridge holds *both* inhibitor kinds.

## 3. Media classification: traps that were each hit once

* **`media.role` for video is `Movie`, not `video`** (VLC). `video_roles` carries both.
* **NetEase (Electron) has an empty `xesam:url`** and is matched as *music* only by Identity — it must
  never fall through to the browser rule, where the missing URL would hit `browser_default = "video"`.
* **A permanently `running` PipeWire stream proves nothing.** Noctalia's own UI-sound stream sits in
  `state=running` forever and a muted ALSA sink reports `running`. Classification must **allow-list**,
  never deny-list; `deny_binaries` exists only to subtract from the allow-list.
* **Many clients omit `application.process.binary`** (`pw-play` exposes only `application.name` and
  `node.name`) — match across all three fields.
* **An app's own idle inhibitor cannot be removed**, only added to. The bridge can never make an
  inhibiting player leave the screen alone.

## 4. Testing: how to verify, and what each suite can reach

```bash
python3 -m unittest discover -s tests      # 32 tests at the CLI interface, no display, no Noctalia
./tests/bridge-tests.sh                    # 15 live scenarios; needs a real niri/Noctalia session
```

* The Python suite drives `nri-idle` with `FakeNoctalia` injected (`--noctalia`), so it can run
  anywhere — that seam (`RealNoctalia` / `FakeNoctalia`) is what keeps the suite honest and cheap.
* The bash suite asserts on logind's `BlockInhibited` — a state signal, not a guess about whether the
  screen *would* have dimmed. A probe that neither fires nor reports the inhibitor is recorded as
  **indeterminate**, never as a bridge failure.
* **A test that stops a service must restart it.** `bridge-tests.sh` stops `media-idle-bridge` to take
  exclusive control of the inhibitor path and restores it on `EXIT` **only if it was active before**
  (`c8ea264`). Before that fix a run silently left the desktop without media awareness.
* **Bus-protocol traps in tests:** `gdbus` gives every call a fresh connection, so an `Inhibit` in one
  process and an `UnInhibit` in another yields `invalid cookie` — pair them in one connection. And
  never parse a bus reply with `grep -oE '[0-9]+'`: a reply containing `uint32` yields `32`.
* Do not let the owner's keystrokes race a timed probe; an idle test that measures "nothing fired"
  needs the seat untouched for the whole window.

## 5. Known upstream issue: lock wakes the screen (Noctalia ≤ 5.1.0)

* **Symptom**: `dim → screen off → lock` lights the panel at the lock and replays the chain.
* **Cause**: `IdleManager::setSessionLocked()` re-armed every behaviour and ran the *resume* action of
  anything already idled; `action = "screen_off"` hard-wires a `ScreenOn` resume.
  `locked_timeout` is consulted only where a notification is (re)created, so an already-fired
  behaviour can no longer be re-targeted once the fix lands — which is intended, but kills the "small
  `locked_timeout` undoes the wake" workaround.
* **Status**: reported as [noctalia-dev/noctalia#4190](https://github.com/noctalia-dev/noctalia/issues/4190),
  fixed in [PR #4002](https://github.com/noctalia-dev/noctalia/pull/4002) (verified on niri and
  approved from this project's author, 2026-09-22). This repo deliberately vendors **no patch** — it
  only documents the bug and the stopgap ([MANUAL §9.1](../MANUAL.md#91-locking-wakes-the-screen-back-up-noctalia--510-fixed-upstream)).
  Users pinned to an unfixed build replace the `screen_off` stage with
  `command = "noctalia msg dpms-off"` (a `command` action resolves to **no** resume action).
* **Evidence method** worth reusing: shorten the chain (10/15/20 s), watch `/sys/class/drm/*/dpms`,
  compare "dpms On at the lock" before and after. Two reproductions on demand before the fix.

## 6. Decisions that should not be re-litigated without new evidence

* **The fragment is the interface** (native Noctalia syntax), not flags or a GUI — deletion test:
  delete the CLI and the fragment is still a valid Noctalia config. See DESIGN.md.
* **Timings stay dim 50 / off 70 / lock 120.** A plan once proposed lock 90 s; the owner's requirement
  is 120 s and the README/MANUAL/tests state it.
* **Two install verbs, on purpose**: `install.sh` puts the toolset on a machine; `nri-idle install`
  applies the policy into the config. The first is one-time, the second is per-edit.
* **swayidle is not deleted**: it owns only the `before-sleep` lock hook, because logind's
  `PrepareForSleep` is the one idle-adjacent signal Noctalia does not own. A second idle owner would
  double-fire on the same idle stream.
* **Fragment lives at `~/.config/nri-idle/idle.toml`**, resolved through an injected search path
  (explicit → `$NRI_IDLE_FRAGMENT` → installed → checkout). Guessing from the script path was wrong the
  moment the tool was installed.

## 7. Release checklist

1. `python3 -m unittest discover -s tests` (32) and `./tests/bridge-tests.sh` (15, 0 indeterminate) —
   the latter needs a live session and leaves `media-idle-bridge` as it found it.
2. `nri-idle status` on the host that just ran the tests: three behaviours, no drift.
3. Bump `CHANGELOG.md`; the version story is "keep a Changelog" + SemVer.
4. The shipped fragment (`config/idle.toml`) and the live one (`~/.config/nri-idle/idle.toml`) are
   normally identical; `diff` them before release.
