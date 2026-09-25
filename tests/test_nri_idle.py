#!/usr/bin/env python3
"""Tests for nri-idle, written at its interface.

Everything here goes through `render` / `install` / `uninstall` / `status` / `paths`, through
`main(argv)` for the command line itself, and through the Noctalia port. Nothing pokes at the
splicing internals, so these tests survive any rewrite of the module body — they describe
behaviour ("the user's comments survive") rather than implementation ("`partition` is called
twice"). The flag × command matrix is read from the module's own `FLAG_APPLICABILITY` table,
so the contract cannot drift from the tests.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

spec = importlib.util.spec_from_loader(
    "nri_idle", SourceFileLoader("nri_idle", str(REPO / "bin" / "nri-idle"))
)
assert spec and spec.loader
nri = importlib.util.module_from_spec(spec)
sys.modules["nri_idle"] = nri
spec.loader.exec_module(nri)

USER_CONFIG = """\
# my own config, with comments I care about
[shell]
polkit_agent = true          # inline comment

[keybinds]
alt-return = "spawn:foot"
"""

GOOD_FRAGMENT = """\
[idle]
behavior_order = ["dim", "screen-off", "lock"]

[idle.behavior.dim]
timeout = 50
action = "command"
command = "brightnessctl -s && brightnessctl set 30%"
resume_command = "brightnessctl -r"

[idle.behavior.screen-off]
timeout = 70
action = "screen_off"

[idle.behavior.lock]
timeout = 120
action = "lock"
"""


FIXTURES = HERE / "fixtures"

# The BEGIN marker older versions wrote: it carried the checkout layout in the text. Blocks
# wearing it must keep working (prefix matching) and be rewritten with the new wording.
LEGACY_BEGIN = (
    "# >>> nri-idle managed block — do not edit here; edit config/idle.toml in the repo, "
    "then re-run 'nri-idle install' >>>"
)


def export_fixture(name: str) -> str:
    """An authored synthetic `noctalia config export full` body from tests/fixtures/exports/.

    These stand in for what the shell resolves — defaults filled in, key order shuffled,
    unknown keys present — so drift is tested against realistic exports instead of a
    byte-identical copy of the fragment.
    """
    return (FIXTURES / "exports" / name).read_text(encoding="utf-8")


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.fragment = self.dir / "idle.toml"
        self.target = self.dir / "config.toml"
        self.fragment.write_text(GOOD_FRAGMENT, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_user_config(self) -> None:
        self.target.write_text(USER_CONFIG, encoding="utf-8")

    def run_main(self, argv: list[str], fake=None) -> tuple[int, str, str]:
        """Drive the real command line and capture what the user would see.

        A FakeNoctalia is injected so no test needs the shell installed — including the
        tests that only exercise flag plumbing, which must never reach a real binary."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nri.main(argv, noctalia=fake if fake is not None else nri.FakeNoctalia())
        return rc, out.getvalue(), err.getvalue()


class TestRender(Harness):
    def test_render_is_pure_and_wraps_the_fragment(self):
        self.write_user_config()
        before = self.target.read_bytes()

        out = nri.render(self.fragment, self.target)

        self.assertIn(nri.BEGIN, out)
        self.assertIn(nri.END, out)
        self.assertIn("[idle.behavior.lock]", out)
        self.assertEqual(before, self.target.read_bytes(), "render must not touch the target")

    def test_render_creates_nothing_when_the_target_is_absent(self):
        out = nri.render(self.fragment, self.target)
        self.assertFalse(self.target.exists())
        self.assertIn(nri.BEGIN, out)


