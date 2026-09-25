# Working journal

Chronological record of how this toolset got here, for the next maintainer: what was built, what was
decided and why, and what each round of verification actually proved. Durable facts and traps live in
[MEMORY.md](MEMORY.md); design rationale lives in [DESIGN.md](DESIGN.md). Keep entries dated and short.

## 2026-09-21 — from a swayidle config to a published toolset

Origin: the owner wanted *dim 50 s → screen off 70 s → lock 120 s* that respects media — **music must
not keep the screen awake, video must**. Three things were established before any code was written:

1. **No idle engine on Wayland can tell music from video.** niri owns `org.freedesktop.ScreenSaver`
   and ORs D-Bus `Inhibit` into the idle-notify state, so what reaches any idle engine is one bit:
   *"an inhibitor exists"*. Every "media-aware" setup is something translating *is this video?* into
   that bit. Hence a daemon, not a clever timeout command.
2. **Idle ownership moved from swayidle to Noctalia** (it has native `dim`/`screen_off`/`lock`
   behaviours), keeping swayidle for `before-sleep` only, because logind's `PrepareForSleep` is the
   one signal Noctalia does not own.
3. **The policy is a native fragment, not flags.** The deletion test decided it: without the CLI the
   fragment is still a valid Noctalia config.

Then the work split in two: `bin/media-idle-bridge` (the *is this video?* translator) and `bin/nri-idle`
(inject/verify the policy). Live verification on niri 26.04 covered NetEase (music), Zen (music URL vs
video URL), mpv and VLC — that table is in the MANUAL, because those identities are load-bearing and
each one cost a debugging round.

## 2026-09-21/22 — the toolset becomes a repo others can use

`/codebase-design` pass: the one-off scripts became two modules behind small interfaces, with the
`noctalia` binary behind a port so the suite runs without a compositor. **Three real bugs surfaced from
the interface tests** (duplicate `[idle]` tables, false drift on disabled behaviours, installed-copy
fragment resolution) — all invisible while the code was a one-off that ran only on the author's machine.
That is the argument for testing *at the interface*: the bugs were in how the pieces were called, not in
the pieces.

Published at <https://github.com/ZhangDM-520/niri-noctalia-idle-finegrained>.

## 2026-09-22 — the lock woke the screen, and the fix belonged upstream

Report: *"when lock is triggered, the screen will be wakeup and again wait for idle timeout to shutoff."*

* Root cause was **not** in this repo: Noctalia's `setSessionLocked()` ran the *resume* action of every
  already-idled behaviour when re-arming, and `screen_off` hard-wires a `ScreenOn` resume — so the lock
  lit the panel and restarted every countdown. Measured: `dpms=On` **36 ms after** the lock.
