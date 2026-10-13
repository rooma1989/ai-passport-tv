"""The second batch of voice commands, on the media server's side: what is playing, the sleep
timer, back to the last channel, a random channel, carry on with a series, subscribing the
uploader of a cast, removing a creator or series (with a confirmation) and the cast history.

The rules are in server/voice/intent.py ("第二批"), the relay calls in session.py; the relay's
side is tests/test_voice_relay_commands.py. Every table row also keeps the older phrasings
from being taken over (channel names, "di N ji", search, list questions)."""

import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.voice import intent
from server.voice.intent import decide
from server.voice.session import VoiceService
from test_voice_intent import CHANNELS, NUMBER, catalog
from test_voice_session import FakeAsr, FakeRelay, ctl, context, tone_blocks, audio

# Relay data as the relay's catalogue gives it (server/relay/voicecmds.py).
SERIES_NOW = catalog("sr01", episode=12, total=52, title="第12集 熊大熊二", position=510, duration=1420,
                     relay=True, back=["ch017", "ch000"])
CREATOR_NOW = catalog("up01", title="为什么房价会跌", position=65, duration=754, relay=True, back=["sr01"])
CAST_NOW = catalog("cast", title="经典欣赏", position=3700, duration=7200, relay=True)
LIVE_NOW = catalog("ch017", relay=True, back=["sr01", "ch017", "ch000"])
NO_RELAY = catalog("sr01", episode=12)
NO_HISTORY = catalog("ch000", relay=True)


def seeded(cat, seed=7):
    cat.rand = random.Random(seed)
    return cat


