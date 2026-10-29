"""Playing the library by voice, on the media server's side (server/voice/intent.py "片库",
session.py): "fang Su Li Nan", "bo fang Shen Tan Zhi Hen Ji", "wo yao kan Tie Quan Jiao Yu",
"Su Li Nan di san ji", "di 3 ji" while a library show is on My cast, "ji xu kan ...", "jie zhe
kan", next / previous episode, with the misheard spellings a recogniser produces.

The titles come from the relay catalogue's library section (server/relay/voicelib.py, tested
in tests/test_relay_voice_library.py); a device whose owner may not use the library gets no
such section and so never a library result. A channel named as clearly as a title wins.
Nothing here touches a real device or recogniser."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.voice import intent
from server.voice.intent import LibraryPlace, LibraryTitle, decide
from server.voice.session import VoiceService
from test_voice_intent import catalog
from test_voice_session import FakeAsr, FakeRelay, audio, context, ctl, tone_blocks

SURINAME, TRACES, FIST = "s" + "1" * 12, "s" + "2" * 12, "m" + "3" * 16
INCEPTION, TOM, GALA = "m" + "4" * 16, "m" + "5" * 16, "m" + "6" * 16
FAMILY, FAMILY2, HOME = "s" + "7" * 12, "s" + "8" * 12, "m" + "9" * 16
TITLES = [LibraryTitle(SURINAME, "苏里南", "show", ("Narco-Saints", "毒枭圣徒"), 6),
          LibraryTitle(TRACES, "神探之痕迹", "show", ("Traces",), 30),
          LibraryTitle(FIST, "铁拳教育", "movie", ("The Iron Fist",)),
          LibraryTitle(INCEPTION, "盗梦空间", "movie", ("Inception",)),
          # The same name as channel ch050 and part of 湖南卫视: the channels win.
          LibraryTitle(TOM, "猫和老鼠", "movie"),
          LibraryTitle(GALA, "湖南卫视跨年晚会", "video"),
          LibraryTitle(FAMILY, "家有儿女", "show", (), 100),
          LibraryTitle(FAMILY2, "家有儿女2", "show", (), 80),
          LibraryTitle(HOME, "宝宝一岁生日", "video")]
LIB = catalog(library=TITLES, library_continue=LibraryPlace(FIST, "铁拳教育", "movie"))
# My cast shows Suriname episode 2 of 6.
ON_SHOW = catalog("cast", library=TITLES, library_playing=LibraryPlace(SURINAME, "苏里南", "show", 2, 6),
                  library_continue=LibraryPlace(SURINAME, "苏里南", "show", 2, 6))
ON_MOVIE = catalog("cast", library=TITLES, library_playing=LibraryPlace(INCEPTION, "盗梦空间", "movie"))
# A guest's device, or an owner without the library: no titles at all.
NO_LIB = catalog()


def play(title, episode=0, season=0):
    params = {"id": title}
    if episode:
        params["episode"] = episode
    if season:
        params["season"] = season
    return ("library", params)


# (utterance, catalogue, expected). Expected forms:
#   ("play", title, episode, season)   the relay's library call
#   ("resume_latest",)                 the relay plays the latest of Continue watching
#   ("channel", id)                    a channel switch, as before
#   ("relay", action)                  another relay action (next / previous / resume / episode)
#   ("choices", [refs])                library rows, in this order
#   ("reply", kind, words)             an answer only (nothing to do), words in the reply
CASES = [
    # ---- naming a title ----
    ("放苏里南", LIB, ("play", SURINAME, 0, 0)),
    ("播放苏里南", LIB, ("play", SURINAME, 0, 0)),
    ("我要看苏里南", LIB, ("play", SURINAME, 0, 0)),
    ("我想看苏里南。", LIB, ("play", SURINAME, 0, 0)),
    ("苏里南", LIB, ("play", SURINAME, 0, 0)),
    ("帮我放一下苏里南吧", LIB, ("play", SURINAME, 0, 0)),
    ("小智小智，放苏里南", LIB, ("play", SURINAME, 0, 0)),
    ("播放神探之痕迹", LIB, ("play", TRACES, 0, 0)),
    ("看神探之痕迹", LIB, ("play", TRACES, 0, 0)),
    ("我要看铁拳教育", LIB, ("play", FIST, 0, 0)),
    ("放铁拳教育", LIB, ("play", FIST, 0, 0)),
    ("打开盗梦空间", LIB, ("play", INCEPTION, 0, 0)),
    ("放宝宝一岁生日", LIB, ("play", HOME, 0, 0)),
    # ---- misheard: homophones, near sounds, a dropped or wrong character ----
    ("放苏利南", LIB, ("play", SURINAME, 0, 0)),
    ("放书里南", LIB, ("play", SURINAME, 0, 0)),
    ("放速里南", LIB, ("play", SURINAME, 0, 0)),
    ("播放神探之很及", LIB, ("play", TRACES, 0, 0)),
    ("播放神探痕迹", LIB, ("play", TRACES, 0, 0)),
    ("播放深探之痕迹", LIB, ("play", TRACES, 0, 0)),
    ("我要看铁全教育", LIB, ("play", FIST, 0, 0)),
    ("我要看贴拳教育", LIB, ("play", FIST, 0, 0)),
    ("我要看铁拳", LIB, ("play", FIST, 0, 0)),
    ("放道梦空间", LIB, ("play", INCEPTION, 0, 0)),
    # ---- other names: English, Douban's original and aka ----
    ("放Narco Saints", LIB, ("play", SURINAME, 0, 0)),
    ("放 narco-saints", LIB, ("play", SURINAME, 0, 0)),
    ("看inception", LIB, ("play", INCEPTION, 0, 0)),
    ("放毒枭圣徒", LIB, ("play", SURINAME, 0, 0)),
    ("放the iron fist", LIB, ("play", FIST, 0, 0)),
    # ---- episodes ----
    ("苏里南第三集", LIB, ("play", SURINAME, 3, 0)),
    ("苏里南第3集", LIB, ("play", SURINAME, 3, 0)),
    ("放苏里南第三集", LIB, ("play", SURINAME, 3, 0)),
    ("我要看苏里南的第三集", LIB, ("play", SURINAME, 3, 0)),
    ("苏里南三集", LIB, ("play", SURINAME, 3, 0)),
    ("苏利南第五集", LIB, ("play", SURINAME, 5, 0)),
    ("神探之痕迹第十二集", LIB, ("play", TRACES, 12, 0)),
    ("苏里南第二季第三集", LIB, ("play", SURINAME, 3, 2)),
    ("苏里南最新一集", LIB, ("play", SURINAME, 6, 0)),
    ("苏里南第七集", LIB, ("reply", "library", "只有 6 集")),
    ("继续看苏里南第四集", LIB, ("play", SURINAME, 4, 0)),
    # While a library show is on My cast: its episodes, next and previous.
    ("第3集", ON_SHOW, ("play", SURINAME, 3, 0)),
    ("第三集", ON_SHOW, ("play", SURINAME, 3, 0)),
    ("播放第三集", ON_SHOW, ("play", SURINAME, 3, 0)),
    ("第二季第三集", ON_SHOW, ("play", SURINAME, 3, 2)),
    ("最后一集", ON_SHOW, ("play", SURINAME, 6, 0)),
    ("第2集", ON_SHOW, ("reply", "library", "正在播《苏里南》第 2 集")),
    ("下一集", ON_SHOW, ("relay", "next")),
    ("上一集", ON_SHOW, ("relay", "previous")),
    ("下一个", ON_SHOW, ("relay", "next")),
    ("第3集", ON_MOVIE, ("reply", "episode", "现在不是在看剧")),
    ("上一集", ON_MOVIE, ("reply", "previous", "只有剧集台")),
    ("第3集", LIB, ("reply", "episode", "现在不是在看剧")),
    # Already on it.
    ("放苏里南", ON_SHOW, ("reply", "library", "正在播《苏里南》第 2 集")),
    ("放盗梦空间", ON_MOVIE, ("reply", "library", "正在播《盗梦空间》")),
    ("放盗梦空间", catalog("cast", library=TITLES, paused=True,
                       library_playing=LibraryPlace(INCEPTION, "盗梦空间", "movie")), ("relay", "resume")),
    # ---- carrying on ----
    ("继续看铁拳教育", LIB, ("play", FIST, 0, 0)),
    ("接着看苏里南", LIB, ("play", SURINAME, 0, 0)),
    ("继续看苏利南", LIB, ("play", SURINAME, 0, 0)),
    ("接着看", LIB, ("resume_latest",)),
    ("继续看", LIB, ("resume_latest",)),
    ("接着看吧", LIB, ("resume_latest",)),
    # Paused: carry on with what is paused; already on the latest: nothing new to play.
    ("接着看", catalog("sr01", library=TITLES, paused=True,
                    library_continue=LibraryPlace(FIST, "铁拳教育", "movie")), ("relay", "resume")),
    ("接着看", ON_SHOW, ("relay", "resume")),
    ("接着看", catalog(library=TITLES), ("reply", "resume", "正在播")),
    ("继续", LIB, ("reply", "resume", "正在播")),
    # ---- ambiguity: the rows, the first is asked about ----
    ("放家有", LIB, ("choices", [f"lb:{FAMILY}", f"lb:{FAMILY2}"])),
    ("家有儿女第三集", LIB, ("play", FAMILY, 3, 0)),
    ("家有儿女2第三集", LIB, ("play", FAMILY2, 3, 0)),
    ("家有第三集", LIB, ("choices", [f"lb:{FAMILY}:3", f"lb:{FAMILY2}:3"])),
    # ---- channels first ----
    ("放CCTV5", LIB, ("channel", "ch004")),
    ("中央五套", LIB, ("channel", "ch004")),
    ("猫和老鼠", LIB, ("channel", "ch050")),
    ("我想看猫和老鼠", LIB, ("channel", "ch050")),
    ("湖南卫视", LIB, ("channel", "ch017")),
    ("看湖南卫视", LIB, ("channel", "ch017")),
    ("湖南台", LIB, ("channel", "ch017")),
    ("湖南卫视跨年晚会", LIB, ("play", GALA, 0, 0)),
    ("熊出没第3集", LIB, ("relay", "episode")),
    ("西游记", LIB, ("channel", "sr03")),
    ("第12台", LIB, ("channel", "ch005")),
    # ---- other commands are not taken ----
    ("暂停", ON_SHOW, ("relay", "pause")),
    ("大声一点", LIB, ("reply", "volume", "音量")),
    ("搜视频苏里南", LIB, ("reply", "search", "正在搜索")),
    ("我要看葫芦娃", LIB, ("reply", "none", "没有找到「葫芦娃」")),
    # ---- no library for this device: never a library result ----
    ("放苏里南", NO_LIB, ("reply", "none", "")),
    ("我要看苏里南", NO_LIB, ("reply", "none", "没有找到「苏里南」")),
    ("苏里南第三集", NO_LIB, ("reply", "none", "没有找到「苏里南」")),
    ("接着看", NO_LIB, ("reply", "resume", "正在播")),
    ("继续看铁拳教育", NO_LIB, ("reply", "none", "没有找到")),
]


def outcome(decision):
    relay = decision.relay
    if relay is not None and relay[0] == "library":
        if relay[1].get("resume"):
            return ("resume_latest",)
        return ("play", relay[1]["id"], relay[1].get("episode", 0), relay[1].get("season", 0))
    if decision.do and "channel" in decision.do:
        return ("channel", decision.do["channel"])
    if relay is not None:
        return ("relay", relay[0])
    if decision.choices and all(str(c.get("ref", "")).startswith("lb:") for c in decision.choices):
        return ("choices", [c["ref"] for c in decision.choices])
    return ("reply", decision.kind, decision.reply)


class LibraryIntentTests(unittest.TestCase):
    def test_table(self):
        for text, cat, expected in CASES:
            with self.subTest(text=text, current=cat.current):
                got = outcome(decide(text, cat))
                if expected[0] == "reply":
                    self.assertEqual(got[:2], expected[:2], got)
                    self.assertIn(expected[2], got[2])
                else:
                    self.assertEqual(got, expected)

    def test_replies_say_what_plays(self):
        self.assertEqual(decide("放苏里南", LIB).reply, "播放《苏里南》")
        self.assertEqual(decide("苏里南第三集", LIB).reply, "播放《苏里南》第 3 集")
        self.assertEqual(decide("苏里南第二季第三集", LIB).reply, "播放《苏里南》第 2 季第 3 集")
        self.assertEqual(decide("继续看铁拳教育", LIB).reply, "接着看《铁拳教育》")
        self.assertEqual(decide("接着看", ON_SHOW.__class__(ON_SHOW.channels, library=TITLES,
                                                          library_continue=LibraryPlace(SURINAME, "苏里南", "show", 4, 6))
                                ).reply, "接着看《苏里南》第 4 集")
        ask = decide("放家有", LIB)
        self.assertEqual(ask.reply, "要放《家有儿女》吗？")
        self.assertEqual([c["label"] for c in ask.choices], ["家有儿女 · 剧集", "家有儿女2 · 剧集"])
        self.assertEqual(decide("家有第三集", LIB).reply, "要放《家有儿女》第 3 集吗？")
        for text, cat, _ in CASES:
            self.assertLessEqual(len(decide(text, cat).reply), 40, text)

    def test_relay_answers_become_the_reply(self):
        reply = intent.library_reply
        self.assertEqual(reply({"name": "苏里南", "kind": "show", "episode": 2, "total": 6}), "播放《苏里南》第 2 集")
        self.assertEqual(reply({"name": "盗梦空间", "kind": "movie", "resume": 3723.4}),
                         "接着放《盗梦空间》，从 1:02:03 起")
        self.assertEqual(reply({"name": "苏里南", "kind": "show", "episode": 1, "note": "已经看完了，从第 1 集重看"}),
                         "《苏里南》第 1 集已经看完了，从第 1 集重看")
        self.assertEqual(reply({"title": "盗梦空间"}), "播放《盗梦空间》")

    def test_refs_fit_the_device_and_round_trip(self):
        for ref in (intent.library_ref(SURINAME), intent.library_ref(SURINAME, 12, 2), intent.library_ref(HOME),
                    intent.library_ref(INCEPTION, 9999, 99)):
            self.assertLessEqual(len(ref), 31, ref)
            self.assertIsNotNone(intent.parse_library_ref(ref))
        self.assertEqual(intent.parse_library_ref(f"lb:{SURINAME}:3:2"), (SURINAME, 3, 2))
        self.assertEqual(intent.parse_library_ref(f"lb:{FIST}"), (FIST, 0, 0))
        for bad in ("lb:", "lb:s123", f"lb:{SURINAME}:x", "lb:../../etc", f"lb:{SURINAME}:12345", "ep:sr01:3"):
            self.assertIsNone(intent.parse_library_ref(bad), bad)
        self.assertEqual(intent.library_choice(f"lb:{SURINAME}:3").relay, play(SURINAME, 3))
        self.assertIsNone(intent.library_choice("rm:up01"))

    def test_yes_words(self):
        for text in ("是的", "是", "好", "好的", "放吧", "就这个", "嗯", "可以", "确定", "第一个"):
            self.assertTrue(intent.confirms_play(text), text)
        for text in ("不要", "算了", "第二个", "放苏里南", "大声一点"):
            self.assertFalse(intent.confirms_play(text), text)


# ---------------------------------------------------------------- through the session

LIBRARY = {"titles": [{"id": SURINAME, "kind": "show", "name": "苏里南", "aka": ["Narco-Saints"], "total": 6},
                      {"id": FIST, "kind": "movie", "name": "铁拳教育", "aka": []},
                      {"id": FAMILY, "kind": "show", "name": "家有儿女", "total": 100},
                      {"id": FAMILY2, "kind": "show", "name": "家有儿女2", "total": 80},
                      {"id": 7, "name": "broken"}, "junk", {"id": TOM, "name": ""}],
           "continue": {"id": FIST, "kind": "movie", "name": "铁拳教育", "episode": 0, "total": 0},
           "playing": None}


class LibraryRelay(FakeRelay):
    def __init__(self, playing="ch000", library=LIBRARY):
        super().__init__(playing)
        self.library_section = library

    def catalog(self):
        answer = super().catalog()
        if self.library_section is not None:
            answer["library"] = self.library_section
        return answer

    def library(self, title="", episode=0, season=0, resume=False):
        return self._do("library", title, episode, season, resume)


class LibrarySessionTests(unittest.TestCase):
    def setUp(self):
        self.asr = FakeAsr()
        self.relay = LibraryRelay()
        self.voice = VoiceService(self.asr, self.relay, lambda _m: None, threaded=False)
        self.uid = 0

    def say(self, text, current="ch000"):
        self.asr.text = text
        self.uid += 1
        ctx = context(current)
        self.voice.take(ctl("begin", self.uid, rate=16000, codec="adpcm"), ctx)
        self.voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        self.voice.take(ctl("end", self.uid), ctx)
        return json.loads(self.voice.results()[-1])

    def pick(self, ref, current="ch000"):
        self.uid += 1
        self.voice.take(ctl("pick", self.uid, ref=ref), context(current))
        return json.loads(self.voice.results()[-1])

    def test_a_title_plays_on_this_tv_from_where_it_stopped(self):
        self.relay.answers["library"] = {"ok": True, "title": "苏里南 · 毒枭", "name": "苏里南", "kind": "show",
                                         "episode": 2, "total": 6, "resume": 200.0}
        result = self.say("放苏利南")
        self.assertEqual(self.relay.calls[-1], ("library", SURINAME, 0, 0, False))
        self.assertEqual((result["intent"], result["reply"], result["label"]),
                         ("library", "接着放《苏里南》第 2 集，从 3:20 起", "苏里南 · 毒枭"))
        self.assertNotIn("do", result, "the relay switches the TV to My cast itself")
        self.relay.answers["library"] = {"ok": True, "title": "苏里南", "name": "苏里南", "kind": "show",
                                         "episode": 3, "total": 6}
        self.assertEqual(self.say("苏里南第三集")["reply"], "播放《苏里南》第 3 集")
        self.assertEqual(self.relay.calls[-1], ("library", SURINAME, 3, 0, False))
        self.say("Narco Saints第二季第三集")
        self.assertEqual(self.relay.calls[-1], ("library", SURINAME, 3, 2, False))
        self.relay.answers["library"] = {"ok": True, "title": "铁拳教育", "name": "铁拳教育", "kind": "movie",
                                         "resume": 1800}
        self.assertEqual(self.say("接着看")["reply"], "接着放《铁拳教育》，从 30:00 起")
        self.assertEqual(self.relay.calls[-1], ("library", "", 0, 0, True))

    def test_relay_refusals_are_said(self):
        self.relay.answers["library"] = {"ok": False, "error": "《苏里南》没有第 2 季第 3 集"}
        self.assertEqual(self.say("苏里南第二季第三集")["reply"], "《苏里南》没有第 2 季第 3 集")
        self.relay.down = True
        self.assertEqual(self.say("我要看苏里南")["reply"], "没有找到「苏里南」，可以说「搜索苏里南」",
                         "no catalogue, no titles")

    def test_rows_and_yes(self):
        result = self.say("放家有")
        self.assertEqual((result["intent"], result["reply"]), ("choices", "要放《家有儿女》吗？"))
        self.assertEqual([c["ref"] for c in result["choices"]], [f"lb:{FAMILY}", f"lb:{FAMILY2}"])
        self.assertNotIn("library", [c[0] for c in self.relay.calls])
        self.relay.answers["library"] = {"ok": True, "title": "家有儿女", "name": "家有儿女", "kind": "show",
                                         "episode": 7, "total": 100}
        result = self.say("是的")
        self.assertEqual(self.relay.calls[-1], ("library", FAMILY, 0, 0, False))
        self.assertEqual(result["reply"], "播放《家有儿女》第 7 集")
        # Only once.
        count = len(self.relay.calls)
        self.say("是的")
        self.assertEqual(len(self.relay.calls), count)
        # A row picked with the buttons.
        self.say("家有第三集")
        self.pick(f"lb:{FAMILY2}:3")
        self.assertEqual(self.relay.calls[-1], ("library", FAMILY2, 3, 0, False))
        # Something else said: the question is dropped.
        self.say("放家有")
        self.say("大声一点")
        self.say("是的")
        self.assertEqual(self.relay.calls[-1][0], "library")
        self.assertEqual(len([c for c in self.relay.calls if c[0] == "library"]), 2)
        # A row that was not offered is stale.
        self.assertEqual(self.pick(f"lb:{SURINAME}")["reply"], "这个选项已经过期了")

    def test_channels_still_win(self):
        self.say("放CCTV5")
        self.assertEqual(self.relay.calls, [])
        self.assertEqual(self.say("湖南卫视")["do"], {"channel": "ch017"})

    def test_library_show_on_my_cast(self):
        self.relay.library_section = dict(LIBRARY, playing={"id": SURINAME, "kind": "show", "name": "苏里南",
                                                            "episode": 2, "total": 6})
        self.relay.playing = "cast"
        self.say("第五集", current="cast")
        self.assertEqual(self.relay.calls[-1], ("library", SURINAME, 5, 0, False))
        self.say("上一集", current="cast")
        self.assertEqual(self.relay.calls[-1], ("previous",))
        self.assertEqual(self.say("下一集", current="cast")["reply"], "下一集")
        self.assertEqual(self.relay.calls[-1], ("next",))

    def test_no_library_section_no_library(self):
        for section in (None, "junk", {"titles": "junk"}):
            self.relay.library_section = section
            self.assertEqual(self.say("放苏里南")["intent"], "none", section)
            self.say("接着看")
            self.assertNotIn("library", [c[0] for c in self.relay.calls], section)


if __name__ == "__main__":
    unittest.main()