* A duplicate check first (per the owner's instruction) found the bug already reported as
  [noctalia-dev/noctalia#4190](https://github.com/noctalia-dev/noctalia/issues/4190) with a mergeable
  fix in [#4002](https://github.com/noctalia-dev/noctalia/pull/4002) whose Niri box was unticked. So:
  no second issue, no competing patch — contribute the missing coverage. The fix was built, verified on
  niri (panel stays `Off` across the lock; brightness still restored on return) and approved upstream.
* This repo documents the bug and the stopgap instead of vendoring a patch: **README section, MANUAL
  §9.1, a pointer in `config/idle.toml`**. Stable-Noctalia users can simply wait for the upstream fix.

## 2026-09-22 — a test suite that disabled the desktop

Running the regression suite stopped the production `media-idle-bridge` service and never restarted it;
the desktop went 4 minutes without media awareness before a final state check caught `inactive`. Fixed
in `c8ea264`: `cleanup()` on `EXIT` restores the service **only if it was active before the run**.
General lesson now in MEMORY.md §4: any test that stops a service or takes an exclusive resource must
put it back on every exit path.

## 2026-09-25 — architecture review for maintainers

Ran `/improve-codebase-architecture` over the whole repo with fresh eyes: where is the depth, where is
the friction, what would make this easier to maintain. Report: `/tmp/architecture-review-20260925-222920.html`
(temp dir per that workflow — regenerating is cheap, the findings are what persist). Six deepening
opportunities, most painful first:

1. **Media-source seam under the classification core** *(Strong)* — `media-idle-bridge` cannot even be
   imported without python-dbus/PyGObject, so its pure decision core is unreachable from the 32-test
   suite, and the PipeWire path has zero coverage even live. Found a latent bug along the way:
   `decide()` returns early when any MPRIS player is music, suppressing the PipeWire backstop — music +
   an MPRIS-less `ffplay` video dims the screen over playing video.
2. **The rules file is an interface without a contract** *(Strong)* — precedence, substring matching and
   value domains live in comments; a `browser_default` typo silently inverts the fail-safe.
3. **The managed block is one concept implemented three times** *(Strong)* — inconsistent damage rules
   (`uninstall` checks only the BEGIN marker), byte-equality drift false positives, and a line-regex
   stand-in for TOML structure that can delete user text under `--replace-idle`.
4. **One parsed-fragment type** *(Worth exploring)* — two near-duplicate TOML→Behaviour mappings, and
   the drift invariant is upheld in tests only because `export_for()` is a no-op.
5. **`install.sh` and `nri-idle` each own the installed layout** *(Worth exploring)* — rules path in
   three files; `install.sh --dry-run --replace-idle` drops the flag and rehearses a refusal the real
   run never hits.
6. **No applicability contract on the CLI's flags** *(Worth exploring)* — inert flag combinations are
   silently ignored, `install --dry-run` skips the validate invariant, and
   `Noctalia.exported_idle` collapses three error modes into `""`.

**Top recommendation: #1** — it is the product's reason to exist, it hides a real bug, and its seam
pays leverage twice (deterministic unit tests + a live suite shrunk to inhibitor lifecycle). Candidate
#2 is its natural companion: the contract sitting behind it. None of the six contradict DESIGN.md —
they deepen the same shape (the fragment stays the interface).

This round also added the maintainer docs this repo lacked: `docs/MEMORY.md` (measured facts, traps,
decisions not to re-litigate) and this journal, plus a "For maintainers" section in the README.

**Not yet done / deliberately deferred:**

* The *is this video?* classification core is only reachable through a live session (the bash suite);
  there is no pure-logic test surface for the rule table itself.
* The "a video is playing but every window reports Paused" case was set aside during verification.
* `HEADLESS-1` output DPMS behaviour is unverified.
* Upstream: when PR #4002 merges, MANUAL §9.1's stopgap can shrink to a historical note.

---

## 2026-09-26 — Fleet implementation: all six architecture candidates, three waves + integration

The owner went offline with one instruction: implement **all six** candidates from the architecture
review, in waves, judging the shape myself. Three implementation waves ran in parallel (two in wave
1), then an integration pass. What each wave chose, and what it found:

**Wave 1 — the two cores.** *(candidate 1+2, the media bridge)*: a `MediaSource` seam
(`snapshot`/`start`/`stop`, MPRIS + PipeWire adapters) so `--once` and the daemon share one decision
path; one pw-dump model (`ingest`) replacing two disagreeing ones; `Rules` validation with
warn-and-fallback (never crash-loop); verdicts now name the matched rule. *(candidate 3+4, nri-idle)*:
`ManagedBlock` as a value type with explicit damage states (absent · intact · begin-only · end-only ·
interleaved · multiple · unparseable-body), whole-line prefix markers so legacy blocks still match,
and every mutation parse-verified on both sides; `ParsedFragment` parsed once per command;
`normalise_export` pure beside the Noctalia port; six authored synthetic export fixtures (recorded
user exports were rejected for privacy).

**Wave 2 — the CLI surface** *(candidate 6)*: declarative `FLAG_APPLICABILITY` (an inapplicable flag
is now a usage error, exit 2, not silence), typed `ExportResult` at the port edge, `status` exit
fan-out 0/1/2/3/4 with 3 and 4 (shell unreadable / export empty) outranking drift, `install --dry-run`
validating for real, `install`/`uninstall` exiting 1 on a failed reload, `uninstall --no-reload`
honoured, and a fifth command `paths` printing the installed layout as `key=value` lines.

**Wave 3 — install.sh** *(candidate 5)*: the script now consumes `nri-idle paths` for every location
(zero `.config/` literals), one argument list and one refusal handler for both the real and the
dry-run branch, and a dry run that is a line-for-line rehearsal (same checks, same refusals, zero
writes, `dry-run: would …`).

**Bugs this found and fixed** (each was real, none theoretical):

1. **Music masked the PipeWire backstop** — `decide()` returned "no inhibit" the moment any MPRIS
   player was music, so an `ffplay` video dimmed the screen over the film. Now: any video inhibits,
   video beats music, music never masks the backstop.
2. **Two pw-dump models disagreed** about a node event missing `info` (removed vs. skipped).
3. **A wrong-typed rule value crash-looped the daemon** (a scalar iterated as characters mid-evaluation,
   under `Restart=on-failure`); `browser_default` typos silently inverted the fail-safe.
4. **`install.sh --dry-run` dropped `--replace-idle`** — the rehearsal could bless what the real run
   would refuse. Found by the wave-3 dry-run/real-run equality test.
5. **`--replace-idle` could delete END-only or interleaved idle tables** the line-regex parser could
   not see — silent data loss, now parse-verified and refused on damage.
6. **`status` leaned on an `export_for()` fiction** (a function that never existed) to distinguish
   "shell unreadable" from "no export"; the typed `ExportResult` replaced the guessing.
7. **`nri-idle paths` reported the resolved *source* fragment** instead of the user-owned location,
   so `install.sh` could not seed a fresh machine — reported by wave 3 against wave 2's output and
   fixed in the integration pass (`owned_fragment()`: explicit → `NRI_IDLE_FRAGMENT` →
   `~/.config/nri-idle/idle.toml`; a checkout copy is a source, never the answer).

**Shape calls recorded so they are not re-litigated:** inhibitor seam in the bridge deferred (the
`MediaSource` seam already makes the decision path testable); port-parsing `ParsedFragment` rejected
in favour of parsing once per command; the classification substring match stays case-insensitive
(including `video_roles`); MANUAL §10's rule 3 was corrected — the code treats a URL-bearing non-music
host as video, and `browser_default` applies only to URL-less players.

**Behaviour changes accepted:** `status` exits 1 on real drift only (formatting is info), exits 3/4
for unreadable/empty shells; a damaged target is refused by `uninstall` and dry runs too; failed
reloads exit 1; `--no-bridge` exits 0 (it used to exit 1 as a leftover status); `paths` gains a
`noctalia=` key.

**Closed from the previous entry's deferred list:** the classification core now has a pure test
surface (`tests/test_media_rules.py`, 56 tests); the whole suite is 169 tests and drives `install.sh`
itself. Still deferred: the "video playing but every window reports Paused" case; `HEADLESS-1` DPMS
verification; shrinking MANUAL §9.1's stopgap once PR #4002 is in a release.

**Integration-pass addendum.** The live suite initially failed 10/15 with `block=idle:handle-power-key`
— not the bridge: Noctalia **Caffeine** was on (it holds a logind `idle` inhibitor *and* a Wayland one
swayidle sees), and `niri` holds `handle-power-key` (harmless). The suite's two signals are global, so
it now *refuses to run* when a foreign idle inhibitor is held (exit 2, naming
`noctalia msg caffeine-disable`) instead of blaming the bridge. With Caffeine off: **15/15, 0
indeterminate** — the rewritten bridge inhibits for MPRIS video, VLC, and URL-bearing/no-URL browsers,
stays out of the way for music and paused players, and releases on SIGTERM. Caffeine was re-enabled
after the run. `./install.sh` on the host, `nri-idle status` (exit 0, three behaviours live), and the
169-test unit suite all green; suite and docs updated with the trap.