# (utterance, catalogue, expected). Expected forms:
#   ("info", words)             an answer on screen, nothing done; words is in the reply
#   ("timer", params, words)    the relay's timer call, and the reply before the relay answers
#   ("timer_no", words)         a timer reply without a relay call
#   ("channel", id, words)      a switch, with words in the reply
#   ("here", id)                already there
#   ("relay", action, params)
#   ("confirm", {ids})          a removal to confirm: these channels, plus "cancel"
#   ("kind", kind)              that decision kind only
CASES = [
    # ---- what's playing ----
    ("现在播的是什么", SERIES_NOW, ("info", "熊出没 第 12/52 集 · 已播 8:30 / 23:40")),
    ("现在在播什么", SERIES_NOW, ("info", "第 12/52 集")),
    ("在放什么", CREATOR_NOW, ("info", "为什么房价会跌 · 1:05 / 12:34")),
    ("这是什么节目", LIVE_NOW, ("info", "湖南卫视（直播）")),
    ("这是哪个台", LIVE_NOW, ("info", "湖南卫视（直播）")),
    ("现在是什么台", CAST_NOW, ("info", "经典欣赏 · 1:01:40 / 2:00:00")),
    ("这是第几集", SERIES_NOW, ("info", "熊出没 第 12/52 集")),
    ("现在第几集", SERIES_NOW, ("info", "第 12/52 集")),
    ("我看到第几集了", SERIES_NOW, ("info", "第 12/52 集")),
    ("这是第几集", CREATOR_NOW, ("info", "不是在看剧")),
    ("这是第几集", LIVE_NOW, ("info", "（直播）")),
    ("还剩多久", SERIES_NOW, ("info", "还剩 15 分 10 秒")),
    ("还有多长时间", CREATOR_NOW, ("info", "还剩 11 分 29 秒")),
    ("这集还有多久结束", SERIES_NOW, ("info", "还剩 15 分 10 秒")),
    ("还剩多久", LIVE_NOW, ("info", "是直播")),
    ("这个视频多长", CREATOR_NOW, ("info", "这个视频 12 分 34 秒")),
    ("这集多长", SERIES_NOW, ("info", "这集 23 分 40 秒")),
    ("播到哪了", CAST_NOW, ("info", "播到 1:01:40 / 2:00:00")),
    ("我看到哪了", CAST_NOW, ("info", "播到 1:01:40 / 2:00:00")),
    ("还剩多久", catalog("up01", relay=True), ("info", "还不知道播到哪了")),
    ("现在播的是什么", NO_RELAY, ("info", "正在播熊出没")),
    ("还剩多久", NO_RELAY, ("info", "中转没回应")),
    ("西游记看到第几集了", catalog(relay=True, series=[intent.Series("sr03", "西游记", 25, 7, 330)]),
     ("info", "西游记看到第 7/25 集 5:30")),
    ("西游记看到哪了", catalog(relay=True, series=[intent.Series("sr03", "西游记", 25, 4, 0)]),
     ("info", "西游记下次从第 4/25 集开始")),
    ("熊出没看到第几集了", SERIES_NOW, ("info", "正在看熊出没第 12/52 集 8:30")),
    ("葫芦娃看到第几集了", SERIES_NOW, ("kind", "none")),
    # ---- sleep timer ----
    ("30分钟后暂停", LIVE_NOW, ("timer", {"action": "set", "minutes": 30}, "30 分钟后暂停")),
    ("三十分钟后暂停", SERIES_NOW, ("timer", {"action": "set", "minutes": 30}, "30 分钟后暂停")),
    ("半小时后关掉", LIVE_NOW, ("timer", {"action": "set", "minutes": 30}, "30 分钟后暂停")),
    ("一小时后关机", LIVE_NOW, ("timer", {"action": "set", "minutes": 60}, "1 小时后暂停")),
    ("一个半小时后关机", LIVE_NOW, ("timer", {"action": "set", "minutes": 90}, "1 小时 30 分钟后暂停")),
    ("两小时以后关电视", LIVE_NOW, ("timer", {"action": "set", "minutes": 120}, "2 小时后暂停")),
    ("20分钟后停", LIVE_NOW, ("timer", {"action": "set", "minutes": 20}, "20 分钟后暂停")),
    ("十五分钟之后就停止播放", LIVE_NOW, ("timer", {"action": "set", "minutes": 15}, "15 分钟后暂停")),
    ("定时30分钟", LIVE_NOW, ("timer", {"action": "set", "minutes": 30}, "30 分钟后暂停")),
    ("定时关机一小时", LIVE_NOW, ("timer", {"action": "set", "minutes": 60}, "1 小时后暂停")),
    ("设个定时四十分钟", LIVE_NOW, ("timer", {"action": "set", "minutes": 40}, "40 分钟后暂停")),
    ("帮我定时20分钟关机", LIVE_NOW, ("timer", {"action": "set", "minutes": 20}, "20 分钟后暂停")),
    ("看完这集就停", SERIES_NOW, ("timer", {"action": "end"}, "这集放完就暂停")),
    ("看完这一集就关", SERIES_NOW, ("timer", {"action": "end"}, "这集放完就暂停")),
    ("放完这个视频就暂停", CAST_NOW, ("timer", {"action": "end"}, "这个视频放完就暂停")),
    ("这集放完就关机", CREATOR_NOW, ("timer", {"action": "end"}, "这个视频放完就暂停")),
    ("看完这集就停", LIVE_NOW, ("timer_no", "直播台没有「这集」")),
    ("十三个小时后关机", LIVE_NOW, ("timer_no", "最长 12 小时")),
    ("取消定时", LIVE_NOW, ("timer", {"action": "cancel"}, "取消定时")),
    ("不要定时了", LIVE_NOW, ("timer", {"action": "cancel"}, "取消定时")),
    ("取消定时关机", LIVE_NOW, ("timer", {"action": "cancel"}, "取消定时")),
    ("还剩多久关机", LIVE_NOW, ("timer", {"action": "status"}, "")),
    ("还有多久暂停", SERIES_NOW, ("timer", {"action": "status"}, "")),
    ("什么时候关机", LIVE_NOW, ("timer", {"action": "status"}, "")),
    ("定时还剩多久", LIVE_NOW, ("timer", {"action": "status"}, "")),
    # ---- back to the last channel ----
    ("回到刚才的台", LIVE_NOW, ("channel", "sr01", "回到熊出没")),
    ("刚才那个台", SERIES_NOW, ("channel", "ch017", "回到湖南卫视")),
    ("换回去", CREATOR_NOW, ("channel", "sr01", "回到熊出没")),
    ("切回去", SERIES_NOW, ("channel", "ch017", "")),
    ("回到之前的频道", SERIES_NOW, ("channel", "ch017", "")),
    ("上一个台", SERIES_NOW, ("channel", "ch017", "回到湖南卫视")),
    ("回到上个台", CREATOR_NOW, ("channel", "sr01", "")),
    ("上一个台", NO_HISTORY, ("channel", "ch5p", "")),         # no history: the list's previous, as before
    ("回到刚才的台", NO_HISTORY, ("kind", "none")),
    ("回到刚才的台", catalog("ch000"), ("kind", "none")),       # relay down
    # ---- random ----
    ("随便换个台", seeded(catalog("ch000")), ("kind", "random_any")),
    ("随机换台", seeded(catalog("ch017")), ("kind", "random_any")),
    ("随便看看", seeded(catalog("ch000")), ("kind", "random_any")),
    ("随机放个动画片", seeded(catalog("ch000")), ("kind", "random_cartoon")),
    ("随便来部剧", seeded(catalog("ch000")), ("kind", "random_series")),
    ("随机一个UP主", seeded(catalog("ch000")), ("kind", "random_up")),
    ("随便放首歌", seeded(catalog("ch000")), ("kind", "random_music")),
    # ---- carry on with a series ----
    ("继续看西游记", catalog(relay=True, series=[intent.Series("sr03", "西游记", 25, 7, 330)]),
     ("channel", "sr03", "接着看西游记第 7 集")),
    ("接着看熊出没", LIVE_NOW, ("channel", "sr01", "接着看熊出没")),
    ("接着看小Lin说", LIVE_NOW, ("channel", "up01", "接着看小Lin说")),
    ("继续看熊出没", SERIES_NOW, ("here", "sr01")),
    ("继续看熊出没", catalog("sr01", paused=True), ("relay", "resume", {})),
    ("继续看熊出没第三集", LIVE_NOW, ("relay", "episode", {"series": "sr01", "episode": 3})),
    ("继续看湖南卫视", SERIES_NOW, ("channel", "ch017", "")),
    ("接着看刚才的台", SERIES_NOW, ("channel", "ch017", "回到湖南卫视")),
    ("继续看葫芦娃", LIVE_NOW, ("kind", "none")),
    # ---- subscribe the uploader of what is playing ----
    ("订阅这个UP主", CAST_NOW, ("relay", "subscribe", {})),
    ("关注他", CAST_NOW, ("relay", "subscribe", {})),
    ("把这个作者加进来", CAST_NOW, ("relay", "subscribe", {})),
    ("订阅", CAST_NOW, ("relay", "subscribe", {})),
    ("关注这个博主", CAST_NOW, ("relay", "subscribe", {})),
    ("把他加到频道里", CAST_NOW, ("relay", "subscribe", {})),
    ("订阅这个UP主", CREATOR_NOW, ("kind", "follow")),
    ("订阅这个UP主", LIVE_NOW, ("kind", "follow")),
    # ---- remove a creator or a series (confirmation first) ----
    ("取消订阅小Lin说", LIVE_NOW, ("confirm", {"up01"})),
    ("不看小Lin说了", LIVE_NOW, ("confirm", {"up01"})),
    ("退订老高与小茉", LIVE_NOW, ("confirm", {"up02"})),
    ("删掉这部剧", SERIES_NOW, ("confirm", {"sr01"})),
    ("取消订阅这个UP主", CREATOR_NOW, ("confirm", {"up01"})),
    ("把西游记删了", LIVE_NOW, ("confirm", {"sr03"})),
    ("把西游记这部剧删掉", LIVE_NOW, ("confirm", {"sr03"})),
    ("删除熊出没", LIVE_NOW, ("confirm", {"sr01"})),
    ("删除熊出没探险", LIVE_NOW, ("confirm", {"sr01", "sr02"})),
    ("删掉这个台", LIVE_NOW, ("kind", "none")),
    ("取消订阅葫芦娃", LIVE_NOW, ("kind", "none")),
    # ---- the cast history ----
    ("重播刚才那个视频", LIVE_NOW, ("relay", "recast", {"step": 0, "resume": False})),
    ("再放一遍刚才的", LIVE_NOW, ("relay", "recast", {"step": 0, "resume": False})),
    ("重新播放刚才的投屏", LIVE_NOW, ("relay", "recast", {"step": 0, "resume": False})),
    ("播放上一个投屏", LIVE_NOW, ("relay", "recast", {"step": 1, "resume": False})),
    ("上一个投屏", CAST_NOW, ("relay", "recast", {"step": 1, "resume": False})),
    ("上上个投屏", LIVE_NOW, ("relay", "recast", {"step": 2, "resume": False})),
    ("继续看刚才投屏的", LIVE_NOW, ("relay", "recast", {"step": 0, "resume": True})),
    ("接着看刚才的视频", LIVE_NOW, ("relay", "recast", {"step": 0, "resume": True})),
    # ---- the older phrasings keep their meaning ----
    ("湖南卫视", SERIES_NOW, ("channel", "ch017", "换台中")),
    ("中央五套", LIVE_NOW, ("channel", "ch004", "")),
    ("熊出没", LIVE_NOW, ("channel", "sr01", "")),
    ("熊出没第三集", LIVE_NOW, ("relay", "episode", {"series": "sr01", "episode": 3})),
    ("第五集", SERIES_NOW, ("relay", "episode", {"series": "sr01", "episode": 5})),
    ("下一个台", LIVE_NOW, ("channel", "ch018", "")),
    ("下一集", SERIES_NOW, ("relay", "next", {})),
    ("暂停", SERIES_NOW, ("relay", "pause", {})),
    ("继续", SERIES_NOW, ("relay", "resume", {})),
    ("继续看", SERIES_NOW, ("relay", "resume", {})),
    ("接着看", CREATOR_NOW, ("relay", "resume", {})),
    ("重播", SERIES_NOW, ("relay", "seek", {"position": 0})),
    ("重播这集", SERIES_NOW, ("relay", "seek", {"position": 0})),
    ("再看一遍", SERIES_NOW, ("relay", "seek", {"position": 0})),
    ("再放一遍", CREATOR_NOW, ("relay", "seek", {"position": 0})),
    ("重新看一遍", SERIES_NOW, ("relay", "seek", {"position": 0})),
    ("快进三十秒", SERIES_NOW, ("relay", "seek", {"delta": 30})),
    ("跳到十分钟", SERIES_NOW, ("relay", "seek", {"position": 600})),
    ("暂停", LIVE_NOW, ("kind", "pause")),
    ("不看了", LIVE_NOW, ("kind", "cancel")),
    ("取消", LIVE_NOW, ("kind", "cancel")),
    ("算了", LIVE_NOW, ("kind", "cancel")),
    ("有什么节目", LIVE_NOW, ("kind", "list")),
    ("都有什么台", LIVE_NOW, ("kind", "list")),
    ("有哪些剧", LIVE_NOW, ("kind", "list")),
    ("我订阅了哪些UP主", LIVE_NOW, ("kind", "list")),
    ("我关注了哪些博主", LIVE_NOW, ("kind", "list")),
    ("有什么可以看的", LIVE_NOW, ("kind", "list")),
    ("搜视频猫和老鼠", LIVE_NOW, ("kind", "search")),
    ("我想看猫和老鼠的视频", LIVE_NOW, ("kind", "search")),
    ("搜UP主老番茄", LIVE_NOW, ("kind", "search")),
    ("大声一点", LIVE_NOW, ("kind", "volume")),
    ("最暗", LIVE_NOW, ("kind", "brightness")),
    ("只听声音", LIVE_NOW, ("kind", "audio_only")),
    ("关掉画面", LIVE_NOW, ("kind", "audio_only")),
    ("状态", LIVE_NOW, ("kind", "page")),
    ("还有多少电", LIVE_NOW, ("kind", "page")),
    ("配网", LIVE_NOW, ("kind", "setup")),
    ("我的投屏", LIVE_NOW, ("channel", "cast", "")),
    ("今天天气怎么样", LIVE_NOW, ("kind", "none")),
    ("随便", LIVE_NOW, ("kind", "none")),
]

