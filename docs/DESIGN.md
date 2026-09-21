# Design

Why this repo is shaped the way it is. Vocabulary follows the "deep modules" framing: a **module** has
an **interface** (everything a caller must know) and an implementation; **depth** is leverage at that
interface; a **seam** is where the interface lives; an **adapter** is a thing that fills it.

## The one design decision that matters

*How does a user say which idle behaviours they want?*

| Candidate interface | Depth |
| :-- | :-- |
| **A. The native fragment is the interface** — `config/idle.toml` is real Noctalia TOML; `nri-idle install` injects it | One file the user already knows how to read. Everything Noctalia can express is expressible, and nothing needs documenting twice. The CLI stays 4 commands wide. |
| **B. Flags are the interface** — `nri-idle --dim 50 --off 70 --lock 120` | Shallow: a second vocabulary that re-encodes a subset of Noctalia's. Cannot express `resume_command`, `locked_timeout`, `lock_before_suspend`, the fade, or `command` actions without growing a flag for each. |
| **C. A GUI/wizard** | Reimplements the Settings UI Noctalia already ships, and adds a surface that can drift from it. |

**A wins**, and it is what shipped. The test of the choice: delete `nri-idle` and see what happens. The
fragment is still a complete, valid Noctalia config — nothing vanishes but convenience. That is the
deletion test passing.

## The modules

### `nri-idle` — the deep one

```
interface:   render | install | status | uninstall
             (+ --fragment, --target, --dry-run, --replace-idle, --no-reload, --noctalia)
hides:       TOML validation, marker-block splicing, comment preservation, backup,
             candidate validation, reload, drift detection, fragment resolution
```

Four commands, and `render` carries most of the test surface: **install is render + validate + write +
reload**, so testing `render` to death is testing the interesting logic with no filesystem risk.

Invariants the interface promises:

1. **Install either succeeds or changes nothing.** The merged candidate is validated by Noctalia *before*
   the target is written. A bad fragment therefore costs you a refusal, never a broken config.
2. **Text outside the managed block is never rewritten.** The block is spliced as text. A TOML
   parse-and-re-serialise would silently delete every comment in the user's file — the single most
   likely way to make someone hate a config tool.
3. **Refuse rather than guess.** One marker line without the other, or idle tables that would be defined
   twice, are refused with the remedy. Deleting the user's own idle text happens only under an explicit
   `--replace-idle`.
4. **The fragment is scoped.** A fragment defining `[shell]` is refused, because it would be spliced in
   verbatim and override unrelated keys.

### `media-idle-bridge` — the other deep one

```
interface:   --config FILE, --once, --debug, SIGTERM
hides:       MPRIS PropertiesChanged + NameOwnerChanged, a streaming JSON-splitting
             pw-dump --monitor parser, rule classification, two inhibitors held in
             lock-step, graceful release, crash-proof config loading
```

Its interface is an *argument list and a rules file*. `--once` exists precisely so a human can ask
"what do you think is playing?" without reading code.

### `config/idle.toml` — a data interface

Worth naming as a module: it is the thing users edit, and it is deliberately *not* a bespoke format,
so its "documentation" is Noctalia's own.

## Seams, and why each one exists

| Seam | Adapters | Justified? |
| :-- | :-- | :-- |
| **`noctalia` binary** (validate / reload / export) | `RealNoctalia` (subprocess), `FakeNoctalia` (records, can fail on demand) | **Yes** — two adapters. This is what makes the whole suite run without a compositor, a display or Noctalia installed. |
| **Fragment location** | `resolve_fragment(script_dir, home, env)`, all injected | **Yes** — installed config dir, a git checkout, `$NRI_IDLE_FRAGMENT`, or `--fragment`; and it is a pure function, so all four orders are tested. |
| **Target path / fragment path** | `--target`, `--fragment` | Testability, and it makes the tool usable on someone else's config. |
| **Filesystem** | real temp dirs in tests | **No invented seam.** A real temp directory is the local substitute; an abstraction over "write a file" would be indirection with one adapter. |

Dependency categories: the `noctalia` binary is a *true external* (mocked behind a port); the filesystem
is *local-substitutable* (real temp dirs, no adapter); classification logic is *in-process pure*.

## Internal seams (not exposed)

`idle_regions()` and `strip_idle()` exist for the `--replace-idle` migration path and are tested
indirectly, through `render --replace-idle` and through the TOML result being parseable. They are not
part of the interface and are not meant to be.

## What the tests are, and what they replaced

`tests/test_nri_idle.py` asserts behaviour *at the CLI's interface*: "comments survive", "installing
twice is a no-op", "a rejected candidate never touches the target", "uninstall round-trips to the
original bytes", "a disabled behaviour is not drift". None of them can break because the splicing
internals change.

They found three real bugs on first contact with reality, which is the argument for the seam:

1. Rendering into a target that **already** had hand-written `[idle]` tables produced a file Noctalia
   rejects (`cannot redefine existing table 'idle'`) → refusal + `--replace-idle`.
2. `status` reported drift for behaviours that were deliberately `enabled = false` and correctly absent
   from Noctalia's export → drift now only covers behaviours that *should* be registered.
3. The installed copy resolved its fragment relative to itself (`~/.local/config/idle.toml`) → the
   fragment is now installed to `~/.config/nri-idle/idle.toml` and resolution is a search path.

Only #3 needed a live system to appear, and it appeared because `install.sh` verifies the *installed*
binary rather than the one in the checkout.

## Rejected alternatives, for the record

* **A wrapper around each Noctalia timeout command** (`playerctl status | grep -q Playing || …`). The
  cheapest thing to write and the most common advice. Rejected: on this compositor a skipped action
  never happens after the media stops, so the screen stays lit until the next keypress. It also cannot
  distinguish music from video, which was the original complaint.
* **`wayland-pipewire-idle-inhibit` as the whole answer.** Per audio *node*, so Zen music and Zen video
  are indistinguishable, and a permanently-running UI-sound stream is a latch risk. It stays a sane
  fallback if the bridge ever proves fragile — which is why the rules file is documented rather than
  hidden.
* **Driving `noctalia msg caffeine-enable/disable` from the bridge.** Least code, but caffeine is a
  user-facing toggle (shared state with the control-centre shortcut) and logind-only — invisible to
  anything reading Wayland idle-inhibit.
* **Dropping swayidle entirely.** Appears cleaner, but logind's `PrepareForSleep` is the one
  idle-adjacent signal Noctalia does not own, so the `before-sleep` lock hook stays while the timeouts
  move to Noctalia. A second idle owner would double-fire on the same idle stream.
* **Guessing the fragment location from the script path.** It looks local and obvious and is wrong the
  moment the tool is installed — see bug #3 above.
