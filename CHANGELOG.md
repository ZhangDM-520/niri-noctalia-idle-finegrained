# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Fixed

* **A music player no longer masks the PipeWire backstop.** `decide()` returned "no inhibit" as soon as
  any MPRIS player was music, so music plus a video playing outside MPRIS (e.g. `ffplay`) dimmed the
  screen over the film. Cross-player rule is now explicit: any video — MPRIS or backstop — inhibits;
  music never inhibits and never suppresses the backstop; video beats music when both play.
* **One pw-dump model.** The daemon and `--once` disagreed about a node event missing `info`
  (removed vs. skipped); both now share one `ingest()` model where missing `info` means the node is
  gone.
* **Rule values are validated at load.** A wrong-typed list used to iterate as *characters* at
  evaluation time (and a scalar crashed the daemon mid-evaluation, crash-looping under
  `Restart=on-failure`); now each bad value warns and falls back to its default.
  `browser_default` accepts only `"video"`, `"music"`, `"unknown"` — a typo used to silently mean
  "URL-less browsers never inhibit", the opposite of the documented fail-safe.
* `tests/bridge-tests.sh` no longer leaves the media bridge stopped. The suite stops the service to
  take exclusive control of the inhibitor path, and now restarts it on exit — but only if it was
  running before. Found the hard way: a run stopped an 11-hour-old service and left the desktop
  without media awareness until it was noticed.
* `tests/bridge-tests.sh` refuses to run (exit 2) when anything *else* holds an idle inhibitor —
  Noctalia's Caffeine mode does — instead of reporting ten false failures against a healthy bridge.
  Both of the suite's signals (swayidle's "idle inhibitor found", logind's `BlockInhibited`) are
  global, so exclusive control is now checked, not assumed.
* `tests/bridge-tests.sh` derives its session environment (`DBUS_SESSION_BUS_ADDRESS`,
  `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`) from the environment instead of hardcoding uid 1000 and
  `wayland-1`.
* **`install --dry-run` no longer drops `--replace-idle`.** `install.sh` built the real run's
  arguments and the rehearsal's separately, so the rehearsal could bless a target the real run would
  refuse (or rehearse a refusal the real run would not make). Both branches now share one argument
  list and one refusal handler.
* **`./install.sh --no-bridge` exits 0 on success** (it used to exit 1 as the last status of the
  conditional that skipped the bridge).
* **`nri-idle paths` reports the fragment you own, not the file it was run from.** On a fresh
  machine it printed the checkout's shipped `config/idle.toml`, so `install.sh` could not seed
  `~/.config/nri-idle/idle.toml`. `paths` now reports where the user's fragment lives or should be
  created (explicit `--fragment`, else `NRI_IDLE_FRAGMENT`, else `~/.config/nri-idle/idle.toml`); a
  checkout copy is a source to seed from, never the answer.

### Changed

* **A damaged managed block is refused by every command, including `uninstall`** (no `--force`):
  edits are made only where both sides parse, so a half-written block can never lose more of your
  config. This is deliberate: previously `--replace-idle` could drop END-only or interleaved tables
  that the parser could not see.
* **`status` drift is three-state and its exit codes fan out**: 0 healthy (or formatting-only
  differences, reported as info) · 1 out of date · 2 refused · 3 the shell could not be read ·
  4 the shell exports nothing or something unparseable (3 and 4 outrank 1). Previously 3 and 4 were
  both reported as drift.
* **`install`/`uninstall` exit 1 when the Noctalia reload fails** (they used to exit 0 regardless);
  `uninstall --no-reload` is honoured instead of being silently ignored.
* **Export results are typed at the port edge** (`ok`/`not_found`/`timeout`/`failed`/`unparseable`/
  `empty`), so `status` says *why* it cannot compare instead of guessing from exit codes. (There is
  no `export_for`; the old error text pointed at a function that never existed.)
* Flag × command applicability is a single declarative table (`FLAG_APPLICABILITY`): a flag that
  makes no sense for a command is a usage error (exit 2), not silence. `--replace-idle` applies to
  `render` and `install`; `--dry-run` and `--no-reload` to `install` and `uninstall`; `--fragment`,
  `--target` and `--noctalia` to everything.
