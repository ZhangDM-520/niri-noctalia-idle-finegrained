# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added

- **MANUAL §9.1 — "locking wakes the screen back up"**, with the mechanism, the DPMS evidence, and a
  one-line stopgap for anyone pinned to a Noctalia release without the upstream fix. Also referenced
  from README and from the `lock` block in `config/idle.toml`.

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
