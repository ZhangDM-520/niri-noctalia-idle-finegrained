"""install.sh's installed-layout contract, driven end to end.

The installer is exercised the way a user runs it — a real bash process against a throwaway
HOME, with stub `noctalia` / `systemctl` / `pw-dump` / `playerctl` on PATH — and pinned on
the interface `--dry-run` promises:

  * the installed layout comes from `nri-idle paths`; the script names no location itself,
  * a dry run makes the same decisions and hits the same refusals (remedy text and exit
    code included) as a real run with the same flags,
  * a dry run writes nothing at all: the HOME tree is byte-identical before and after,
    and the stub systemctl log stays empty,
  * a dry run says "dry-run: would ..." for every would-be write and "skipped (dry run)"
    for the installed-copy verification; a real run never uses those phrases,
  * user configs are created only when absent and never overwritten.
"""

import os
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
NRI_IDLE = REPO / "bin" / "nri-idle"

SHIPPED_FRAGMENT = (REPO / "config" / "idle.toml").read_text(encoding="utf-8")
SHIPPED_RULES = (REPO / "config" / "media-idle-rules.toml").read_text(encoding="utf-8")

# A pre-existing fragment that differs from the shipped one by one comment: byte-different,
# so `cmp` must leave it alone, but the same behaviours, so `nri-idle status` stays green.
EDITED_FRAGMENT = SHIPPED_FRAGMENT + "\n# a local edit the installer must never overwrite\n"
EDITED_RULES = SHIPPED_RULES + "\n# my own rules edit\n"

CLEAN_CONFIG = """[shell]
# a hand-written config with no idle tables at all
width = 800
"""

HAND_WRITTEN_IDLE = """[shell]
width = 800

[idle]
behavior_order = ["mine"]

[idle.behavior.mine]
timeout = 30
action = "lock"
"""

# What the stub `noctalia config export full` answers: the three shipped behaviours as the
# running shell would resolve them, so `nri-idle status` can pass on a healthy install.
EXPORT_TEXT = """[idle.behavior.dim]
timeout = 50
action = "command"

[idle.behavior.screen-off]
timeout = 70
action = "screen_off"

[idle.behavior.lock]
timeout = 120
action = "lock"
"""

NOCTALIA_STUB = (
    """#!/bin/bash
printf '%s\\n' "$*" >> "$NOCTALIA_LOG"
case "${1:-}" in
  --version) echo "noctalia stub 1.0"; exit 0 ;;
esac
case "${1:-} ${2:-}" in
  "config validate")
    if [ -n "${NOCTALIA_REJECT_VALIDATE:-}" ]; then
      echo "stub: rejected" >&2
      exit 1
    fi
    exit 0 ;;
  "config export")
    cat <<'TOML'
"""
    + EXPORT_TEXT
    + """TOML
    exit 0 ;;
  "msg config-reload")
    if [ -n "${NOCTALIA_RELOAD_FAIL:-}" ]; then
      echo "stub: no running shell" >&2
      exit 1
    fi
    exit 0 ;;
esac
exit 0
"""
)