RANDOM_POOLS = {
    "random_any": None,
    "random_cartoon": {c.id for c in CHANNELS if c.group == "动画影视"},
    "random_series": {"sr01", "sr02", "sr03"},
    "random_up": {"up01", "up02"},
    "random_music": {c.id for c in CHANNELS if c.group == "音乐"},
}


class SecondBatchTableTests(unittest.TestCase):
    def check(self, text, cat, expected):
        d = decide(text, cat)
        where = f"{text!r} -> {d}"
        kind = expected[0]
        if kind == "info":
            self.assertEqual((d.kind, d.do, d.relay, d.choices), ("info", None, None, None), where)
            self.assertIn(expected[1], d.reply, where)
        elif kind == "timer":
            self.assertEqual((d.kind, d.relay), ("timer", ("timer", expected[1])), where)
            self.assertIn(expected[2], d.reply, where)
            self.assertIsNone(d.do, where)
        elif kind == "timer_no":
            self.assertEqual((d.kind, d.relay, d.do), ("timer", None, None), where)
            self.assertIn(expected[1], d.reply, where)
        elif kind == "channel":
            self.assertEqual((d.kind, d.do), ("channel", {"channel": expected[1]}), where)
            self.assertEqual(d.lead, f"{NUMBER[expected[1]]:02d}", where)
            self.assertIn(expected[2], d.reply, where)
        elif kind == "here":
            self.assertEqual((d.kind, d.do), ("channel", None), where)
            self.assertTrue(d.reply.startswith("正在播"), where)
        elif kind == "relay":
            self.assertEqual(d.relay, (expected[1], expected[2]), where)
            self.assertIsNone(d.do, where)
        elif kind == "confirm":
            self.assertEqual(d.kind, "confirm", where)
            refs = {c["ref"][3:] for c in d.choices if "ref" in c}
            self.assertEqual(refs, expected[1], where)
            self.assertEqual(d.choices[-1], {"label": "算了", "lead": "", "do": {"page": "close"}}, where)
            self.assertIsNone(d.relay, "nothing is removed before the confirmation")
        elif expected[1] in RANDOM_POOLS:
            self.assertEqual(d.kind, "channel", where)
            chosen = d.do["channel"]
            self.assertNotEqual(chosen, cat.current, where)
            self.assertNotEqual(chosen, "cast", where)
            pool = RANDOM_POOLS[expected[1]]
            if pool is not None:
                self.assertIn(chosen, pool, where)
            self.assertTrue(d.reply.startswith("随机换到"), where)
        else:
            self.assertEqual(d.kind, expected[1], where)

    def test_table(self):
        self.assertGreaterEqual(len(CASES), 140)
        for text, cat, expected in CASES:
            with self.subTest(text=text, current=cat.current):
                if cat.rand is not None:
                    cat.rand.seed(7)
                self.check(text, cat, expected)

    def test_replies_fit_the_panel(self):
        for text, cat, _ in CASES:
            self.assertLessEqual(len(decide(text, cat).reply), 40, text)

    def test_random_never_repeats_the_current_channel(self):
        cat = catalog("ch000", rand=random.Random(1))
        seen = {decide("随便换个台", cat).do["channel"] for _ in range(200)}
        self.assertNotIn("ch000", seen)
        self.assertGreater(len(seen), 20, "spread over the list")
        only = intent.Catalog([c for c in CHANNELS if c.id in ("sr01", "ch000")], current="sr01",
                              rand=random.Random(1))
        self.assertEqual(decide("随机换台", only).do, {"channel": "ch000"})
        alone = intent.Catalog([c for c in CHANNELS if c.id == "sr01"], current="sr01")
        self.assertEqual(decide("随机放个动画片", alone).kind, "none")

    def test_back_skips_channels_no_longer_listed(self):
        cat = catalog("ch000", relay=True, back=["up09", "ch017"])
        self.assertEqual(decide("回到刚才的台", cat).do, {"channel": "ch017"})

    def test_live_resume_after_a_sleep_pause(self):
        # A live channel pauses only when the sleep timer fired; "carry on" wakes it.
        self.assertEqual(decide("继续", catalog("ch017", paused=True)).relay, ("resume", {}))
        self.assertEqual(decide("继续", catalog("ch017")).reply, "正在播")

    def test_help_lists_topics(self):
        d = decide("你能做什么", LIVE_NOW)
        self.assertEqual(d.kind, "help")
        self.assertEqual([c["ref"] for c in d.choices], [f"hp:{n}" for n in range(len(intent.HELP_TOPICS))])
        self.assertLessEqual(len(d.choices), intent.CHOICES_MAX)
        for n, (label, words) in enumerate(intent.HELP_TOPICS):
            topic = intent.help_topic(f"hp:{n}")
            self.assertEqual(topic.reply, words)
            self.assertLessEqual(len(words), 40, label)
        self.assertIsNone(intent.help_topic("hp:9"))
        joined = "".join(words for _, words in intent.HELP_TOPICS)
        for said in ("回到刚才的台", "30分钟后暂停", "现在播的是什么", "订阅这个UP主", "重播刚才那个视频"):
            self.assertIn(said, joined)

    def test_every_help_example_is_understood(self):
        for _, words in intent.HELP_TOPICS:
            for said in words.strip("「」").split("」「"):
                cat = SERIES_NOW if "集" in said or "刚才的台" in said else CAST_NOW
                # The examples name the user's channels; the fixture has others.
                said = said.replace("犬夜叉", "西游记").replace("王志安", "小Lin说")
                d = decide(said, cat)
                self.assertNotEqual(d.kind, "none", said)

    def test_timer_replies(self):
        self.assertEqual(intent.timer_reply("set", {"kind": "time", "left": 1800}, 30), "30 分钟后暂停")
        self.assertEqual(intent.timer_reply("cancel", {"kind": "time", "left": 30}), "已取消定时")
        self.assertEqual(intent.timer_reply("cancel", None), "没有定时")
        self.assertEqual(intent.timer_reply("status", None), "没有定时")
        self.assertEqual(intent.timer_reply("status", {"kind": "time", "left": 725}), "还有 13 分钟暂停")
        self.assertEqual(intent.timer_reply("status", {"kind": "time", "left": 3660}), "还有 1 小时 1 分钟暂停")
        self.assertEqual(intent.timer_reply("status", {"kind": "time", "left": 20}), "不到 1 分钟就暂停")
        self.assertEqual(intent.timer_reply("status", {"kind": "end", "what": "episode"}), "这集放完就暂停")
        self.assertEqual(intent.timer_reply("status", {"kind": "end", "what": "video"}), "这个视频放完就暂停")

    def test_confirmation_words(self):
        for said in ("确定", "删除", "是的", "对", "好的", "删吧", "确定。"):
            self.assertTrue(intent.confirms(said), said)
        for said in ("不要", "算了", "删除小Lin说", "湖南卫视"):
            self.assertFalse(intent.confirms(said), said)
        self.assertEqual(intent.removal_ref("rm:up04"), "up04")
        self.assertIsNone(intent.removal_ref("rm:ch001"))
        self.assertIsNone(intent.removal_ref("ls:..1:0"))


