#!/usr/bin/env python3
"""Tests for media-idle-bridge's rules contract and decision core, written at its interface.

Everything here goes through `load_rules` / `classify_player` / `backstop_video` / `decide` /
`ingest` / `describe` - the pure seam.  The module is loaded with plain python3: no dbus, no
PyGObject, no session.  Live behaviour (MPRIS signals, inhibitors) stays in tests/bridge-tests.sh.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

spec = importlib.util.spec_from_loader(
    "media_idle_bridge",
    SourceFileLoader("media_idle_bridge", str(REPO / "bin" / "media-idle-bridge")),
)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
sys.modules["media_idle_bridge"] = bridge
spec.loader.exec_module(bridge)


# ── fixtures ──────────────────────────────────────────────────────────────────────────────

def rules(**overrides):
    return replace(bridge.default_rules(), **overrides)


def player(identity="", status="Playing", url="", bus_name="org.mpris.MediaPlayer2.fake"):
    return bridge.Player(bus_name=bus_name, identity=identity, status=status, url=url)


def stream(binary="", app="", node="", role="", state="running", node_id=1):
    return bridge.Stream(node_id=node_id, binary=binary, app=app, node=node, role=role, state=state)


def media_state(players=(), streams=()):
    return bridge.MediaState(players=list(players), streams=list(streams))


def load_rules_text(text: str):
    with TemporaryDirectory() as td:
        path = Path(td) / "rules.toml"
        path.write_text(text, encoding="utf-8")
        return bridge.load_rules(str(path))


def node_event(node_id=5, with_info=True, state="running", media_class="Stream/Output/Audio",
               binary="ffplay", app="", node="", role=""):
    obj = {"type": "PipeWire:Interface:Node", "id": node_id}
    if with_info:
        obj["info"] = {
            "state": state,
            "props": {
                "media.class": media_class,
                "application.process.binary": binary,
                "application.name": app,
                "node.name": node,
                "media.role": role,
            },
        }
    return obj


class FakeSource:
    """Duck-typed MediaSource for describe(): facts without a session."""

    def __init__(self, state: bridge.MediaState) -> None:
        self.state = state

    def snapshot(self) -> bridge.MediaState:
        return self.state


# ── the suite ─────────────────────────────────────────────────────────────────────────────

class ImportTests(unittest.TestCase):
    def test_module_imports_without_dbus_or_gi(self):
        # The whole point of the lazy-import implementation: the decision core and this suite
        # must load on a machine without python-dbus / PyGObject.
        self.assertNotIn("dbus", sys.modules)
        self.assertNotIn("gi", sys.modules)


class PlayerPrecedenceTests(unittest.TestCase):
    """Per-player order is contractual: music identity > video identity > browser rule."""

    def test_netease_empty_url_is_music_never_browser_rule(self):
        v = bridge.classify_player(player(identity="netease-cloud-music-web-player"), rules())
        self.assertEqual((v.kind, v.rule), ("music", "music_players"))
        self.assertEqual(v.matched, "netease")

    def test_music_identity_beats_url(self):
        # Identity wins even when the URL looks like video: the music rule is first.
        v = bridge.classify_player(
            player(identity="spotify", url="https://www.youtube.com/watch?v=x"), rules())
        self.assertEqual(v.kind, "music")

    def test_identity_in_both_lists_resolves_to_music(self):
        r = rules(music_players=("mpv",), video_players=("mpv",))
        v = bridge.classify_player(player(identity="mpv"), r)
        self.assertEqual((v.kind, v.rule), ("music", "music_players"))

    def test_browser_with_music_host_url_is_music(self):
        v = bridge.classify_player(
            player(identity="zen", url="https://music.youtube.com/watch?v=abc"), rules())
        self.assertEqual((v.kind, v.rule), ("music", "music_hosts"))
        self.assertEqual(v.matched, "music.youtube.com")

    def test_browser_with_other_url_is_video(self):
        # Code rule: a URL that is not a music host is video - browser_default never applies.
        v = bridge.classify_player(
            player(identity="zen", url="https://www.youtube.com/watch?v=abc"), rules())
        self.assertEqual((v.kind, v.rule), ("video", "browser_url"))
        self.assertIn("youtube.com", v.matched)

    def test_urlless_browser_uses_browser_default(self):
        v = bridge.classify_player(player(identity="zen"), rules())
        self.assertEqual((v.kind, v.rule, v.matched), ("video", "browser_default", "video"))
        for setting in ("video", "music", "unknown"):
            v = bridge.classify_player(player(identity="zen"), rules(browser_default=setting))
            self.assertEqual((v.kind, v.matched), (setting, setting))

    def test_browser_matched_on_bus_name(self):
        v = bridge.classify_player(
            player(identity="Mozilla", bus_name="org.mpris.MediaPlayer2.firefox.instance123"),
            rules())
        self.assertEqual((v.kind, v.rule), ("video", "browser_default"))

    def test_unknown_player(self):
        v = bridge.classify_player(player(identity="totally-obscure"), rules())
        self.assertEqual((v.kind, v.rule), ("unknown", "none"))


class CrossPlayerTests(unittest.TestCase):
    """Cross-player precedence: any video inhibits; music never masks the backstop."""

    def test_music_plus_pipewire_video_inhibits(self):
        # Regression for the bug where decide() returned "no inhibit" on any music player
        # BEFORE the PipeWire backstop - leaving the screen to dim over playing video.
        st = media_state(players=[player(identity="netease-cloud-music-web-player")],
                         streams=[stream(binary="ffplay")])
        snap = bridge.decide(st, rules())
        self.assertTrue(snap.decision)

    def test_music_plus_backstop_reasons_show_why(self):
        st = media_state(players=[player(identity="netease")],
                         streams=[stream(binary="ffplay")])
        snap = bridge.decide(st, rules())
        inhibiting = [r for r in snap.reasons if r.inhibits]
        self.assertEqual(len(inhibiting), 1)
        self.assertEqual(inhibiting[0].verdict.rule, "video_binaries")
        notes = [r for r in snap.reasons if not r.inhibits]
        self.assertTrue(any("no inhibit" in r.text for r in notes))

    def test_music_alone_never_inhibits(self):
        st = media_state(players=[player(identity="spotify")])
        self.assertFalse(bridge.decide(st, rules()).decision)

    def test_browser_plus_video_mpris_inhibits(self):
        st = media_state(players=[player(identity="zen", url="https://music.youtube.com/watch?v=1"),
                                  player(identity="mpv")])
        self.assertTrue(bridge.decide(st, rules()).decision)

    def test_music_plus_video_mpris_video_wins(self):
        st = media_state(players=[player(identity="netease"), player(identity="vlc")])
        snap = bridge.decide(st, rules())
        self.assertTrue(snap.decision)
        kinds = {r.verdict.kind for r in snap.reasons}
        self.assertEqual(kinds, {"video", "music"})

    def test_inhibit_reason_never_mentions_music(self):
        # The inhibit reason feeds systemd-inhibit --why and the ScreenSaver call; a music
        # "(no inhibit)" note must never leak into it.
        st = media_state(players=[player(identity="netease"), player(identity="vlc")])
        snap = bridge.decide(st, rules())
        reason = "; ".join(str(r) for r in snap.reasons if r.inhibits)
        self.assertIn("mpris video", reason)
        self.assertNotIn("no inhibit", reason)

    def test_paused_player_never_inhibits(self):
        st = media_state(players=[player(identity="mpv", status="Paused")])
        self.assertFalse(bridge.decide(st, rules()).decision)

    def test_snapshot_decision_follows_reasons(self):
        st = media_state(streams=[stream(binary="vlc")])
        snap = bridge.decide(st, rules())
        self.assertEqual(snap.decision, any(r.inhibits for r in snap.reasons))


class JsonStreamParserTests(unittest.TestCase):
    """Chunking at every boundary: pw-dump --monitor delivers arbitrarily split bytes."""

    DOC = '{"a": "x\\"y"} {"b": [1, {"c": "d"}]} '

    def test_single_value(self):
        p = bridge._JsonStreamParser()
        self.assertEqual(p.feed('{"a": 1}'), [{"a": 1}])

    def test_multi_value_in_one_feed(self):
        p = bridge._JsonStreamParser()
        self.assertEqual(p.feed('[1] {"k": "v"} [2, 3]'), [[1], {"k": "v"}, [2, 3]])

    def test_trailing_whitespace(self):
        p = bridge._JsonStreamParser()
        self.assertEqual(p.feed('{"a": 1}  \n\t '), [{"a": 1}])
        self.assertEqual(p.feed("{}"), [{}])       # nothing stale left in the buffer

    def test_split_mid_string(self):
        p = bridge._JsonStreamParser()
        self.assertEqual(p.feed('{"a": "hel'), [])
        self.assertEqual(p.feed('lo"}'), [{"a": "hello"}])

    def test_split_inside_escape(self):
        doc = '{"a": "x\\"y"}'
        idx = doc.index("\\")
        p = bridge._JsonStreamParser()
        self.assertEqual(p.feed(doc[:idx]), [])
        self.assertEqual(p.feed(doc[idx:]), [{"a": 'x"y'}])

    def test_split_at_every_boundary(self):
        expected = bridge._JsonStreamParser().feed(self.DOC)
        self.assertEqual(expected, [{"a": 'x"y'}, {"b": [1, {"c": "d"}]}])
        for i in range(len(self.DOC) + 1):
            p = bridge._JsonStreamParser()
            got = p.feed(self.DOC[:i]) + p.feed(self.DOC[i:])
            self.assertEqual(got, expected, f"split at {i}")


class NodeLifecycleTests(unittest.TestCase):
    """ONE ingest model of the pw-dump node stream, shared by monitor and snapshot paths."""

    def test_node_with_info_is_present(self):
        state = bridge.ingest({}, node_event(node_id=7, binary="ffplay", state="running"))
        self.assertEqual(list(state), [7])
        s = state[7]
        self.assertEqual((s.binary, s.state, s.role), ("ffplay", "running", ""))
        self.assertEqual(s.node_id, 7)

    def test_node_event_updates(self):
        state = bridge.ingest({}, node_event(node_id=7, state="running"))
        state = bridge.ingest(state, node_event(node_id=7, state="suspended", binary="mpv"))
        self.assertEqual(len(state), 1)
        self.assertEqual((state[7].state, state[7].binary), ("suspended", "mpv"))

    def test_event_without_info_removes(self):
        state = bridge.ingest({}, node_event(node_id=7))
        self.assertEqual(bridge.ingest(state, node_event(node_id=7, with_info=False)), {})

    def test_info_null_removes(self):
        state = bridge.ingest({}, node_event(node_id=7))
        obj = {"type": "PipeWire:Interface:Node", "id": 7, "info": None}
        self.assertEqual(bridge.ingest(state, obj), {})

    def test_non_node_objects_ignored(self):
        state = bridge.ingest({}, node_event(node_id=7))
        out = bridge.ingest(state, {"type": "PipeWire:Interface:Client", "id": 7})
        out = bridge.ingest(out, {"id": 7})                     # no type at all
        out = bridge.ingest(out, ["not", "a", "dict"])
        self.assertEqual(list(out), [7])

    def test_non_stream_node_is_not_a_playback_stream(self):
        self.assertEqual(bridge.ingest({}, node_event(media_class="Audio/Sink")), {})
        self.assertEqual(bridge.ingest({}, node_event(media_class="Video/Source")), {})

    def test_stale_entry_dropped_when_node_stops_being_a_stream(self):
        state = bridge.ingest({}, node_event(node_id=7))
        self.assertEqual(bridge.ingest(state, node_event(node_id=7, media_class="Audio/Sink")), {})

    def test_list_value_folds_all_objects(self):
        state = bridge.ingest({}, [node_event(node_id=1), node_event(node_id=2, binary="mpv")])
        self.assertEqual(set(state), {1, 2})

    def test_ingest_is_pure(self):
        before = bridge.ingest({}, node_event(node_id=7))
        snapshot = dict(before)
        bridge.ingest(before, node_event(node_id=7, with_info=False))
        self.assertEqual(before, snapshot)


class RulesValidationTests(unittest.TestCase):
    """load_rules never raises and never crash-loops: wrong values fall back with a warning."""

    def test_missing_file_defaults_without_warning(self):
        r, warnings = bridge.load_rules("/nonexistent/rules.toml")
        self.assertEqual(r, bridge.default_rules())
        self.assertEqual(warnings, [])

    def test_bad_toml_defaults_with_warning(self):
        r, warnings = load_rules_text("video_players = [unterminated")
        self.assertEqual(r, bridge.default_rules())
        self.assertEqual(len(warnings), 1)

    def test_unknown_key_warned_and_dropped(self):
        r, warnings = load_rules_text('video_players = ["mpv"]\nbogus_key = 3\n')
        self.assertEqual(r.video_players, ("mpv",))
        self.assertFalse(hasattr(r, "bogus_key"))
        self.assertTrue(any("bogus_key" in w for w in warnings))

    def test_bare_string_falls_back_to_default(self):
        # A bare string would iterate as CHARACTERS and misclassify.
        r, warnings = load_rules_text('music_players = "netease"\n')
        self.assertEqual(r.music_players, bridge.default_rules().music_players)
        self.assertTrue(any("music_players" in w for w in warnings))

    def test_scalar_int_falls_back_to_default(self):
        # A scalar raises at evaluation time and crash-loops under Restart=on-failure.
        r, warnings = load_rules_text("video_binaries = 3\n")
        self.assertEqual(r.video_binaries, bridge.default_rules().video_binaries)
        self.assertTrue(any("video_binaries" in w for w in warnings))

    def test_mixed_list_falls_back_to_default(self):
        r, warnings = load_rules_text('video_roles = ["video", 42]\n')
        self.assertEqual(r.video_roles, bridge.default_rules().video_roles)
        self.assertTrue(any("video_roles" in w for w in warnings))

    def test_browser_default_invalid_falls_back_to_video(self):
        r, warnings = load_rules_text('browser_default = "banana"\n')
        self.assertEqual(r.browser_default, "video")
        self.assertTrue(any("browser_default" in w for w in warnings))

    def test_browser_default_non_string_falls_back(self):
        r, warnings = load_rules_text("browser_default = 5\n")
        self.assertEqual(r.browser_default, "video")
        self.assertTrue(any("browser_default" in w for w in warnings))

    def test_browser_default_domain(self):
        for value in ("video", "music", "unknown"):
            r, warnings = load_rules_text(f'browser_default = "{value}"\n')
            self.assertEqual((r.browser_default, warnings), (value, []))

    def test_empty_list_disables_that_rule(self):
        r, warnings = load_rules_text('deny_binaries = []\nvideo_roles = []\n')
        self.assertEqual((r.deny_binaries, r.video_roles, warnings), ((), (), []))
        # deny_binaries empty => the deny veto is gone; video_binaries still matches.
        v = bridge.backstop_video(stream(binary="noctalia-mpv"), r)
        self.assertEqual(v.kind, "video")

    def test_missing_key_uses_default(self):
        r, warnings = load_rules_text('browser_default = "music"\n')
        self.assertEqual(r.browser_default, "music")
        self.assertEqual(r.video_players, bridge.default_rules().video_players)

    def test_rules_is_typed(self):
        r = bridge.default_rules()
        self.assertIsInstance(r, bridge.Rules)
        self.assertIsInstance(r.video_players, tuple)
        self.assertEqual(r.browser_default, "video")
        self.assertEqual(
            set(r.__dataclass_fields__),
            {"video_players", "music_players", "browser_players", "browser_default",
             "music_hosts", "video_binaries", "video_roles", "deny_binaries"})

    def test_no_exception_reaches_the_caller(self):
        for text in ('video_players = "mpv"', "video_binaries = 3", "music_hosts = 7.5",
                     "deny_binaries = [1, 2]", 'browser_default = true', "???=broken",
                     "video_roles = [[\"nested\"]]"):
            r, warnings = load_rules_text(text + "\n")
            self.assertIsInstance(r, bridge.Rules)
            self.assertTrue(warnings, text)


class BackstopTests(unittest.TestCase):
    """Allow-list-first: only listed, running streams may inhibit; deny subtracts only."""

    def test_not_running_never_inhibits(self):
        for state in ("paused", "corked", "suspended", ""):
            v = bridge.backstop_video(stream(binary="mpv", state=state), rules())
            self.assertEqual((v.kind, v.rule), ("none", "state"), state)

    def test_deny_binaries_beats_video_binaries(self):
        v = bridge.backstop_video(stream(binary="noctalia-mpv"), rules())
        self.assertEqual((v.kind, v.rule, v.matched), ("none", "deny_binaries", "noctalia"))
        v = bridge.backstop_video(stream(binary="pipewire-frontend"), rules())
        self.assertEqual((v.kind, v.rule), ("none", "deny_binaries"))

    def test_three_field_matching(self):
        r = rules()
        self.assertEqual(bridge.backstop_video(stream(binary="ffplay"), r).kind, "video")
        self.assertEqual(bridge.backstop_video(stream(app="ffplay"), r).kind, "video")
        self.assertEqual(bridge.backstop_video(stream(node="gst-play-1.0"), r).kind, "video")

    def test_video_binaries_is_substring(self):
        v = bridge.backstop_video(stream(binary="org.example.mpv.wrapper"), rules())
        self.assertEqual((v.kind, v.matched), ("video", "mpv"))

    def test_role_is_substring_and_case_insensitive(self):
        # video_roles used to be exact-match; it is now substring like every other rule.
        r = rules()
        self.assertEqual(bridge.backstop_video(stream(role="Movie"), r).matched, "movie")
        self.assertEqual(bridge.backstop_video(stream(role="the-video-role"), r).matched, "video")
        self.assertEqual(bridge.backstop_video(stream(role="AUDIO"), r).kind, "none")

    def test_empty_stream_never_inhibits(self):
        v = bridge.backstop_video(stream(), rules())
        self.assertEqual((v.kind, v.rule), ("none", "none"))

    def test_verdict_carries_rule_and_match(self):
        v = bridge.backstop_video(stream(binary="ffplay"), rules())
        self.assertEqual((v.kind, v.rule, v.matched), ("video", "video_binaries", "ffplay"))
        self.assertIn("video_binaries", v.evidence)


class DescribeTests(unittest.TestCase):
    """--once rendering: every verdict shows which rule matched, warnings fail the exit code."""

    def test_inhibit_report(self):
        st = media_state(players=[player(identity="netease")], streams=[stream(binary="ffplay")])
        report, code = bridge.describe(rules(), [], source=FakeSource(st))
        self.assertEqual(code, 0)
        self.assertIn("decision: INHIBIT (video playing)", report)
        self.assertIn("class=music [music_players matched 'netease']", report)
        self.assertIn("backstop=yes [video_binaries matched 'ffplay']", report)
        self.assertIn("reason: pipewire stream: ffplay", report)

    def test_normal_idle_report(self):
        st = media_state(players=[player(identity="netease")])
        report, code = bridge.describe(rules(), [], source=FakeSource(st))
        self.assertEqual(code, 0)
        self.assertIn("decision: normal idle chain", report)
        self.assertIn("mpris music (no inhibit)", report)

    def test_rule_warning_exits_nonzero(self):
        st = media_state(players=[player(identity="mpv", url="")])
        report, code = bridge.describe(rules(), ["rules.toml: video_binaries bogus"],
                                       source=FakeSource(st))
        self.assertEqual(code, 1)
        self.assertIn("rule warning: rules.toml: video_binaries bogus", report)

    def test_describe_uses_the_same_decision_path(self):
        # The --once report must never disagree with decide(): same inputs, same verdicts.
        st = media_state(players=[player(identity="zen", url="")],
                         streams=[stream(binary="ffplay", state="paused")])
        report, _ = bridge.describe(rules(), [], source=FakeSource(st))
        snap = bridge.decide(st, rules())
        self.assertIn(f"decision: {'INHIBIT (video playing)' if snap.decision else 'normal idle chain'}",
                      report)
        self.assertIn("backstop=no [state matched 'paused']", report)


if __name__ == "__main__":
    unittest.main()