class TestInstall(Harness):
    def test_user_text_survives_byte_for_byte(self):
        self.write_user_config()

        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        result = self.target.read_text(encoding="utf-8")
        for line in USER_CONFIG.rstrip("\n").splitlines():
            self.assertIn(line, result, "a comment or key was rewritten")
        self.assertLess(result.index("[keybinds]"), result.index(nri.BEGIN))

    def test_install_is_idempotent(self):
        self.write_user_config()

        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        once = self.target.read_text(encoding="utf-8")
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        twice = self.target.read_text(encoding="utf-8")

        self.assertEqual(once, twice)
        self.assertEqual(twice.count(nri.BEGIN), 1)
        self.assertEqual(twice.count(nri.END), 1)

    def test_changed_fragment_replaces_the_block_and_says_so_in_status(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        self.fragment.write_text(GOOD_FRAGMENT.replace("timeout = 120", "timeout = 240"), encoding="utf-8")
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        result = self.target.read_text(encoding="utf-8")
        self.assertIn("timeout = 240", result)
        self.assertNotIn("timeout = 120", result)
        self.assertEqual(result.count(nri.BEGIN), 1)

    def test_a_bad_fragment_leaves_the_target_untouched(self):
        self.write_user_config()
        before = self.target.read_bytes()
        fake = nri.FakeNoctalia()
        self.fragment.write_text("[idle]\nthis is not toml = = =\n", encoding="utf-8")

        with self.assertRaises(nri.Refused):
            nri.install(self.fragment, self.target, fake)

        self.assertEqual(before, self.target.read_bytes())
        self.assertEqual(fake.validated, [], "nothing should even be validated")

    def test_a_fragment_outside_idle_is_refused(self):
        self.write_user_config()
        before = self.target.read_bytes()
        self.fragment.write_text(GOOD_FRAGMENT + "\n[shell]\npolkit_agent = false\n", encoding="utf-8")

        with self.assertRaises(nri.Refused) as ctx:
            nri.install(self.fragment, self.target, nri.FakeNoctalia())

        self.assertIn("outside [idle]", str(ctx.exception))
        self.assertEqual(before, self.target.read_bytes())

    def test_rejected_by_noctalia_means_the_target_is_never_written(self):
        self.write_user_config()
        before = self.target.read_bytes()

        with self.assertRaises(nri.Refused) as ctx:
            nri.install(self.fragment, self.target, nri.FakeNoctalia(validate_ok=False))

        self.assertIn("left untouched", str(ctx.exception))
        self.assertEqual(before, self.target.read_bytes())
        leftovers = [p for p in self.dir.iterdir() if p.name not in {"idle.toml", "config.toml"}]
        self.assertEqual(leftovers, [], "the validation probe file should be cleaned up")

    def test_the_candidate_is_validated_before_being_written(self):
        self.write_user_config()
        fake = nri.FakeNoctalia()

        nri.install(self.fragment, self.target, fake)

        self.assertEqual(len(fake.validated), 1)
        self.assertEqual(fake.reloaded, 1)

    def test_an_existing_target_is_backed_up(self):
        self.write_user_config()

        steps = nri.install(self.fragment, self.target, nri.FakeNoctalia())

        backups = list(self.dir.glob("config.toml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), USER_CONFIG)
        self.assertTrue(any("backed up" in s for s in steps))

    def test_dry_run_writes_nothing(self):
        self.write_user_config()
        before = self.target.read_bytes()

        steps = nri.install(self.fragment, self.target, nri.FakeNoctalia(), dry_run=True)

        self.assertEqual(before, self.target.read_bytes())
        self.assertEqual(list(self.dir.glob("*.bak-*")), [])
        self.assertTrue(any("dry-run" in s for s in steps))

    def test_no_reload_flag_is_honoured(self):
        self.write_user_config()
        fake = nri.FakeNoctalia()
        nri.install(self.fragment, self.target, fake, reload=False)
        self.assertEqual(fake.reloaded, 0)

    def test_a_half_present_block_is_refused(self):
        self.target.write_text(USER_CONFIG + "\n" + nri.BEGIN + "\n", encoding="utf-8")
        before = self.target.read_bytes()

        with self.assertRaises(nri.Refused) as ctx:
            nri.install(self.fragment, self.target, nri.FakeNoctalia())

        self.assertIn("damaged", str(ctx.exception))
        self.assertEqual(before, self.target.read_bytes())


class TestUninstall(Harness):
    def test_round_trip_restores_the_original_file(self):
        self.write_user_config()
        original = self.target.read_bytes()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        nri.uninstall(self.fragment, self.target, nri.FakeNoctalia())

        self.assertEqual(original, self.target.read_bytes())

    def test_uninstall_without_a_block_is_a_no_op(self):
        self.write_user_config()
        before = self.target.read_bytes()

        steps = nri.uninstall(self.fragment, self.target, nri.FakeNoctalia())

        self.assertEqual(before, self.target.read_bytes())
        self.assertTrue(any("nothing to do" in s for s in steps))


class TestStatus(Harness):
    def test_status_reports_knowledge_and_missing_behaviours(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        fake = nri.FakeNoctalia(export=export_fixture("defaults-filled.toml"))

        self.assertEqual(nri.status(self.fragment, self.target, fake), 0)

    def test_status_detects_drift_between_file_and_running_shell(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        stale = export_fixture("defaults-filled.toml").replace("timeout = 120", "timeout = 600")
        fake = nri.FakeNoctalia(export=stale)

        self.assertEqual(nri.status(self.fragment, self.target, fake), 1)

    def test_status_flags_an_unknown_behavior_order_name(self):
        self.write_user_config()
        self.fragment.write_text(
            GOOD_FRAGMENT.replace('"screen-off"', '"screenoff"'), encoding="utf-8"
        )
        self.assertIn("screenoff", "\n".join(nri.warnings(self.fragment.read_text())))


class TestPreexistingIdleConfig(Harness):
    """The migration case: the target already has hand-written idle tables."""

    HAND_WRITTEN = USER_CONFIG + """
[idle]
behavior_order = ["dim", "lock"]

[idle.behavior.dim]
timeout = 300
action = "command"
command = "brightnessctl set 5%"
"""

    def test_render_refuses_rather_than_defining_idle_twice(self):
        self.target.write_text(self.HAND_WRITTEN, encoding="utf-8")
        before = self.target.read_bytes()

        with self.assertRaises(nri.Refused) as ctx:
            nri.render(self.fragment, self.target)

        self.assertIn("--replace-idle", str(ctx.exception))
        self.assertEqual(before, self.target.read_bytes())

    def test_install_refuses_and_touches_nothing(self):
        self.target.write_text(self.HAND_WRITTEN, encoding="utf-8")
        before = self.target.read_bytes()
        fake = nri.FakeNoctalia()

        with self.assertRaises(nri.Refused):
            nri.install(self.fragment, self.target, fake)

        self.assertEqual(before, self.target.read_bytes())
        self.assertEqual(fake.validated, [], "refusal happens before validation")
        self.assertEqual(list(self.dir.glob("*.bak-*")), [])

    def test_replace_idle_removes_the_old_tables_and_keeps_everything_else(self):
        self.target.write_text(self.HAND_WRITTEN, encoding="utf-8")

        nri.install(self.fragment, self.target, nri.FakeNoctalia(), replace_idle=True)

        result = self.target.read_text(encoding="utf-8")
        self.assertNotIn("brightnessctl set 5%", result, "the stale hand-written dim is gone")
        self.assertNotIn("behavior_order = [\"dim\", \"lock\"]", result)
        self.assertIn("brightnessctl set 30%", result, "the fragment's dim is in")
        self.assertIn("[keybinds]", result)
        self.assertIn("# my own config, with comments I care about", result)
        self.assertEqual(result.count("[idle]"), 1, "exactly one [idle] table survives")

    def test_replaced_config_validates_as_toml_with_one_idle_table(self):
        import tomllib as tb

        self.target.write_text(self.HAND_WRITTEN, encoding="utf-8")
        rendered = nri.render(self.fragment, self.target, replace_idle=True)

        doc = tb.loads(rendered)
        self.assertEqual(sorted(doc["idle"]["behavior"]), ["dim", "lock", "screen-off"])
        self.assertEqual(doc["idle"]["behavior"]["lock"]["timeout"], 120)


class TestDisabledBehaviorsAreNotDrift(Harness):
    def test_disabled_and_zero_timeout_blocks_have_no_drift(self):
        self.write_user_config()
        frag = GOOD_FRAGMENT + "\n[idle.behavior.suspend]\ntimeout = 900\naction = \"suspend\"\nenabled = false\n"
        self.fragment.write_text(frag, encoding="utf-8")
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        # Noctalia's export omits the disabled behaviour entirely - that is correct, not drift.
        fake = nri.FakeNoctalia(export=export_fixture("disabled-and-zero-absent.toml"))

        self.assertEqual(nri.status(self.fragment, self.target, fake), 0)


class TestFragmentResolution(unittest.TestCase):
    """Where the fragment comes from: installed config dir, or a checkout. Injected, so testable."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        (self.home / ".config" / "nri-idle").mkdir(parents=True)
        (self.repo / "config").mkdir(parents=True)
        (self.repo / "bin").mkdir(parents=True)
        self.script = self.repo / "bin"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_installed_fragment_wins_over_the_checkout(self):
        installed = self.home / ".config" / "nri-idle" / "idle.toml"
        installed.write_text("[idle]\n", encoding="utf-8")
        (self.repo / "config" / "idle.toml").write_text("[idle]\n", encoding="utf-8")

        got = nri.resolve_fragment(self.script, self.home, None)

        self.assertEqual(got, installed)

    def test_checkout_is_used_when_nothing_is_installed(self):
        (self.repo / "config" / "idle.toml").write_text("[idle]\n", encoding="utf-8")

        got = nri.resolve_fragment(self.script, self.home, None)

        self.assertEqual(got, self.repo / "config" / "idle.toml")

    def test_env_override_beats_both(self):
        env = self.root / "elsewhere.toml"
        env.write_text("[idle]\n", encoding="utf-8")
        (self.home / ".config" / "nri-idle" / "idle.toml").write_text("[idle]\n", encoding="utf-8")

        self.assertEqual(nri.resolve_fragment(self.script, self.home, str(env)), env)

    def test_explicit_flag_beats_everything(self):
        explicit = self.root / "explicit.toml"
        self.assertEqual(
            nri.resolve_fragment(self.script, self.home, "/nope", explicit), explicit
        )

    def test_nothing_found_points_at_the_place_to_create(self):
        got = nri.resolve_fragment(self.script, self.home, None)
        self.assertEqual(got, self.home / ".config" / "nri-idle" / "idle.toml")


class TestFragmentChecks(Harness):
    def test_a_missing_fragment_is_refused(self):
        with self.assertRaises(nri.Refused):
            nri.read_fragment(self.dir / "nope.toml")

    def test_a_fragment_without_behaviors_is_refused(self):
        self.fragment.write_text("[idle]\npre_action_fade_seconds = 0.0\n", encoding="utf-8")
        with self.assertRaises(nri.Refused):
            nri.read_fragment(self.fragment)

    def test_behaviors_are_parsed_with_their_semantics(self):
        got = {b.name: b for b in nri.behaviors(GOOD_FRAGMENT)}
        self.assertEqual(got["dim"].action, "command")
        self.assertEqual(got["lock"].timeout, 120)
        self.assertTrue(got["lock"].enabled)

    def test_shipped_fragment_in_the_repo_is_valid(self):
        """The fragment we actually ship must install cleanly — this is the repo's own gate."""
        shipped = REPO / "config" / "idle.toml"
        text = nri.read_fragment(shipped)
        names = [b.name for b in nri.behaviors(text)]
        self.assertIn("dim", names)
        self.assertIn("screen-off", names)
        self.assertIn("lock", names)
        self.assertEqual(nri.warnings(text), [], "the shipped fragment carries no warnings")


class TestDamageMatrix(Harness):
    """Every damage state is refused by every command that reads the target — the module
    never guesses. (`paths` prints locations and never opens the file.)

    Before `ManagedBlock`, each call site had its own marker logic and its own damage rules:
    `uninstall` ignored an orphan END, two blocks were "up to date", and END-before-BEGIN
    deleted user text. These pin one verdict per state, at one seam.
    """

    def damage_texts(self) -> dict[str, str]:
        body = GOOD_FRAGMENT.strip("\n")
        return {
            "begin_only": USER_CONFIG + "\n" + nri.BEGIN + "\n",
            "end_only": USER_CONFIG + "\n" + nri.END + "\n" + body + "\n",
            "interleaved": USER_CONFIG + "\n" + nri.END + "\n" + body + "\n" + nri.BEGIN + "\n",
            "multiple": USER_CONFIG + "\n" + nri.block(GOOD_FRAGMENT) + nri.block(GOOD_FRAGMENT),
            "unparseable_body": (
                USER_CONFIG + "\n" + nri.BEGIN + "\nthis is not toml = = =\n" + nri.END + "\n"
            ),
        }

    def assert_refused_with_remedy(self, call) -> None:
        with self.assertRaises(nri.Refused) as ctx:
            call()
        message = str(ctx.exception)
        self.assertIn("damaged", message)
        self.assertIn("grep -n 'nri-idle managed block'", message, "every refusal carries a remedy")

    def test_render_refuses_every_damage_state(self):
        for state, text in self.damage_texts().items():
            with self.subTest(state):
                self.target.write_text(text, encoding="utf-8")
                self.assert_refused_with_remedy(lambda: nri.render(self.fragment, self.target))
                self.assertEqual(text.encode("utf-8"), self.target.read_bytes())

    def test_install_refuses_every_damage_state_and_writes_nothing(self):
        for state, text in self.damage_texts().items():
            with self.subTest(state):
                self.target.write_text(text, encoding="utf-8")
                fake = nri.FakeNoctalia()
                self.assert_refused_with_remedy(
                    lambda: nri.install(self.fragment, self.target, fake)
                )
                self.assertEqual(text.encode("utf-8"), self.target.read_bytes())
                self.assertEqual(fake.validated, [], "refusal happens before validation")
                self.assertEqual(list(self.dir.glob("*.bak-*")), [])

    def test_status_refuses_every_damage_state(self):
        for state, text in self.damage_texts().items():
            with self.subTest(state):
                self.target.write_text(text, encoding="utf-8")
                self.assert_refused_with_remedy(
                    lambda: nri.status(self.fragment, self.target, nri.FakeNoctalia())
                )

    def test_uninstall_refuses_every_damage_state(self):
        for state, text in self.damage_texts().items():
            with self.subTest(state):
                self.target.write_text(text, encoding="utf-8")
                fake = nri.FakeNoctalia()
                self.assert_refused_with_remedy(
                    lambda: nri.uninstall(self.fragment, self.target, fake)
                )
                self.assertEqual(text.encode("utf-8"), self.target.read_bytes())
                self.assertEqual(fake.reloaded, 0)

    def test_damage_exits_2_from_the_command_line(self):
        self.target.write_text(self.damage_texts()["begin_only"], encoding="utf-8")
        for command in ["render", "install", "status", "uninstall"]:
            with self.subTest(command):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = nri.main(
                        [command, "--fragment", str(self.fragment), "--target", str(self.target)]
                    )
                self.assertEqual(rc, 2)

    def test_end_before_begin_never_deletes_user_text(self):
        # The old partition() found no END after the orphan BEGIN and silently dropped the
        # user's text after it. Refuse instead: the bytes must survive unharmed.
        text = (
            USER_CONFIG + "\n" + nri.END + "\n" + GOOD_FRAGMENT.strip("\n") + "\n"
            + nri.BEGIN + "\nsecret = 'keep me'\n"
        )
        self.target.write_text(text, encoding="utf-8")

        self.assert_refused_with_remedy(lambda: nri.install(self.fragment, self.target, nri.FakeNoctalia()))

        self.assertIn("secret = 'keep me'", self.target.read_text(encoding="utf-8"))

    def test_two_intact_blocks_are_refused_not_kept(self):
        # The old splice was idempotent over two blocks and uninstall dropped only the first.
        text = USER_CONFIG + "\n" + nri.block(GOOD_FRAGMENT) + nri.block(GOOD_FRAGMENT)
        self.target.write_text(text, encoding="utf-8")

        self.assert_refused_with_remedy(lambda: nri.render(self.fragment, self.target))
        self.assert_refused_with_remedy(
            lambda: nri.uninstall(self.fragment, self.target, nri.FakeNoctalia())
        )
        self.assertEqual(text.encode("utf-8"), self.target.read_bytes())

    def test_end_only_target_is_refused_by_uninstall(self):
        # Uninstall used to look only for BEGIN: "nothing to do", orphan END forever.
        self.target.write_text(USER_CONFIG + "\n" + nri.END + "\n", encoding="utf-8")

        self.assert_refused_with_remedy(
            lambda: nri.uninstall(self.fragment, self.target, nri.FakeNoctalia())
        )

    def test_marker_text_inside_a_multiline_string_is_refused_not_replaced(self):
        # Whole-line matching still finds markers inside a string; the splice would then edit
        # the string's *value*. The tomllib safety net must refuse — never corrupt.
        text = (
            '[shell]\nmotd = """\n'
            + nri.BEGIN + "\n[idle.behavior.dim]\ntimeout = 50\n"
            + nri.END + '\n"""\n'
        )
        self.target.write_text(text, encoding="utf-8")
        before = self.target.read_bytes()

        with self.assertRaises(nri.Refused) as ctx:
            nri.install(self.fragment, self.target, nri.FakeNoctalia())

        self.assertIn("outside [idle]", str(ctx.exception))
        self.assertEqual(before, self.target.read_bytes())

    def test_legacy_marker_text_is_a_block_and_install_rewrites_it(self):
        # Older versions put the checkout layout in the BEGIN text. Prefix matching keeps
        # those blocks valid; install rewrites them with today's layout-agnostic wording.
        legacy_block = LEGACY_BEGIN + "\n" + GOOD_FRAGMENT.strip("\n") + "\n" + nri.END + "\n"
        self.target.write_text(USER_CONFIG + "\n" + legacy_block, encoding="utf-8")

        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        result = self.target.read_text(encoding="utf-8")
        self.assertIn(nri.BEGIN, result)
        self.assertNotIn("edit config/idle.toml in the repo", result)
        self.assertEqual(result.count(nri.BEGIN_PREFIX), 1)
        self.assertEqual(result.count(nri.END_PREFIX), 1)


class TestBlockFreshness(Harness):
    """The three states of block-vs-fragment comparison in `status` (Part 1, decision 4)."""

    def status_output(self, export: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = nri.status(self.fragment, self.target, nri.FakeNoctalia(export=export))
        return rc, buf.getvalue()

    def test_fresh_block_is_up_to_date(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        rc, out = self.status_output(export_fixture("defaults-filled.toml"))

        self.assertEqual(rc, 0)
        self.assertNotIn("out of date", out)
        self.assertNotIn("formatting only", out)

    def test_formatting_only_edits_are_informational_not_drift(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        reformatted = self.target.read_text(encoding="utf-8").replace(
            "timeout = 50", "timeout   =   50"
        )
        self.target.write_text(reformatted, encoding="utf-8")

        rc, out = self.status_output(export_fixture("defaults-filled.toml"))

        self.assertEqual(rc, 0, "bytes differ but the parse is identical — that is not drift")
        self.assertIn("formatting only", out)
        self.assertIn("not drift", out)
        self.assertIn("nri-idle install", out)

    def test_value_edits_make_the_block_out_of_date(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        changed = self.target.read_text(encoding="utf-8").replace("timeout = 50", "timeout = 51")
        self.target.write_text(changed, encoding="utf-8")

        rc, out = self.status_output(export_fixture("defaults-filled.toml"))

        self.assertEqual(rc, 1)
        self.assertIn("out of date", out)


class TestExportNormalisation(Harness):
    """Export parsing is normalisation, not fiction: drift is decided on parsed behaviours."""

    def install_block(self) -> None:
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

    def status_rc(self, export: str) -> int:
        self.install_block()
        with contextlib.redirect_stdout(io.StringIO()):
            return nri.status(self.fragment, self.target, nri.FakeNoctalia(export=export))

    def export_mutating(self, table: str, old: str, new: str) -> str:
        """Change one field inside one [idle.behavior.*] table of the defaults-filled export."""
        text = export_fixture("defaults-filled.toml")
        head, sep, tail = text.partition(f"[idle.behavior.{table}]\n")
        assert sep and old in tail, f"fixture lost the anchor {old!r} in {table}"
        return head + sep + tail.replace(old, new, 1)

    def test_normalisation_round_trip(self):
        want = set(nri.effective(nri.behaviors(GOOD_FRAGMENT)))
        for name in [
            "defaults-filled.toml",
            "key-order-differing.toml",
            "unknown-keys.toml",
            "disabled-and-zero-absent.toml",
        ]:
            with self.subTest(export=name):
                live = set(nri.normalise_export(export_fixture(name)))
                self.assertEqual(want, live, "every effective behaviour equals its export counterpart")

    def test_defaults_filled_export_is_not_drift(self):
        # The old test fiction fed the fragment back as the export, hiding this class: an
        # export with defaults written out normalises to the same behaviours. Not drift.
        self.assertEqual(self.status_rc(export_fixture("defaults-filled.toml")), 0)

    def test_key_order_is_not_drift(self):
        self.assertEqual(self.status_rc(export_fixture("key-order-differing.toml")), 0)

    def test_unknown_keys_are_not_drift(self):
        self.assertEqual(self.status_rc(export_fixture("unknown-keys.toml")), 0)

    def test_export_mutation_timeout_yields_drift(self):
        self.assertEqual(self.status_rc(self.export_mutating("dim", "timeout = 50", "timeout = 55")), 1)

    def test_export_mutation_action_yields_drift(self):
        mutated = self.export_mutating("screen-off", 'action = "screen_off"', 'action = "suspend"')
        self.assertEqual(self.status_rc(mutated), 1)

    def test_export_mutation_enabled_yields_drift(self):
        self.assertEqual(
            self.status_rc(self.export_mutating("lock", "enabled = true", "enabled = false")), 1
        )

    def test_export_mutation_locked_timeout_yields_drift(self):
        mutated = self.export_mutating("lock", "locked_timeout = 0", "locked_timeout = 30")
        self.assertEqual(self.status_rc(mutated), 1)

    def test_export_mutation_name_yields_drift(self):
        mutated = export_fixture("defaults-filled.toml").replace(
            "[idle.behavior.screen-off]", "[idle.behavior.screenoff]"
        )
        self.assertEqual(self.status_rc(mutated), 1)

    def test_empty_idle_export_reports_no_idle_behaviours(self):
        # Was exit 1 with "(nothing — is Noctalia running?)": an empty export is now its own
        # class (exit 4) — the shell answered, it just named no behaviours.
        rc = self.status_rc(export_fixture("empty-idle.toml"))
        self.assertEqual(rc, 4)
        self.assertEqual(nri.normalise_export(export_fixture("empty-idle.toml")), [])

    def test_malformed_export_reports_no_idle_behaviours(self):
        # Was exit 1 lumped in with drift: unparseable-but-readable is exit 4 now, the
        # same class as an empty export ("the running shell exports no idle behaviours").
        rc = self.status_rc(export_fixture("malformed.toml"))
        self.assertEqual(rc, 4)
        self.assertEqual(nri.normalise_export(export_fixture("malformed.toml")), [])


class TestReplaceIdleVerification(Harness):
    """--replace-idle: the line scan nominates spans, tomllib decides what actually goes."""

    def test_lookalike_header_inside_a_user_string_survives(self):
        self.target.write_text(
            '[shell]\nmotd = """\n[lucky] definitely not a table\n"""\n\n'
            '[idle]\nbehavior_order = ["dim"]\nstale = 1\n',
            encoding="utf-8",
        )

        nri.install(self.fragment, self.target, nri.FakeNoctalia(), replace_idle=True)

        result = self.target.read_text(encoding="utf-8")
        self.assertIn("[lucky] definitely not a table", result, "the string keeps its contents")
        self.assertNotIn("stale = 1", result, "the hand-written idle table is gone")
        self.assertIn("[idle.behavior.lock]", result)

    def test_removal_that_would_shred_a_string_is_refused(self):
        # The scan cuts the [idle] region at the next header-lookalike — here a line inside
        # the string — which would leave the user's value in pieces. Refuse, write nothing.
        text = '[shell]\nmotd = """\n[idle]\n"""\n'
        self.target.write_text(text, encoding="utf-8")
        before = self.target.read_bytes()

        with self.assertRaises(nri.Refused):
            nri.install(self.fragment, self.target, nri.FakeNoctalia(), replace_idle=True)

        self.assertEqual(before, self.target.read_bytes())
        self.assertIn("[idle]", self.target.read_text(encoding="utf-8"), "the lookalike survives")

    def test_removal_that_would_edit_a_string_value_is_refused(self):
        # The cut here still parses, but the user's string would silently change value: only
        # the outside-[idle] comparison can tell the two apart.
        text = (
            '[shell]\nmotd = """\n[idle]\n"""\nb = """\n[lucky]\n"""\n'
        )
        self.target.write_text(text, encoding="utf-8")
        before = self.target.read_bytes()

        with self.assertRaises(nri.Refused) as ctx:
            nri.render(self.fragment, self.target, replace_idle=True)

        self.assertIn("outside [idle]", str(ctx.exception))
        self.assertEqual(before, self.target.read_bytes())


class TestParsedFragment(Harness):
    def test_fragment_is_parsed_once_into_behaviors_and_warnings(self):
        frag = nri.load_fragment(self.fragment)

        self.assertEqual(frag.text, GOOD_FRAGMENT)
        self.assertEqual([b.name for b in frag.behaviors], ["dim", "screen-off", "lock"])
        self.assertEqual(list(frag.warnings), [])
        self.assertEqual(frag.doc["idle"]["behavior_order"], ["dim", "screen-off", "lock"])

    def test_warnings_are_computed_during_the_one_parse(self):
        frag = nri.parse_fragment(GOOD_FRAGMENT.replace('"screen-off"', '"screenoff"'))
        self.assertTrue(any("screenoff" in w for w in frag.warnings))


class TestExportOutcome(unittest.TestCase):
    """The port's edge: raw bytes + outcome → the typed result. 127/124 keep their meaning,
    and every failure gets its own kind instead of the old `""` sentinel."""

    BODY = b"[idle.behavior.dim]\ntimeout = 5\n"

    def test_success_carries_the_raw_export(self):
        res = nri.export_outcome(0, self.BODY)
        self.assertEqual(res.kind, nri.EXPORT_OK)
        self.assertEqual(res.text.encode("utf-8"), self.BODY)

    def test_missing_binary_is_not_found(self):
        self.assertEqual(nri.export_outcome(127, b"noctalia: not found").kind, nri.EXPORT_NOT_FOUND)

    def test_a_hang_is_timeout(self):
        self.assertEqual(nri.export_outcome(124, b"timed out").kind, nri.EXPORT_TIMEOUT)

    def test_any_other_rc_is_failed(self):
        res = nri.export_outcome(2, b"boom")
        self.assertEqual(res.kind, nri.EXPORT_FAILED)
        self.assertEqual(res.detail, "boom", "the binary's own words survive for the report")

    def test_undecodable_bytes_are_unparseable(self):
        self.assertEqual(nri.export_outcome(0, b"\xff\xfe").kind, nri.EXPORT_UNPARSEABLE)

    def test_bad_toml_is_unparseable(self):
        res = nri.export_outcome(0, b"this is not toml = = =")
        self.assertEqual(res.kind, nri.EXPORT_UNPARSEABLE)
        self.assertTrue(res.detail)

    def test_blank_export_is_empty(self):
        self.assertEqual(nri.export_outcome(0, b"  \n").kind, nri.EXPORT_EMPTY)

    def test_export_without_behaviors_is_empty(self):
        self.assertEqual(nri.export_outcome(0, b"[idle]\n").kind, nri.EXPORT_EMPTY)

    def test_fake_modes_mirror_the_typed_kinds(self):
        for mode in nri.FakeNoctalia.EXPORT_MODES:
            with self.subTest(mode=mode):
                res = nri.FakeNoctalia(export_mode=mode).exported_idle()
                self.assertEqual(res.kind, mode)


class TestFlagApplicability(Harness):
    """The flag × command matrix is a contract, and these tests read the same table the
    parser does. An inapplicable flag must be refused loudly before anything happens —
    an accepted-but-ignored flag is a lie about what the run did."""

    # Values for the value-taking flags, kept inside the temp dir: a matrix cell must not
    # scribble on the checkout (a relative --target would land in the repo root).
    def value_for(self, flag: str) -> str:
        return {
            "--fragment": str(self.dir / "other-idle.toml"),
            "--target": str(self.dir / "other-config.toml"),
            "--noctalia": "other-noctalia",
        }[flag]

    def cell_argv(self, command: str, flag: str) -> list[str]:
        argv = [command, "--fragment", str(self.fragment), "--target", str(self.target), flag]
        if flag in ("--fragment", "--target", "--noctalia"):
            argv.append(self.value_for(flag))
        return argv

    def test_inapplicable_flags_are_refused_with_usage_and_write_nothing(self):
        for flag, commands in nri.FLAG_APPLICABILITY.items():
            for command in nri.COMMANDS:
                if command in commands:
                    continue
                with self.subTest(command=command, flag=flag):
                    self.write_user_config()
                    before = self.target.read_bytes()

                    rc, out, err = self.run_main(self.cell_argv(command, flag))

                    self.assertEqual(rc, 2, "an inapplicable flag must be a hard error")
                    self.assertIn("usage:", err)
                    self.assertIn(flag, err)
                    self.assertEqual(before, self.target.read_bytes(), "nothing may be written")
                    self.assertEqual(list(self.dir.glob("*.bak-*")), [])

    def test_every_flag_is_accepted_by_its_commands(self):
        for flag, commands in nri.FLAG_APPLICABILITY.items():
            for command in commands:
                with self.subTest(command=command, flag=flag):
                    self.write_user_config()
                    rc, out, err = self.run_main(
                        self.cell_argv(command, flag),
                        nri.FakeNoctalia(export=export_fixture("defaults-filled.toml")),
                    )
                    self.assertNotIn("does not apply to", err, "a valid cell must pass the guard")


class TestInstallRehearsalAndReload(Harness):
    """`install --dry-run` is a rehearsal: every decision and refusal of the real run, minus
    the writes. And a FAILED reload is a failed install, whatever the file now says."""

    def argv(self, *extra: str) -> list[str]:
        return ["install", "--fragment", str(self.fragment), "--target", str(self.target), *extra]

    def test_dry_run_validates_the_candidate_and_touches_nothing(self):
        self.write_user_config()
        before = self.target.read_bytes()
        fake = nri.FakeNoctalia()

        rc, out, err = self.run_main(self.argv("--dry-run"), fake)

        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.validated), 1, "the rehearsal runs the same validation")
        self.assertEqual(fake.reloaded, 0)
        self.assertEqual(before, self.target.read_bytes(), "the target is never touched")
        self.assertEqual(list(self.dir.glob("*.bak-*")), [])
        self.assertIn("dry-run", out)

    def test_dry_run_refuses_exactly_as_a_real_run_would(self):
        self.write_user_config()
        before = self.target.read_bytes()
        fake = nri.FakeNoctalia(validate_mode="fail", validate_message="fake: nope")

        rc, out, err = self.run_main(self.argv("--dry-run"), fake)

        self.assertEqual(rc, 2, "the rehearsal reproduces the real run's refusals")
        self.assertIn("left untouched", err)
        self.assertIn("fake: nope", err)
        self.assertEqual(before, self.target.read_bytes())
        self.assertEqual(list(self.dir.glob("*.bak-*")), [])

    def test_a_failed_reload_exits_1_and_says_so(self):
        self.write_user_config()
        fake = nri.FakeNoctalia(reload_ok=False)

        rc, out, err = self.run_main(self.argv(), fake)

        self.assertEqual(rc, 1, "a FAILED reload is not success")
        self.assertIn("FAILED", out)
        self.assertIn("reload failed", out)
        self.assertTrue(self.target.is_file(), "the write did happen; only the verdict is new")

    def test_success_exits_0_only_when_everything_including_reload_succeeded(self):
        self.write_user_config()
        fake = nri.FakeNoctalia()

        rc, out, err = self.run_main(self.argv(), fake)

        self.assertEqual(rc, 0)
        self.assertEqual(fake.reloaded, 1)


class TestUninstallReloadControl(Harness):
    """`uninstall --no-reload` used to be accepted and ignored. The matrix says the flag
    applies to `uninstall`, so it must be honoured."""

    def argv(self, *extra: str) -> list[str]:
        return ["uninstall", "--fragment", str(self.fragment), "--target", str(self.target), *extra]

    def test_no_reload_skips_the_reload(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        fake = nri.FakeNoctalia()

        rc, out, err = self.run_main(self.argv("--no-reload"), fake)

        self.assertEqual(rc, 0)
        self.assertEqual(fake.reloaded, 0, "--no-reload must be honoured, not silently ignored")
        self.assertNotIn(nri.BEGIN, self.target.read_text(encoding="utf-8"))

    def test_reload_still_happens_by_default(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        fake = nri.FakeNoctalia()

        rc, out, err = self.run_main(self.argv(), fake)

        self.assertEqual(rc, 0)
        self.assertEqual(fake.reloaded, 1)

    def test_a_failed_reload_exits_1(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(reload_ok=False))

        self.assertEqual(rc, 1)
        self.assertIn("reload failed", out)

    def test_the_function_honours_the_reload_parameter_too(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        fake = nri.FakeNoctalia()

        nri.uninstall(self.fragment, self.target, fake, reload=False)

        self.assertEqual(fake.reloaded, 0)


class TestStatusExitCodes(Harness):
    """The fan-out: one exit code per kind of failure, and honest words for each."""

    def argv(self) -> list[str]:
        return ["status", "--fragment", str(self.fragment), "--target", str(self.target)]

    def installed(self) -> None:
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())

    def test_0_when_healthy(self):
        self.installed()
        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export=export_fixture("defaults-filled.toml")))
        self.assertEqual(rc, 0)

    def test_0_for_formatting_only(self):
        self.installed()
        reformatted = self.target.read_text(encoding="utf-8").replace("timeout = 50", "timeout   =   50")
        self.target.write_text(reformatted, encoding="utf-8")

        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export=export_fixture("defaults-filled.toml")))

        self.assertEqual(rc, 0, "formatting only is informational, not drift")

    def test_1_for_drift_against_the_running_shell(self):
        self.installed()
        stale = export_fixture("defaults-filled.toml").replace("timeout = 120", "timeout = 600")

        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export=stale))

        self.assertEqual(rc, 1)
        self.assertIn("drift", out)

    def test_1_for_an_out_of_date_block(self):
        self.installed()
        self.target.write_text(
            self.target.read_text(encoding="utf-8").replace("timeout = 50", "timeout = 51"),
            encoding="utf-8",
        )

        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export=export_fixture("defaults-filled.toml")))

        self.assertEqual(rc, 1)
        self.assertIn("out of date", out)

    def test_2_for_a_damaged_target(self):
        self.write_user_config()
        self.target.write_text(self.target.read_text(encoding="utf-8") + nri.BEGIN + "\n", encoding="utf-8")

        rc, out, err = self.run_main(self.argv())

        self.assertEqual(rc, 2)
        self.assertIn("refused:", err)

    def test_3_when_noctalia_is_missing(self):
        self.installed()
        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export_mode=nri.EXPORT_NOT_FOUND))
        self.assertEqual(rc, 3)
        self.assertIn("install Noctalia or pass --noctalia", out)

    def test_3_when_the_export_times_out(self):
        self.installed()
        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export_mode=nri.EXPORT_TIMEOUT))
        self.assertEqual(rc, 3)
        self.assertIn("wedged", out)

    def test_3_when_the_export_fails(self):
        self.installed()
        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export_mode=nri.EXPORT_FAILED))
        self.assertEqual(rc, 3)
        self.assertIn("export failed", out)

    def test_4_when_the_export_is_unparseable(self):
        self.installed()
        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export_mode=nri.EXPORT_UNPARSEABLE))
        self.assertEqual(rc, 4)
        self.assertIn("no idle behaviours", out)

    def test_4_when_the_export_is_empty(self):
        self.installed()
        rc, out, err = self.run_main(self.argv(), nri.FakeNoctalia(export_mode=nri.EXPORT_EMPTY))
        self.assertEqual(rc, 4)
        self.assertIn("no idle behaviours", out)