class SecondBatchRelay(FakeRelay):
    """FakeRelay with the second batch: catalogue extras and the four new calls."""

    def __init__(self, playing="ch000"):
        super().__init__(playing)
        self.extra = {"back": ["ch017"], "timer": None, "casts": ["经典欣赏"]}
        self.now = {}

    def catalog(self):
        answer = super().catalog()
        answer["playing"].update(self.now)
        answer.update(self.extra)
        return answer

    def timer(self, action, minutes=None):
        answer = self._do("timer", action, minutes)
        if "timer" not in self.answers:
            if action == "set":
                answer = {"ok": True, "timer": {"kind": "time", "left": minutes * 60}}
            elif action == "status":
                answer = {"ok": True, "timer": self.extra["timer"]}
        return answer

    def subscribe_playing(self, timeout):
        return self._do("subscribe", timeout)

    def unsubscribe(self, channel):
        return self._do("unsubscribe", channel)

    def recast(self, step=0, resume=False):
        return self._do("recast", step, resume)


class SecondBatchSessionTests(unittest.TestCase):
    def setUp(self):
        self.asr = FakeAsr()
        self.relay = SecondBatchRelay()
        self.log = []
        self.voice = VoiceService(self.asr, self.relay, self.log.append, threaded=False)
        self.uid = 0

    def say(self, text, current="ch000", **device):
        self.asr.text = text
        self.uid += 1
        ctx = context(current, **device)
        self.voice.take(ctl("begin", self.uid, rate=16000, codec="adpcm"), ctx)
        self.voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        self.voice.take(ctl("end", self.uid), ctx)
        return json.loads(self.voice.results()[-1])

    def pick(self, ref, current="ch000"):
        self.uid += 1
        self.voice.take(ctl("pick", self.uid, ref=ref), context(current))
        return json.loads(self.voice.results()[-1])

    def test_what_is_playing_from_the_relay(self):
        self.relay.playing = "sr01"
        self.relay.now = {"episode": 12, "title": "第12集", "position": 510, "duration": 1420, "total": 52}
        result = self.say("现在播的是什么", current="sr01")
        self.assertEqual((result["intent"], result["reply"]), ("info", "熊出没 第 12/52 集 · 已播 8:30 / 23:40"))
        self.assertNotIn("do", result)
        self.assertEqual(result["lead"], "06")
        self.assertEqual(self.relay.calls, [], "only an answer")
        # The relay's playing channel is not the device's (it just switched): no stale title.
        result = self.say("现在播的是什么", current="ch017")
        self.assertEqual(result["reply"], "湖南卫视（直播）")

    def test_back_to_the_last_channel(self):
        result = self.say("回到刚才的台", current="sr01")
        self.assertEqual((result["do"], result["reply"]), ({"channel": "ch017"}, "回到湖南卫视"))
        self.relay.extra["back"] = []
        self.assertEqual(self.say("回到刚才的台", current="sr01")["intent"], "none")
        self.relay.down = True
        self.assertIn("中转没回应", self.say("回到刚才的台", current="sr01")["reply"])

    def test_timer_calls_and_replies(self):
        result = self.say("半小时后关掉")
        self.assertEqual(self.relay.calls[-1], ("timer", "set", 30))
        self.assertEqual((result["intent"], result["reply"]), ("timer", "30 分钟后暂停"))
        self.relay.extra["timer"] = {"kind": "time", "left": 600}
        self.assertEqual(self.say("还剩多久关机")["reply"], "还有 10 分钟暂停")
        self.relay.answers["timer"] = {"ok": True, "timer": {"kind": "time", "left": 600}}
        self.assertEqual(self.say("取消定时")["reply"], "已取消定时")
        self.relay.answers["timer"] = {"ok": False, "error": "直播台没有「这集」，说「30分钟后暂停」吧"}
        self.assertIn("直播台", self.say("看完这集就停", current="sr01")["reply"])
        self.relay.down = True
        self.assertEqual(self.say("30分钟后暂停")["reply"], "视频中转没回应，稍后再试")

    def test_timer_without_a_relay(self):
        voice = VoiceService(self.asr, None, self.log.append, threaded=False)
        self.asr.text = "30分钟后暂停"
        ctx = context()
        voice.take(ctl("begin", 1, rate=16000, codec="adpcm"), ctx)
        voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        voice.take(ctl("end", 1), ctx)
        self.assertEqual(json.loads(voice.results()[0])["reply"], "这台电视没有连到视频中转")

    def test_subscribe_the_uploader_of_a_cast(self):
        self.relay.answers["subscribe"] = {"ok": True, "creator": {"id": "up02", "name": "老番茄"}}
        result = self.say("订阅这个UP主", current="cast")
        self.assertEqual(self.relay.calls[-1][0], "subscribe")
        self.assertEqual((result["intent"], result["reply"]), ("follow", "已订阅「老番茄」"))
        self.relay.answers["subscribe"] = {"ok": True, "pending": True}
        self.assertEqual(self.say("关注他", current="cast")["reply"], "正在订阅，稍后出现在频道列表里")
        self.relay.answers["subscribe"] = {"ok": False, "error": "最多只能订阅 12 个 UP 主"}
        self.assertEqual(self.say("订阅他", current="cast")["reply"], "最多只能订阅 12 个 UP 主")

    def test_removal_needs_a_confirmation(self):
        result = self.say("取消订阅小Lin说")
        self.assertEqual((result["intent"], result["reply"]), ("confirm", "确定删除「小Lin说」？"))
        self.assertEqual([c.get("ref") for c in result["choices"]], ["rm:up01", None])
        self.assertNotIn("unsubscribe", [c[0] for c in self.relay.calls])
        self.relay.answers["unsubscribe"] = {"ok": True, "id": "up01", "name": "小Lin说"}
        result = self.pick("rm:up01")
        self.assertEqual(self.relay.calls[-1], ("unsubscribe", "up01"))
        self.assertEqual(result["reply"], "已删除「小Lin说」")

    def test_removal_confirmed_by_voice(self):
        self.say("不看小Lin说了")
        self.relay.answers["unsubscribe"] = {"ok": True, "id": "up01", "name": "小Lin说"}
        result = self.say("确定")
        self.assertEqual(self.relay.calls[-1], ("unsubscribe", "up01"))
        self.assertEqual(result["reply"], "已删除「小Lin说」")
        # Only once: another "yes" is not a removal any more.
        before = len(self.relay.calls)
        self.assertNotEqual(self.say("确定")["intent"], "unsubscribe")
        self.assertEqual(len(self.relay.calls), before)

    def test_something_else_drops_the_confirmation(self):
        self.say("取消订阅小Lin说")
        self.say("大声一点")
        self.say("确定")
        self.assertNotIn("unsubscribe", [c[0] for c in self.relay.calls])

    def test_confirmation_expires(self):
        now = [100.0]
        voice = VoiceService(self.asr, self.relay, self.log.append, threaded=False, clock=lambda: now[0])
        self.voice = voice
        self.say("取消订阅小Lin说")
        now[0] += 31
        self.say("确定")
        self.assertNotIn("unsubscribe", [c[0] for c in self.relay.calls])
        # The button still works while the panel shows it.
        self.relay.answers["unsubscribe"] = {"ok": True, "name": "小Lin说"}
        self.say("取消订阅小Lin说")
        self.assertEqual(self.pick("rm:up01")["reply"], "已删除「小Lin说」")

    def test_only_offered_removals_are_accepted(self):
        result = self.pick("rm:up02")
        self.assertEqual(result["reply"], "这个选项已经过期了")
        self.assertEqual(self.relay.calls, [])

    def test_recast(self):
        self.relay.answers["recast"] = {"ok": True, "title": "经典欣赏", "resumed": False}
        result = self.say("重播刚才那个视频")
        self.assertEqual(self.relay.calls[-1], ("recast", 0, False))
        self.assertEqual((result["intent"], result["reply"], result["label"]), ("cast", "投屏中…", "经典欣赏"))
        self.relay.answers["recast"] = {"ok": True, "title": "经典欣赏", "resumed": True}
        self.assertEqual(self.say("继续看刚才投屏的")["reply"], "接着放投屏")
        self.assertEqual(self.relay.calls[-1], ("recast", 0, True))
        self.relay.answers["recast"] = {"ok": False, "error": "投屏历史里没有更早的了"}
        self.assertEqual(self.say("上上个投屏")["reply"], "投屏历史里没有更早的了")
        self.assertEqual(self.relay.calls[-1], ("recast", 2, False))

    def test_help_topics_by_pick(self):
        result = self.say("你能做什么")
        self.assertEqual(len(result["choices"]), len(intent.HELP_TOPICS))
        result = self.pick("hp:3")
        self.assertEqual((result["intent"], result["reply"]), ("help", intent.HELP_TOPICS[3][1]))
        self.assertNotIn("choices", result)

    def test_series_progress_from_the_relay(self):
        rows = self.relay.catalog()["series"]
        rows[0].update(next=7, position=330)
        self.relay.catalog = lambda: {"ok": True, "series": rows, "creators": [],
                                      "playing": {"channel": "ch000", "episode": 0, "paused": False}}
        self.assertEqual(self.say("熊出没看到第几集了")["reply"], "熊出没看到第 7/52 集 5:30")


if __name__ == "__main__":
    unittest.main()
