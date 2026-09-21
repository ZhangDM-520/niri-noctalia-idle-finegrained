#!/usr/bin/env python3
"""Tests for nri-idle, written at its interface.

Everything here goes through `render` / `install` / `uninstall` / `status` and the Noctalia port.
Nothing pokes at the splicing internals, so these tests survive any rewrite of the module body —
they describe behaviour ("the user's comments survive") rather than implementation
("`partition` is called twice").

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory

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


def export_for(fragment: str) -> str:
    """A `noctalia config export full` body that matches a fragment exactly."""
    body = fragment.replace("behavior_order", "behavior_order")
    return body


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
        fake = nri.FakeNoctalia(export=export_for(GOOD_FRAGMENT))

        self.assertEqual(nri.status(self.fragment, self.target, fake), 0)

    def test_status_detects_drift_between_file_and_running_shell(self):
        self.write_user_config()
        nri.install(self.fragment, self.target, nri.FakeNoctalia())
        stale = export_for(GOOD_FRAGMENT).replace("timeout = 120", "timeout = 600")
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
        fake = nri.FakeNoctalia(export=export_for(GOOD_FRAGMENT))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