* `install --dry-run` validates for real: the candidate is probed through Noctalia's validator in
  the system temp dir, with zero writes to your state.
* `install.sh` takes its whole layout from `nri-idle paths` — no `.config/` literals left in the
  script — and its dry run is a faithful rehearsal: same checks, same refusals (remedy text and exit
  code included), zero writes, every would-be write printed as `dry-run: would …`.

### Added

* **`tests/test_media_rules.py`** — 56 unit tests over the media classification core: per-player and
  cross-player precedence, the NetEase empty-URL case, chunked `pw-dump` JSON at every boundary, node
  lifecycle, rules validation fallbacks, and backstop matching. The suite needs no display, no bus and
  no PipeWire — the bridge module now imports under plain Python.
* **A `MediaSource` seam** under the classification core (`snapshot()` / `start()` / `stop()`, with
  MPRIS and PipeWire adapters): `--once` and the daemon share one decision path, and the rule that
  matched is printed with every verdict (`--once` exits non-zero when a rule was rejected).
* **MANUAL §9.1 — "locking wakes the screen back up"**, with the mechanism, the DPMS evidence, and a
  one-line stopgap for anyone pinned to a Noctalia release without the upstream fix. Also referenced
  from README and from the `lock` block in `config/idle.toml`.
* **A fifth command, `nri-idle paths`** — the installed layout as shell-greppable `key=value` lines
  (`fragment`, `target`, `rules`, `bin_dir`, `unit`, `noctalia`). One module owns the layout;
  `install.sh` consumes it, so a moved directory breaks in the tests first.
* **MANUAL §8 contract tables** — the exit-code table and the flag × command applicability matrix,
  and §10's browser classification rule corrected (the code treats a URL-bearing non-music host as
  video; `browser_default` applies only to URL-less players — the old text said the opposite).
* **Tests**: `tests/test_nri_idle.py` 32 → 95 (managed-block states, three-state drift, typed export
  fan-out, flag matrix, `paths`, command-line surface through `main(argv)`), plus
  `tests/test_install_layout.py` (16) driving `install.sh` itself, and six authored synthetic
  fixtures under `tests/fixtures/exports/` (no user data). 169 tests total, all display-free.

### Notes

- The wake-and-replay behaviour is a **Noctalia bug**, not a configuration mistake, and it is already
  fixed upstream: [noctalia-dev/noctalia#4190](https://github.com/noctalia-dev/noctalia/issues/4190)
  (report) and [noctalia-dev/noctalia#4002](https://github.com/noctalia-dev/noctalia/pull/4002) (fix,
  `mergeable`, closes #4190). Both were verified against niri from this repo: the DPMS state now stays
  `Off` across a lock, brightness is still restored on return, and `media-idle-bridge`'s two inhibitors
  still debounce correctly on release.
- This repo therefore **does not vendor a patch**. The packaging recipe that carries the fix locally
  lives with the package, not here.

## [1.0.0] — 2026-09-21

### Added

- `nri-idle` (render · install · status · uninstall): injects a marker-delimited idle block into an
  existing `~/.config/noctalia/config.toml`, validating the candidate with `noctalia config validate`
  before writing, and refusing rather than guessing when the target already has hand-written `[idle]`
  tables (use `--replace-idle`).
- `media-idle-bridge`: holds an idle inhibitor while **video** plays (logind `idle` block + a niri
  `ScreenSaver.Inhibit` cookie) and releases both when playback stops, so music runs the normal chain
  while video suppresses it entirely.
- `config/idle.toml` — the idle policy as an editable file in unmodified Noctalia syntax
  (shipped: dim 50 s, screen off 70 s, lock 120 s).
- `MANUAL.md` — the Noctalia idle syntax reference, measured against upstream source, plus the media
  rule tables and the observed identities (NetEase, Zen, mpv, VLC).
- 32 interface tests for the CLI (with `noctalia` injected) and a live media-classification suite for
  the bridge.