class TestPaths(Harness):
    """`paths` pins the installed layout the toolset lives in — scripts consume these lines,
    so a moved directory must break here first."""

    def test_paths_pins_the_installed_layout(self):
        rc, out, err = self.run_main(
            ["paths", "--fragment", str(self.fragment), "--target", str(self.target)]
        )
        home = Path.home()
        self.assertEqual(rc, 0)
        self.assertEqual(
            dict(line.split("=", 1) for line in out.strip().splitlines()),
            {
                "fragment": str(self.fragment),
                "target": str(self.target),
                "rules": str(home / ".config" / "media-idle-bridge" / "config.toml"),
                "bin_dir": str(home / ".local" / "bin"),
                "unit": str(home / ".config" / "systemd" / "user" / "media-idle-bridge.service"),
                "noctalia": "noctalia",
            },
        )

    def test_paths_reports_the_owned_fragment_on_a_fresh_home(self):
        """The fragment `paths` reports is where the *user's* copy lives or will be
        created. A checkout's shipped idle.toml is a source to seed from, never the
        answer — otherwise install.sh cannot seed a fresh machine."""
        home = self.dir / "fresh-home"
        checkout = self.dir / "checkout"
        (checkout / "config").mkdir(parents=True)
        (checkout / "config" / "idle.toml").write_text(GOOD_FRAGMENT, encoding="utf-8")

        with mock.patch.object(nri, "HERE", checkout / "bin"), \
                mock.patch.object(nri.Path, "home", return_value=home), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NRI_IDLE_FRAGMENT", None)
            rc, out, err = self.run_main(["paths"])

        fragment = dict(line.split("=", 1) for line in out.strip().splitlines())["fragment"]
        self.assertEqual(rc, 0)
        self.assertEqual(Path(fragment), home / ".config" / "nri-idle" / "idle.toml")

    def test_paths_honours_the_fragment_env_override(self):
        home = self.dir / "fresh-home"
        seeded = self.dir / "seeded.toml"

        with mock.patch.object(nri.Path, "home", return_value=home), \
                mock.patch.dict(os.environ, {"NRI_IDLE_FRAGMENT": str(seeded)}):
            rc, out, err = self.run_main(["paths"])

        fragment = dict(line.split("=", 1) for line in out.strip().splitlines())["fragment"]
        self.assertEqual(rc, 0)
        self.assertEqual(Path(fragment), seeded)

    def test_paths_echoes_the_noctalia_override(self):
        rc, out, err = self.run_main(["paths", "--noctalia", "my-noctalia"])

        self.assertEqual(rc, 0)
        self.assertIn("noctalia=my-noctalia", out)

    def test_paths_writes_nothing(self):
        self.write_user_config()
        before = self.target.read_bytes()

        rc, out, err = self.run_main(
            ["paths", "--fragment", str(self.fragment), "--target", str(self.target)]
        )

        self.assertEqual(rc, 0)
        self.assertEqual(before, self.target.read_bytes())
        self.assertEqual(list(self.dir.glob("*.bak-*")), [])


class TestRenderThroughMain(Harness):
    """`render`'s stdout is user config text: it must arrive byte-for-byte or it is broken."""

    def test_stdout_is_byte_exact_through_main(self):
        self.write_user_config()
        expected = nri.render(self.fragment, self.target)

        rc, out, err = self.run_main(
            ["render", "--fragment", str(self.fragment), "--target", str(self.target)]
        )

        self.assertEqual(rc, 0)
        self.assertEqual(out, expected)
        self.assertEqual(err, "")

    def test_stdout_is_byte_exact_through_the_script(self):
        self.write_user_config()
        before = self.target.read_bytes()
        expected = nri.render(self.fragment, self.target).encode("utf-8")

        proc = subprocess.run(
            [sys.executable, str(REPO / "bin" / "nri-idle"), "render",
             "--fragment", str(self.fragment), "--target", str(self.target)],
            capture_output=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, expected, "no newline translation, no encoding wobble")
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(before, self.target.read_bytes(), "render writes nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