SYSTEMCTL_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
exit 0
"""

TINY_STUB = """#!/bin/bash
exit 0
"""


class Fixture:
    """One throwaway machine: a HOME, stubs on PATH, and log files outside the HOME tree."""

    def __init__(self, root: Path, name: str) -> None:
        self.root = root / name
        self.root.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.stubbin = self.root / "stubbin"
        self.stubbin.mkdir()
        self.systemctl_log_path = self.root / "systemctl.log"
        self.noctalia_log_path = self.root / "noctalia.log"
        self._install_stub("noctalia", NOCTALIA_STUB)
        self._install_stub("systemctl", SYSTEMCTL_STUB)
        self._install_stub("pw-dump", TINY_STUB)
        self._install_stub("playerctl", TINY_STUB)
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.stubbin) + os.pathsep + "/usr/bin:/bin",
            "SYSTEMCTL_LOG": str(self.systemctl_log_path),
            "NOCTALIA_LOG": str(self.noctalia_log_path),
        }

    def _install_stub(self, name: str, text: str) -> None:
        stub = self.stubbin / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(0o755)

    # The documented layout as this fixture's HOME resolves it — used only to *seed*
    # fixtures and to read back what the installer touched. What the installer must use is
    # always compared against `nri-idle paths`, never against these.
    @property
    def target(self) -> Path:
        return self.home / ".config" / "noctalia" / "config.toml"

    @property
    def fragment(self) -> Path:
        return self.home / ".config" / "nri-idle" / "idle.toml"

    @property
    def rules(self) -> Path:
        return self.home / ".config" / "media-idle-bridge" / "config.toml"

    def write_target(self, text: str) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text(text, encoding="utf-8")

    def write_fragment(self, text: str) -> None:
        self.fragment.parent.mkdir(parents=True, exist_ok=True)
        self.fragment.write_text(text, encoding="utf-8")

    def write_rules(self, text: str) -> None:
        self.rules.parent.mkdir(parents=True, exist_ok=True)
        self.rules.write_text(text, encoding="utf-8")

    def run_install(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(INSTALL_SH), *args],
            capture_output=True, encoding="utf-8", env=env, cwd=str(REPO),
        )

    def run_paths(self, env_extra: dict | None = None) -> dict:
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [str(NRI_IDLE), "paths"],
            capture_output=True, encoding="utf-8", env=env, cwd=str(REPO),
        )
        assert proc.returncode == 0, proc.stderr
        return dict(line.split("=", 1) for line in proc.stdout.strip().splitlines())

    def systemctl_log(self) -> str:
        return self.systemctl_log_path.read_text(encoding="utf-8") if self.systemctl_log_path.exists() else ""


def snapshot(home: Path) -> dict:
    """The whole HOME tree as {relpath: bytes} — the dry-run verdict is judged on this."""
    tree = {}
    for path in sorted(home.rglob("*")):
        rel = str(path.relative_to(home))
        tree[rel] = b"<dir>" if path.is_dir() else path.read_bytes()
    return tree


def combined(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


HINT_PREFIXES = (
    "check what is actually running with:",
    "check the whole picture with:",
    "watch the bridge with:",
    "diff -u ",
)

# A dry-run line and its real-run twin are the same sentence minus the "dry-run: would "
# prefix. Where the tool driven inside the installer words the two modes differently, the
# phrases fold onto one token; advice lines (hints, diff suggestions) are not steps.
WRITE_VERB = re.compile(r"^  (?:dry-run: would )?(create|install|systemctl)\b")


def step_set(proc: subprocess.CompletedProcess, home: Path) -> list:
    out = combined(proc).replace(str(home), "<HOME>")
    steps = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.startswith("▸"):
            continue
        if line.startswith(HINT_PREFIXES):
            continue
        if line.startswith("note: config/idle.toml uses brightnessctl"):
            continue
        step = re.sub(r"^dry-run: would ", "", line)
        step = re.sub(r"^dry-run: ", "", step)
        if (
            "skipped (dry run)" in step
            or step.startswith("installed-copy verification")
            or step.startswith("warning: the installed copy")
        ):
            step = "verify-installed-copy"
        elif step.startswith("validated candidate:"):
            step = "validate:" + step.split(":", 1)[1].strip()
        elif "would be written" in step or step.startswith("wrote:"):
            step = "write-target"
        elif "would be taken" in step or step.startswith("backed up:"):
            step = "backup-target"
        elif (
            "would be reloaded" in step
            or "would not be reloaded" in step
            or step.startswith("reloaded the running shell")
        ):
            step = "reload"
        steps.append(step)
    return sorted(steps)


class TestFlagForwarding(unittest.TestCase):
    """The bug this deepening fixes: --replace-idle used to be dropped on the dry-run path,
    so the rehearsal refused on hand-written idle tables the real run would have taken over."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_replace_idle_is_forwarded_on_the_dry_run_path(self) -> None:
        fx = Fixture(self.root, "case")
        fx.write_target(HAND_WRITTEN_IDLE)
        before = snapshot(fx.home)

        proc = fx.run_install("--dry-run", "--replace-idle")
        out = combined(proc)

        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("refused:", out)
        self.assertNotIn("Re-run with --replace-idle", out)
        self.assertIn("would be written", out)  # the rehearsal spliced past the [idle] tables
        self.assertEqual(before, snapshot(fx.home), "a dry run must write nothing")
        self.assertEqual(fx.systemctl_log(), "")

    def test_dry_run_refuses_hand_written_idle_exactly_like_the_real_run(self) -> None:
        fx_dry = Fixture(self.root, "dry")
        fx_dry.write_target(HAND_WRITTEN_IDLE)
        fx_real = Fixture(self.root, "real")
        fx_real.write_target(HAND_WRITTEN_IDLE)
        before = snapshot(fx_dry.home)

        dry = fx_dry.run_install("--dry-run")
        real = fx_real.run_install()

        self.assertEqual(dry.returncode, 2, combined(dry))
        self.assertEqual(real.returncode, 2, combined(real))
        for proc in (dry, real):
            out = combined(proc)
            self.assertIn("already defines [idle] tables", out)
            self.assertIn("Re-run with --replace-idle", out)
        self.assertEqual(before, snapshot(fx_dry.home), "a dry run must write nothing")


class TestDryRunDecisions(unittest.TestCase):
    """--dry-run is the same run minus the writes: same checks, same refusals, same steps."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    @staticmethod
    def _seed(fx: Fixture, target: str) -> None:
        if target == "clean":
            fx.write_target(CLEAN_CONFIG)
        elif target == "hand":
            fx.write_target(HAND_WRITTEN_IDLE)

    def test_dry_run_and_real_run_make_the_same_decisions(self) -> None:
        cases = [
            ("clean-target", "clean", [], {}),
            ("clean-target-replace-idle", "clean", ["--replace-idle"], {}),
            ("hand-written-idle", "hand", [], {}),
            ("hand-written-idle-replace-idle", "hand", ["--replace-idle"], {}),
            ("absent-target", "absent", [], {}),
            ("no-bridge", "clean", ["--no-bridge"], {}),
            ("validate-refusal", "clean", [], {"NOCTALIA_REJECT_VALIDATE": "1"}),
        ]
        for label, target, flags, env_extra in cases:
            with self.subTest(label):
                fx_dry = Fixture(self.root, "dry-" + label)
                fx_real = Fixture(self.root, "real-" + label)
                self._seed(fx_dry, target)
                self._seed(fx_real, target)
                before = snapshot(fx_dry.home)

                dry = fx_dry.run_install("--dry-run", *flags, env_extra=env_extra)
                real = fx_real.run_install(*flags, env_extra=env_extra)

                self.assertEqual(dry.returncode, real.returncode, combined(dry) + "\n---\n" + combined(real))
                self.assertEqual(
                    step_set(dry, fx_dry.home), step_set(real, fx_real.home),
                    combined(dry) + "\n---\n" + combined(real),
                )
                self.assertEqual(
                    "Re-run with --replace-idle" in combined(dry),
                    "Re-run with --replace-idle" in combined(real),
                )
                self.assertEqual(before, snapshot(fx_dry.home), "a dry run must write nothing")

    def test_dry_run_output_says_would_for_every_write(self) -> None:
        fx = Fixture(self.root, "case")  # a fresh tree: dirs, binaries, unit, rules all pending
        proc = fx.run_install("--dry-run")
        out = proc.stdout

        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("dry-run: would ", out)
        self.assertIn("installed-copy verification: skipped (dry run)", out)
        seen = 0
        for line in out.splitlines():
            if WRITE_VERB.match(line):
                seen += 1
                self.assertTrue(line.startswith("  dry-run: would "), line)
        self.assertGreaterEqual(seen, 4, out)
        self.assertIn("  dry-run: would install ", out)
        self.assertIn("  dry-run: would create ", out)
        self.assertIn("  dry-run: would systemctl --user daemon-reload", out)

    def test_real_run_output_never_claims_a_dry_run(self) -> None:
        fx = Fixture(self.root, "case")
        proc = fx.run_install()
        out = combined(proc)

        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("dry-run:", out)
        self.assertNotIn("skipped (dry run)", out)
        seen = [line for line in proc.stdout.splitlines() if WRITE_VERB.match(line)]
        self.assertGreaterEqual(len(seen), 4, out)
        self.assertIn("  systemctl --user daemon-reload", proc.stdout)
        self.assertIn("  create ", proc.stdout)  # the rules config really is created

    def test_validate_refusal_has_the_same_remedy_on_both_paths(self) -> None:
        fx_dry = Fixture(self.root, "dry")
        fx_dry.write_target(CLEAN_CONFIG)
        fx_real = Fixture(self.root, "real")
        fx_real.write_target(CLEAN_CONFIG)
        env = {"NOCTALIA_REJECT_VALIDATE": "1"}
        before = snapshot(fx_dry.home)

        dry = fx_dry.run_install("--dry-run", env_extra=env)
        real = fx_real.run_install(env_extra=env)

        for proc in (dry, real):
            out = combined(proc)
            self.assertEqual(proc.returncode, 2, out)
            self.assertIn("refused:", out)
            self.assertIn("Noctalia rejected the merged config", out)
            self.assertIn("Re-run with --replace-idle", out)
        self.assertEqual(before, snapshot(fx_dry.home), "a dry run must write nothing")
        self.assertEqual(CLEAN_CONFIG, fx_real.target.read_text(encoding="utf-8"),
                         "a refused install leaves the target untouched")


class TestLayoutOwnership(unittest.TestCase):
    """The layout is `nri-idle paths`' module; install.sh must be its adapter and nothing
    more — a moved directory changes one file (bin/nri-idle), never the installer."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_install_script_names_no_config_location_itself(self) -> None:
        text = INSTALL_SH.read_text(encoding="utf-8")
        self.assertNotIn(".config/", text)

    def test_every_path_used_equals_nri_idle_paths_output(self) -> None:
        fx = Fixture(self.root, "case")
        # The user fragment pre-exists (edited), so the layout query reports the user copy
        # and the "left alone" line names the fragment path the run used.
        fx.write_fragment(EDITED_FRAGMENT)
        fx.write_target(CLEAN_CONFIG)
        paths = fx.run_paths()

        proc = fx.run_install()
        out = proc.stdout

        self.assertEqual(proc.returncode, 0, combined(proc))
        frag = re.search(r"^\s*(\S+) left alone \(yours differs", out, re.M)
        self.assertIsNotNone(frag, out)
        self.assertEqual(frag.group(1), paths["fragment"])

        rules = re.search(r"^\s*(?:dry-run: would )?create (\S+) \(from the shipped rules", out, re.M)
        self.assertIsNotNone(rules, out)
        self.assertEqual(rules.group(1), paths["rules"])

        pairs = re.findall(r"^\s*(?:dry-run: would )?install (\S+) -> (\S+)$", out, re.M)
        by_dst = {dst: src for src, dst in pairs}
        unit_used = [dst for dst in by_dst if dst.endswith("media-idle-bridge.service")]
        self.assertEqual(unit_used, [paths["unit"]])
        self.assertEqual(
            by_dst.get(str(Path(paths["bin_dir"]) / "nri-idle")),
            str(REPO / "bin" / "nri-idle"),
        )

        # The layout echo is the query's own words, verbatim.
        self.assertIn(f"fragment : {paths['fragment']}", out)
        self.assertIn(f"target   : {paths['target']}", out)
        self.assertIn(f"rules    : {paths['rules']}", out)
        self.assertIn(f"bin_dir  : {paths['bin_dir']}", out)
        self.assertIn(f"unit     : {paths['unit']}", out)

        # What exists afterwards is what the layout said, and the edited fragment survived.
        self.assertTrue(Path(paths["rules"]).is_file())
        self.assertTrue(Path(paths["unit"]).is_file())
        self.assertTrue(Path(paths["bin_dir"], "nri-idle").is_file())
        self.assertEqual(EDITED_FRAGMENT, Path(paths["fragment"]).read_text(encoding="utf-8"))

    def test_dry_run_uses_the_same_layout_lines(self) -> None:
        fx = Fixture(self.root, "case")
        fx.write_fragment(EDITED_FRAGMENT)
        fx.write_target(CLEAN_CONFIG)
        paths = fx.run_paths()

        proc = fx.run_install("--dry-run")

        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn(f"fragment : {paths['fragment']}", proc.stdout)
        self.assertIn(f"unit     : {paths['unit']}", proc.stdout)


class TestUserConfigsAreNeverOverwritten(unittest.TestCase):
    """The fragment and the rules are the user's: created once, then left alone."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_existing_fragment_and_rules_are_left_alone(self) -> None:
        for mode, args in (("dry", ("--dry-run",)), ("real", ())):
            with self.subTest(mode):
                fx = Fixture(self.root, mode)
                fx.write_fragment(EDITED_FRAGMENT)
                fx.write_rules(EDITED_RULES)
                fx.write_target(CLEAN_CONFIG)
                before = snapshot(fx.home)

                proc = fx.run_install(*args)
                out = combined(proc)

                self.assertEqual(proc.returncode, 0, out)
                self.assertEqual(EDITED_FRAGMENT, fx.fragment.read_text(encoding="utf-8"))
                self.assertEqual(EDITED_RULES, fx.rules.read_text(encoding="utf-8"))
                self.assertIn("left alone (yours differs", out)
                self.assertIn("exists — left alone", out)
                if mode == "dry":
                    self.assertEqual(before, snapshot(fx.home), "a dry run must write nothing")


class TestNoSystemdCallsOnADryRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_systemctl_log_is_empty_after_a_dry_run(self) -> None:
        fx = Fixture(self.root, "case")
        proc = fx.run_install("--dry-run")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertEqual(fx.systemctl_log(), "")

    def test_the_real_run_does_enable_the_bridge(self) -> None:
        fx = Fixture(self.root, "case")
        proc = fx.run_install()
        self.assertEqual(proc.returncode, 0, combined(proc))
        log = fx.systemctl_log()
        self.assertIn("daemon-reload", log)
        self.assertIn("enable --now media-idle-bridge.service", log)

    def test_no_bridge_skips_the_bridge_and_systemctl(self) -> None:
        fx = Fixture(self.root, "case")
        proc = fx.run_install("--no-bridge")
        self.assertEqual(proc.returncode, 0, combined(proc))
        paths = fx.run_paths()
        self.assertFalse(Path(paths["unit"]).exists())
        self.assertFalse((Path(paths["bin_dir"]) / "media-idle-bridge").exists())
        self.assertTrue((Path(paths["bin_dir"]) / "nri-idle").is_file())
        self.assertEqual(fx.systemctl_log(), "")


class TestInterfaceAndInvariants(unittest.TestCase):
    """install.sh's own interface is unchanged, and the small behaviours it promised stay."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_flags_and_exit_codes_are_unchanged(self) -> None:
        fx = Fixture(self.root, "case")
        help_proc = fx.run_install("--help")
        self.assertEqual(help_proc.returncode, 0)
        for flag in ("--dry-run", "--replace-idle", "--no-bridge"):
            self.assertIn(flag, help_proc.stdout)

        bad = fx.run_install("--bogus")
        self.assertEqual(bad.returncode, 64)
        self.assertIn("unknown option: --bogus", bad.stderr)

    def test_brightnessctl_dependency_hint_sniff_stays(self) -> None:
        text = INSTALL_SH.read_text(encoding="utf-8")
        self.assertIn("brightnessctl", text)
        self.assertIn("uses brightnessctl, which is not installed", text)

    def test_a_fresh_tree_installs_without_a_running_shell(self) -> None:
        fx = Fixture(self.root, "case")  # target, fragment and rules all absent: a fresh clone
        proc = fx.run_install()
        out = combined(proc)

        self.assertEqual(proc.returncode, 0, out)
        paths = fx.run_paths()
        self.assertTrue(Path(paths["bin_dir"], "nri-idle").is_file())
        self.assertTrue(Path(paths["rules"]).is_file())
        self.assertTrue(Path(paths["unit"]).is_file())
        self.assertTrue(Path(paths["fragment"]).is_file())
        self.assertIn("[idle.behavior.screen-off]", fx.target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
