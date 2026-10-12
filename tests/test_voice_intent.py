"""The voice intent engine: about 200 spoken phrasings against a fixture channel table.

Each row is an utterance as SenseVoice might return it (with or without
punctuation, Chinese or Arabic numerals, homophones, fillers) and the decision
the rules must reach. See server/voice/intent.py and
docs/superpowers/specs/2026-10-12-voice-control-design.md section 3.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.channel_groups import classify
from server.voice import intent, match, numbers
from server.voice.intent import Catalog, Channel, Series, decide

ROWS = [("cast", "我的投屏"), ("up01", "小Lin说"), ("up02", "老高与小茉"), ("sr01", "熊出没"),
        ("sr02", "熊出没之探险日记"), ("sr03", "西游记")]
ROWS += [(f"ch{n:03d}", f"CCTV{n + 1}") for n in range(17)]
ROWS += [("ch5p", "CCTV-5+ 体育赛事"), ("ch017", "湖南卫视"), ("ch018", "浙江卫视"), ("ch019", "江苏卫视"),
         ("ch020", "东方卫视"), ("ch021", "香港卫视"), ("ch022", "东方卫视4K"), ("ch023", "浙江卫视4K"),
         ("ch028", "凤凰中文"), ("ch029", "凤凰资讯"), ("ch030", "凤凰香港"), ("ch037", "CGTN英语"),
         ("ch038", "CGTN记录"), ("ch039", "CNA"), ("ch050", "猫和老鼠"), ("ch051", "哆啦A梦"),
         ("ch070", "薛之谦精选30首(2小时)"), ("ch077", "王菲精选145首(10小时)"),
         ("ch073", "周深精选72首(5小时)"), ("ch084", "BBC News (1080p)"), ("gd1", "广东体育"),
         ("ch035", "中国交通"), ("ch049", "纪实科教")]
CHANNELS = [Channel(cid, name, n, classify(cid, name)) for n, (cid, name) in enumerate(ROWS, 1)]
NUMBER = {c.id: c.number for c in CHANNELS}
SERIES = [Series("sr01", "熊出没", 52), Series("sr02", "熊出没之探险日记", 26), Series("sr03", "西游记", 25)]


def catalog(current="ch000", **overrides):
    values = dict(series=SERIES, current=current, volume=50, brightness=2, steps=5, audio_only=0)
    values.update(overrides)
    return Catalog(CHANNELS, **values)


LIVE = catalog()
SERIES_PLAYING = catalog("sr01", episode=3)
CREATOR_PLAYING = catalog("up01")

# (utterance, catalogue, expected). Expected forms:
#   ("channel", id)            switch at once to id
#   ("here", id)               already on id: a reply, nothing to do
#   ("choices", {ids})         candidates; exactly these channel ids, in any order
#   ("choices_has", {ids})     candidates that include at least these
#   ("volume", n) / ("brightness", n) / ("audio_only", n) / ("page", name)
#   ("relay", action, params)  the relay does it
#   ("episode", series, n)     relay episode pick, and a switch when not already there
#   ("ep_choices", {refs})
#   (kind,)                    just the decision kind, nothing to do (a reply)
CASES = [
    # ---- channels: exact names, fillers, verbs ----
    ("湖南卫视", LIVE, ("channel", "ch017")),
    ("我要看湖南卫视", LIVE, ("channel", "ch017")),
    ("我要看湖南卫视。", LIVE, ("channel", "ch017")),
    ("请帮我切换到湖南卫视", LIVE, ("channel", "ch017")),
    ("换到湖南卫视吧", LIVE, ("channel", "ch017")),
    ("打开湖南卫视频道", LIVE, ("channel", "ch017")),
    ("播放湖南卫视", LIVE, ("channel", "ch017")),
    ("看湖南卫视", LIVE, ("channel", "ch017")),
    ("湖南卫视，谢谢", LIVE, ("channel", "ch017")),
    ("小智小智，湖南卫视", LIVE, ("channel", "ch017")),
    ("浙江卫视", LIVE, ("channel", "ch018")),
    ("江苏卫视", LIVE, ("channel", "ch019")),
    ("东方卫视", LIVE, ("channel", "ch020")),
    ("香港卫视", LIVE, ("channel", "ch021")),
    ("东方卫视4K", LIVE, ("channel", "ch022")),
    ("东方卫视 4k", LIVE, ("channel", "ch022")),
    ("浙江卫视四K", LIVE, ("channel", "ch023")),
    ("凤凰中文", LIVE, ("channel", "ch028")),
    ("凤凰资讯", LIVE, ("channel", "ch029")),
    ("凤凰香港", LIVE, ("channel", "ch030")),
    ("猫和老鼠", LIVE, ("channel", "ch050")),
    ("我想看猫和老鼠", LIVE, ("channel", "ch050")),
    ("哆啦A梦", LIVE, ("channel", "ch051")),
    ("哆啦a梦", LIVE, ("channel", "ch051")),
    ("中国交通", LIVE, ("channel", "ch035")),
    ("纪实科教", LIVE, ("channel", "ch049")),
    ("广东体育", LIVE, ("channel", "gd1")),
    ("我的投屏", LIVE, ("channel", "cast")),
    ("CNA", LIVE, ("channel", "ch039")),
    # ---- satellite channels without "weishi" ----
    ("湖南", LIVE, ("channel", "ch017")),
    ("湖南台", LIVE, ("channel", "ch017")),
    ("看湖南台", LIVE, ("channel", "ch017")),
    ("湖南电视台", LIVE, ("channel", "ch017")),
    ("浙江", LIVE, ("channel", "ch018")),
    ("江苏台", LIVE, ("channel", "ch019")),
    ("东方台", LIVE, ("channel", "ch020")),
    ("上海卫视", LIVE, ("channel", "ch020")),
    ("东方4K", LIVE, ("channel", "ch022")),
    ("凤凰卫视", LIVE, ("channel", "ch028")),
    ("凤凰台", LIVE, ("channel", "ch028")),
    # ---- CCTV aliases ----
    ("中央五套", LIVE, ("channel", "ch004")),
    ("中央5套", LIVE, ("channel", "ch004")),
    ("央视五套", LIVE, ("channel", "ch004")),
    ("央视五台", LIVE, ("channel", "ch004")),
    ("中央五台", LIVE, ("channel", "ch004")),
    ("中央电视台五套", LIVE, ("channel", "ch004")),
    ("CCTV5", LIVE, ("channel", "ch004")),
    ("cctv5", LIVE, ("channel", "ch004")),
    ("CCTV-5", LIVE, ("channel", "ch004")),
    ("CCTV 5", LIVE, ("channel", "ch004")),
    ("CCTV五", LIVE, ("channel", "ch004")),
    ("五套", LIVE, ("channel", "ch004")),
    ("我要看五套", LIVE, ("channel", "ch004")),
    ("中央五", LIVE, ("channel", "ch004")),
    ("央视体育", LIVE, ("channel", "ch004")),
    ("中央舞套", LIVE, ("channel", "ch004")),
    ("中央一套", catalog("ch017"), ("channel", "ch000")),
    ("中央1套", LIVE, ("here", "ch000")),
    ("CCTV1", catalog("ch017"), ("channel", "ch000")),
    ("中央二套", LIVE, ("channel", "ch001")),
    ("央视财经", LIVE, ("channel", "ch001")),
    ("中央三套", LIVE, ("channel", "ch002")),
    ("中央六套", LIVE, ("channel", "ch005")),
    ("央视电影", LIVE, ("channel", "ch005")),
    ("中央八套", LIVE, ("channel", "ch007")),
    ("中央十套", LIVE, ("channel", "ch009")),
    ("中央十一套", LIVE, ("channel", "ch010")),
    ("中央十二套", LIVE, ("channel", "ch011")),
    ("中央十三套", LIVE, ("channel", "ch012")),
    ("中央十三", LIVE, ("channel", "ch012")),
    ("央视新闻", LIVE, ("channel", "ch012")),
    ("CCTV13", LIVE, ("channel", "ch012")),
    ("cctv 13", LIVE, ("channel", "ch012")),
    ("中央十四套", LIVE, ("channel", "ch013")),
    ("央视少儿", LIVE, ("channel", "ch013")),
    ("中央十五", LIVE, ("channel", "ch014")),
    ("中央十七套", LIVE, ("channel", "ch016")),
    ("中央五套加", LIVE, ("channel", "ch5p")),
    ("中央五加", LIVE, ("channel", "ch5p")),
    ("五加", LIVE, ("channel", "ch5p")),
    ("5+", LIVE, ("channel", "ch5p")),
    ("CCTV5+", LIVE, ("channel", "ch5p")),
    ("CCTV5加", LIVE, ("channel", "ch5p")),
    ("央视五加", LIVE, ("channel", "ch5p")),
    ("体育赛事", LIVE, ("channel", "ch5p")),
    # ---- homophones and near misses (toneless pinyin) ----
    ("湖南为是", LIVE, ("channel", "ch017")),
    ("胡南卫视", LIVE, ("channel", "ch017")),
    ("湖南卫士", LIVE, ("channel", "ch017")),
    ("浙江为视", LIVE, ("channel", "ch018")),
    ("江苏为是", LIVE, ("channel", "ch019")),
    ("东方为视", LIVE, ("channel", "ch020")),
    ("凤黄中文", LIVE, ("channel", "ch028")),
    ("猫和老树", LIVE, ("channel", "ch050")),
    ("哆拉A梦", LIVE, ("channel", "ch051")),
    ("多啦a梦", LIVE, ("channel", "ch051")),
    ("中国交铜", LIVE, ("channel", "ch035")),
    ("熊初没", LIVE, ("channel", "sr01")),
    ("西游计", LIVE, ("channel", "sr03")),
    ("小林说", LIVE, ("channel", "up01")),
    ("小lin说", LIVE, ("channel", "up01")),
    ("老高与小末", LIVE, ("channel", "up02")),
    ("老高和小茉", LIVE, ("channel", "up02")),
    ("老高", LIVE, ("channel", "up02")),
    ("周深", LIVE, ("channel", "ch073")),
    ("我想听周深的歌", LIVE, ("channel", "ch073")),
    ("薛之谦", LIVE, ("channel", "ch070")),
    ("来点王菲的歌", LIVE, ("channel", "ch077")),
    ("王菲精选", LIVE, ("channel", "ch077")),
    ("CGTN", LIVE, ("channel", "ch037")),
    ("BBC", LIVE, ("channel", "ch084")),
    ("BBC新闻", LIVE, ("channel", "ch084")),
    # ---- ambiguity: candidates ----
    ("凤凰", LIVE, ("choices", {"ch028", "ch029", "ch030"})),
    ("看凤凰", LIVE, ("choices", {"ch028", "ch029", "ch030"})),
    ("体育", LIVE, ("choices_has", {"ch004", "ch5p", "gd1"})),
    ("看体育", LIVE, ("choices_has", {"ch004", "ch5p", "gd1"})),
    ("熊出没探险", LIVE, ("choices", {"sr01", "sr02"})),
    # The longer series is listed first, but the shorter name is in the words too: ask.
    ("熊出没之探险", LIVE, ("choices", {"sr01", "sr02"})),
    ("熊出没之探险日记", LIVE, ("channel", "sr02")),
    ("cgtn纪录片", LIVE, ("channel", "ch038")),
    ("广东卫视", LIVE, ("choices_has", {"ch017", "ch018"})),
    # ---- groups ----
    ("卫视", LIVE, ("choices_has", {"ch017", "ch018", "ch019"})),
    ("看卫视", LIVE, ("choices_has", {"ch017"})),
    ("央视", LIVE, ("choices_has", {"ch000"})),
    ("动画片", LIVE, ("choices_has", {"sr01", "ch050"})),
    ("我想看动画", LIVE, ("choices_has", {"sr01"})),
    ("港澳台", LIVE, ("choices_has", {"ch028", "ch021"})),
    ("音乐", LIVE, ("choices_has", {"ch070", "ch077"})),
    ("听歌", LIVE, ("choices_has", {"ch070"})),
    ("UP主", LIVE, ("choices", {"up01", "up02"})),
    # ---- channel numbers ----
    ("第十二台", LIVE, ("channel", ROWS[11][0])),
    ("第12台", LIVE, ("channel", ROWS[11][0])),
    ("十二台", LIVE, ("channel", ROWS[11][0])),
    ("频道十二", LIVE, ("channel", ROWS[11][0])),
    ("十二频道", LIVE, ("channel", ROWS[11][0])),
    ("第二十五个台", LIVE, ("channel", ROWS[24][0])),
    ("换到第3台", LIVE, ("channel", ROWS[2][0])),
    ("第一个台", catalog("ch017"), ("channel", ROWS[0][0])),
    ("第一百台", LIVE, ("channel_missing",)),
    # ---- channel steps ----
    ("下一个台", LIVE, ("channel", "ch001")),
    ("下一个频道", LIVE, ("channel", "ch001")),
    ("换个台", LIVE, ("channel", "ch001")),
    ("上一个台", LIVE, ("channel", "ch5p")),
    ("上个频道", catalog("ch001"), ("channel", "ch000")),
    ("下一个", LIVE, ("channel", "ch001")),
    # ---- series and episodes ----
    ("熊出没", LIVE, ("channel", "sr01")),
    ("熊出没第三集", LIVE, ("episode", "sr01", 3)),
    ("熊出没 第三集", LIVE, ("episode", "sr01", 3)),
    ("熊出没第3集", LIVE, ("episode", "sr01", 3)),
    ("我要看熊出没第三集", LIVE, ("episode", "sr01", 3)),
    ("播放熊出没的第十二集", LIVE, ("episode", "sr01", 12)),
    ("熊出没第二十集", LIVE, ("episode", "sr01", 20)),
    ("熊出没3集", LIVE, ("episode", "sr01", 3)),
    ("熊初没第三集", LIVE, ("episode", "sr01", 3)),
    ("熊出没之探险日记第二集", LIVE, ("episode", "sr02", 2)),
    ("探险日记第二集", LIVE, ("episode", "sr02", 2)),
    ("西游记第一集", LIVE, ("episode", "sr03", 1)),
    ("西游记第一百集", LIVE, ("episode_out_of_range", "sr03")),
    ("西游记最新一集", LIVE, ("episode", "sr03", 25)),
    ("西游记的最后一集", LIVE, ("episode", "sr03", 25)),
    ("第五集", SERIES_PLAYING, ("episode", "sr01", 5)),
    ("看第五集", SERIES_PLAYING, ("episode", "sr01", 5)),
    ("第五集", LIVE, ("episode_no_series",)),
    ("熊出没第三集", SERIES_PLAYING, ("episode", "sr01", 3)),
    ("熊出没探险第三集", LIVE, ("ep_choices", {"ep:sr01:3", "ep:sr02:3"})),
    ("葫芦娃第三集", LIVE, ("none",)),
    # ---- episode steps and playback (relay) ----
    ("下一集", SERIES_PLAYING, ("relay", "next", {})),
    ("下集", SERIES_PLAYING, ("relay", "next", {})),
    ("播下一集", SERIES_PLAYING, ("relay", "next", {})),
    ("下一个", CREATOR_PLAYING, ("relay", "next", {})),
    ("下一个视频", CREATOR_PLAYING, ("relay", "next", {})),
    ("跳过这个", CREATOR_PLAYING, ("relay", "next", {})),
    ("上一集", SERIES_PLAYING, ("relay", "previous", {})),
    ("上一集", CREATOR_PLAYING, ("previous",)),
    ("暂停", SERIES_PLAYING, ("relay", "pause", {})),
    ("暂停一下", SERIES_PLAYING, ("relay", "pause", {})),
    ("停一下", CREATOR_PLAYING, ("relay", "pause", {})),
    ("先停", CREATOR_PLAYING, ("relay", "pause", {})),
    ("继续", SERIES_PLAYING, ("relay", "resume", {})),
    ("继续播放", SERIES_PLAYING, ("relay", "resume", {})),
    ("接着播", CREATOR_PLAYING, ("relay", "resume", {})),
    ("播放", CREATOR_PLAYING, ("relay", "resume", {})),
    ("快进", SERIES_PLAYING, ("relay", "seek", {"delta": 30})),
    ("快进三十秒", SERIES_PLAYING, ("relay", "seek", {"delta": 30})),
    ("快进1分钟", SERIES_PLAYING, ("relay", "seek", {"delta": 60})),
    ("快进两分钟", SERIES_PLAYING, ("relay", "seek", {"delta": 120})),
    ("后退十秒", SERIES_PLAYING, ("relay", "seek", {"delta": -10})),
    ("快退", CREATOR_PLAYING, ("relay", "seek", {"delta": -30})),
    ("倒回去", CREATOR_PLAYING, ("relay", "seek", {"delta": -30})),
    ("跳到十分钟", CREATOR_PLAYING, ("relay", "seek", {"position": 600})),
    ("从头播", SERIES_PLAYING, ("relay", "seek", {"position": 0})),
    ("重播这集", SERIES_PLAYING, ("relay", "seek", {"position": 0})),
    ("暂停", LIVE, ("pause",)),
    ("快进三十秒", LIVE, ("seek",)),
    ("下一集", LIVE, ("next",)),
    ("上一集", LIVE, ("previous",)),
    ("继续", LIVE, ("resume",)),
    # ---- volume ----
    ("大声一点", LIVE, ("volume", 60)),
    ("大声点", LIVE, ("volume", 60)),
    ("声音大一点", LIVE, ("volume", 60)),
    ("音量调大", LIVE, ("volume", 60)),
    ("调大音量", LIVE, ("volume", 60)),
    ("再大声一点", LIVE, ("volume", 60)),
    ("听不见", LIVE, ("volume", 60)),
    ("大声很多", LIVE, ("volume", 80)),
    ("声音大很多", LIVE, ("volume", 80)),
    ("小声一点", LIVE, ("volume", 40)),
    ("小声点", LIVE, ("volume", 40)),
    ("声音小一点", LIVE, ("volume", 40)),
    ("轻一点", LIVE, ("volume", 40)),
    ("太吵了", LIVE, ("volume", 40)),
    ("音量调小", LIVE, ("volume", 40)),
    ("音量调到三十", LIVE, ("volume", 30)),
    ("音量调整60%。", LIVE, ("volume", 60)),
    ("音量调整为百分之四十", LIVE, ("volume", 40)),
    ("声音调节到20", LIVE, ("volume", 20)),
    ("音量改成七十", LIVE, ("volume", 70)),
    ("音量30", LIVE, ("volume", 30)),
    ("音量七", LIVE, ("volume", 70)),
    ("声音开到八", LIVE, ("volume", 80)),
    ("音量百分之六十", LIVE, ("volume", 60)),
    ("音量一百", LIVE, ("volume", 100)),
    ("最大声", LIVE, ("volume", 100)),
    ("音量最小", LIVE, ("volume", 10)),
    ("静音", LIVE, ("volume", 0)),
    ("取消静音", catalog(volume=0, unmute_volume=70), ("volume", 70)),
    ("打开声音", catalog(volume=0), ("volume", 50)),
    ("大声一点", catalog(volume=100), ("volume_same",)),
    ("音量五", LIVE, ("volume_same",)),
    ("大声一点", catalog(volume=None), ("volume", 60)),
    # ---- brightness (device step 0 is the brightest of 5) ----
    ("亮一点", LIVE, ("brightness", 1)),
    ("调亮一点", LIVE, ("brightness", 1)),
    ("太暗了", LIVE, ("brightness", 1)),
    ("暗一点", LIVE, ("brightness", 3)),
    ("调暗", LIVE, ("brightness", 3)),
    ("太亮了", LIVE, ("brightness", 3)),
    ("亮度五", LIVE, ("brightness", 0)),
    ("亮度一", LIVE, ("brightness", 4)),
    ("亮度调到4", LIVE, ("brightness", 1)),
    ("最亮", LIVE, ("brightness", 0)),
    ("最暗", LIVE, ("brightness", 4)),
    ("亮一点", catalog(brightness=0), ("brightness_same",)),
    ("亮一点", catalog(steps=None, brightness=None), ("brightness_same",)),
    # ---- audio only ----
    ("只听声音", LIVE, ("audio_only", 1)),
    ("关掉画面", LIVE, ("audio_only", 1)),
    ("不要画面", LIVE, ("audio_only", 1)),
    ("打开画面", catalog(audio_only=1), ("audio_only", 0)),
    ("显示画面", catalog(audio_only=1), ("audio_only", 0)),
    ("退出只听声音", catalog(audio_only=1), ("audio_only", 0)),
    ("只听声音", catalog(audio_only=1), ("audio_only_same",)),
    # ---- pages, setup, cancel, help ----
    ("状态", LIVE, ("page", "status")),
    ("打开状态页", LIVE, ("page", "status")),
    ("信号怎么样", LIVE, ("page", "status")),
    ("还有多少电", LIVE, ("page", "status")),
    ("我要配网", LIVE, ("setup",)),
    ("配网", LIVE, ("setup",)),
    ("连接WiFi", LIVE, ("setup",)),
    ("换个网络", LIVE, ("setup",)),
    ("连Wi-Fi", LIVE, ("setup",)),
    ("算了", LIVE, ("cancel",)),
    ("取消", LIVE, ("cancel",)),
    ("不用了", LIVE, ("cancel",)),
    ("你能做什么", LIVE, ("help",)),
    ("帮助", LIVE, ("help",)),
    # ---- not understood ----
    ("那个那个什么来着", LIVE, ("none",)),
    ("今天天气怎么样", LIVE, ("none",)),
    ("", LIVE, ("none",)),
    ("。", LIVE, ("none",)),
    ("<|zh|><|NEUTRAL|><|Speech|><|withitn|>湖南卫视", LIVE, ("channel", "ch017")),
]


class IntentTableTests(unittest.TestCase):
    def check(self, text, cat, expected):
        d = decide(text, cat)
        kind = expected[0]
        where = f"{text!r} -> {d}"
        if kind == "channel":
            self.assertEqual((d.kind, d.do), ("channel", {"channel": expected[1]}), where)
            self.assertEqual(d.lead, f"{NUMBER[expected[1]]:02d}", where)
            self.assertTrue(d.label, where)
        elif kind == "here":
            self.assertEqual((d.kind, d.do), ("channel", None), where)
            self.assertTrue(d.reply.startswith("正在播"), where)
        elif kind in ("choices", "choices_has"):
            self.assertEqual(d.kind, "choices", where)
            # A long group ends with a "next page" row (a ref, not a channel).
            ids = {c["do"]["channel"] for c in d.choices if "do" in c}
            self.assertLessEqual(len(d.choices), intent.CHOICES_MAX, where)
            if kind == "choices":
                self.assertEqual(ids, expected[1], where)
            else:
                self.assertLessEqual(expected[1], ids, where)
        elif kind in ("volume", "brightness", "audio_only", "page"):
            self.assertEqual((d.kind, d.do), (kind, {kind: expected[1]}), where)
        elif kind in ("volume_same", "brightness_same", "audio_only_same"):
            self.assertEqual((d.kind, d.do), (kind[:-5], None), where)
        elif kind == "relay":
            self.assertEqual(d.relay, (expected[1], expected[2]), where)
            self.assertIsNone(d.do, where)
        elif kind == "episode":
            self.assertEqual(d.relay, ("episode", {"series": expected[1], "episode": expected[2]}), where)
            self.assertEqual(d.then_channel, "" if cat.current == expected[1] else expected[1], where)
            self.assertEqual(d.lead, f"{expected[2]:02d}", where)
        elif kind == "ep_choices":
            self.assertEqual(d.kind, "choices", where)
            self.assertEqual({c["ref"] for c in d.choices}, expected[1], where)
        elif kind == "episode_out_of_range":
            self.assertEqual((d.kind, d.relay), ("episode", None), where)
            self.assertIn("25", d.reply, where)
        elif kind == "episode_no_series":
            self.assertEqual((d.kind, d.relay), ("episode", None), where)
        elif kind == "channel_missing":
            self.assertEqual((d.kind, d.do), ("channel", None), where)
            self.assertIn("没有第 100 台", d.reply, where)
        elif kind == "setup":
            self.assertEqual(d.kind, "setup", where)
            self.assertEqual(d.choices[0]["do"], {"setup": 1}, where)
            self.assertIsNone(d.do, "setup needs a confirmation first")
        elif kind == "cancel":
            self.assertEqual((d.kind, d.do), ("cancel", {"page": "close"}), where)
        else:
            self.assertEqual(d.kind, kind, where)
            self.assertIsNone(d.do, where)
            self.assertIsNone(d.relay, where)
            self.assertTrue(d.reply, where)

    def test_table(self):
        self.assertGreaterEqual(len(CASES), 200)
        for text, cat, expected in CASES:
            with self.subTest(text=text, current=cat.current):
                self.check(text, cat, expected)

    def test_replies_are_short_chinese(self):
        for text, cat, _ in CASES:
            d = decide(text, cat)
            self.assertLessEqual(len(d.reply), 40, text)

    def test_choices_carry_label_and_number(self):
        d = decide("凤凰", LIVE)
        self.assertEqual(d.choices[0], {"label": "凤凰中文", "lead": f"{NUMBER['ch028']:02d}",
                                        "do": {"channel": "ch028"}})

    def test_labels_drop_the_bracket_note(self):
        self.assertEqual(decide("薛之谦", LIVE).label, "薛之谦精选30首")

    def test_series_without_the_relay_still_switches(self):
        cat = catalog(series=[])
        self.assertEqual(decide("熊出没", cat).do, {"channel": "sr01"})
        d = decide("熊出没第三集", cat)
        self.assertEqual(d.relay, ("episode", {"series": "sr01", "episode": 3}))
        self.assertIn("不知道", decide("熊出没最新一集", cat).reply)


# ---- listing a category ----
# Today's channel set, with the site each relay channel comes from (the relay catalogue's "site").
TODAY_ROWS = [("cast", "我的投屏", ""), ("up01", "小Lin说", "bilibili"), ("up02", "老高与小茉", "bilibili"),
              ("up03", "Dulaa Music", "youtube"), ("up04", "王志安", "youtube"),
              ("up05", "罗翔说刑法", "bilibili"), ("up06", "半佛仙人", "bilibili"),
              ("up07", "李永乐老师", "bilibili"), ("up08", "影视飓风", "bilibili"),
              ("up09", "何同学", "bilibili"), ("sr01", "熊出没", "bilibili"), ("sr02", "西游记", "bilibili"),
              ("sr03", "Peppa Pig 小猪佩奇", "youtube")]
TODAY_ROWS += [(f"ch{n:03d}", f"CCTV{n + 1}", "") for n in range(17)]
TODAY_ROWS += [("ch017", "湖南卫视", ""), ("ch018", "浙江卫视", ""), ("ch028", "凤凰中文", ""),
               ("ch021", "香港卫视", ""), ("ch037", "CGTN英语", ""), ("ch039", "CNA", ""),
               ("ch070", "薛之谦精选30首(2小时)", ""), ("ch077", "王菲精选145首(10小时)", ""),
               ("ch050", "猫和老鼠", ""), ("ch090", "中国交通", "")]
TODAY_CHANNELS = [Channel(cid, name, n, classify(cid, name), site=site)
                  for n, (cid, name, site) in enumerate(TODAY_ROWS, 1)]
TODAY = Catalog(TODAY_CHANNELS, current="ch000", volume=50)
NO_YT_SERIES = Catalog([c for c in TODAY_CHANNELS if c.id != "sr03"], current="ch000")
# The relay did not answer: relay channels without a site.
NO_SITES = Catalog([Channel(c.id, c.name, c.number, c.group) for c in TODAY_CHANNELS], current="ch000")
YT = {"up03", "up04", "sr03"}
YT_UP = {"up03", "up04"}
BL_UP = {"up01", "up02", "up05", "up06", "up07", "up08", "up09"}
SERIES_IDS = {"sr01", "sr02", "sr03"}
GROUPS_ALL = "groups"

# (utterance, catalogue, expected): a set of channel ids that are listed (over every page),
# GROUPS_ALL for the overview of groups, or ("none", words in the reply) / ("channel", id).
LIST_CASES = [
    # ---- by site ----
    ("我有哪些youtube频道告诉我。", TODAY, YT),
    ("我有哪些YouTube频道", TODAY, YT),
    ("我有哪些YouTube频道？", TODAY, YT),
    ("有哪些油管频道", TODAY, YT),
    ("有哪些优兔频道", TODAY, YT),
    ("油管上有什么", TODAY, YT),
    ("YouTube 有哪些", TODAY, YT),
    ("YouTube上都有什么台", TODAY, YT),
    ("列出YouTube频道", TODAY, YT),
    ("列出所有的YouTube频道", TODAY, YT),
    ("显示油管的频道", TODAY, YT),
    ("告诉我有哪些YouTube频道", TODAY, YT),
    ("帮我列一下YouTube的频道", TODAY, YT),
    ("有没有YouTube频道", TODAY, YT),
    ("我在YouTube上订阅了哪些", TODAY, YT),
    ("YouTube频道", TODAY, YT),
    ("youtube的频道有哪些呢", TODAY, YT),
    ("我有哪些YouTube的频道？告诉我一下。", TODAY, YT),
    ("YouTube的频道都有哪些", TODAY, YT),
    ("哪些频道是YouTube的", TODAY, YT),
    ("小智小智，有哪些YouTube频道", TODAY, YT),
    ("我想知道有哪些YouTube频道", TODAY, YT),
    ("我的youtube频道", TODAY, YT),
    ("B站有哪些频道", TODAY, BL_UP | {"sr01", "sr02"}),
    ("哔哩哔哩上有什么", TODAY, BL_UP | {"sr01", "sr02"}),
    # ---- creators ----
    ("B站的UP主有哪些", TODAY, BL_UP),
    ("b站的up主有哪些？", TODAY, BL_UP),
    ("哔哩哔哩的up主有哪些", TODAY, BL_UP),
    ("bilibili有哪些up主", TODAY, BL_UP),
    ("B站UP主", TODAY, BL_UP),
    ("你给我列一下B站的UP主", TODAY, BL_UP),
    ("B 站 UP 主有哪些", TODAY, BL_UP),
    ("油管的up主有哪些", TODAY, YT_UP),
    ("YouTube上的博主有哪些", TODAY, YT_UP),
    ("我订阅了哪些UP主", TODAY, BL_UP | YT_UP),
    ("我订阅了哪些UP主。", TODAY, BL_UP | YT_UP),
    ("有哪些up主", TODAY, BL_UP | YT_UP),
    ("我关注了哪些博主", TODAY, BL_UP | YT_UP),
    ("UP主都有谁", TODAY, BL_UP | YT_UP),
    ("列出所有UP主", TODAY, BL_UP | YT_UP),
    # ---- series ----
    ("有哪些剧", TODAY, SERIES_IDS),
    ("有什么剧集", TODAY, SERIES_IDS),
    ("都有哪些电视剧", TODAY, SERIES_IDS),
    ("我有哪些连续剧", TODAY, SERIES_IDS),
    ("YouTube有哪些剧集", TODAY, {"sr03"}),
    ("B站的剧集有哪些", TODAY, {"sr01", "sr02"}),
    ("有哪些YouTube的剧", NO_YT_SERIES, ("none", "还没有 YouTube 剧集")),
    ("有哪些YouTube的剧", NO_SITES, ("none", "中转")),
    ("我有哪些youtube频道告诉我。", NO_SITES, ("none", "中转")),
    # ---- groups ----
    ("有哪些动画片", TODAY, {"sr01", "sr02", "sr03", "ch050"}),
    ("有什么动画", TODAY, {"sr01", "sr02", "sr03", "ch050"}),
    ("有没有动画片", TODAY, {"sr01", "sr02", "sr03", "ch050"}),
    ("有什么好看的电影", TODAY, {"sr01", "sr02", "sr03", "ch050"}),
    ("有哪些卫视", TODAY, {"ch017", "ch018"}),
    ("卫视有哪些", TODAY, {"ch017", "ch018"}),
    ("央视有哪些台", TODAY, {f"ch{n:03d}" for n in range(17)}),
    ("中央台有哪些频道", TODAY, {f"ch{n:03d}" for n in range(17)}),
    ("CCTV有哪些频道", TODAY, {f"ch{n:03d}" for n in range(17)}),
    ("列出央视的台", TODAY, {f"ch{n:03d}" for n in range(17)}),
    ("有什么音乐台", TODAY, {"ch070", "ch077"}),
    ("有什么歌可以听", TODAY, {"ch070", "ch077"}),
    ("请问有哪些卫视台", TODAY, {"ch017", "ch018"}),
    ("有哪些台湾台", TODAY, {"ch028", "ch021"}),
    ("有哪些港台频道", TODAY, {"ch028", "ch021"}),
    ("有哪些香港台", TODAY, {"ch028", "ch021"}),
    ("国际频道", TODAY, {"ch037", "ch039"}),
    ("有哪些国际台", TODAY, {"ch037", "ch039"}),
    ("国外的台有哪些", TODAY, {"ch037", "ch039"}),
    ("其他频道有哪些", TODAY, {"ch090"}),
    ("有哪些动画片", Catalog([c for c in TODAY_CHANNELS if c.group != "动画影视"]), ("none", "动画影视")),
    # ---- everything: the groups first ----
    ("都有什么台", TODAY, GROUPS_ALL),
    ("有什么可以看的", TODAY, GROUPS_ALL),
    ("有什么可以看的。", TODAY, GROUPS_ALL),
    ("有哪些频道", TODAY, GROUPS_ALL),
    ("你这里有什么节目", TODAY, GROUPS_ALL),
    ("一共有多少个台", TODAY, GROUPS_ALL),
    ("列出所有频道", TODAY, GROUPS_ALL),
    ("告诉我都有哪些台", TODAY, GROUPS_ALL),
    ("有什么", TODAY, GROUPS_ALL),
    # ---- not a list: a channel is named, or two different groups ----
    ("有没有湖南卫视", TODAY, ("channel", "ch017")),
    ("有没有熊出没", TODAY, ("channel", "sr01")),
    ("央视电影", TODAY, ("channel", "ch005")),
    ("电视剧", TODAY, ("channel", "ch007")),
    ("我的投屏", TODAY, ("channel", "cast")),
    ("看王志安", TODAY, ("channel", "up04")),
    ("王菲的歌", TODAY, ("channel", "ch077")),
]


def listed_ids(d, cat):
    """Every channel the answer lists, following the "next page" rows."""
    ids, seen = [], 0
    while True:
        ids += [c["do"]["channel"] for c in d.choices if "do" in c]
        more = [c["ref"] for c in d.choices if "ref" in c]
        if not more:
            return ids
        seen += 1
        assert seen < 50, "paging never ends"
        d = intent.list_page(more[-1], cat)


class ListTests(unittest.TestCase):
    def test_list_table(self):
        self.assertGreaterEqual(len(LIST_CASES), 70)
        for text, cat, expected in LIST_CASES:
            with self.subTest(text=text):
                d = decide(text, cat)
                where = f"{text!r} -> {d}"
                self.assertLessEqual(len(d.reply), 40, where)
                if isinstance(expected, tuple) and expected[0] == "channel":
                    self.assertEqual((d.kind, d.do), ("channel", {"channel": expected[1]}), where)
                elif isinstance(expected, tuple):
                    self.assertEqual((d.kind, d.do, d.choices), ("none", None, None), where)
                    self.assertIn(expected[1], d.reply, where)
                elif expected == GROUPS_ALL:
                    self.assertEqual(d.kind, "list", where)
                    self.assertTrue(all("ref" in c for c in d.choices), where)
                    self.assertIn("个台", d.reply, where)
                else:
                    self.assertIn(d.kind, ("list", "choices"), where)
                    self.assertIsNone(d.do, where)
                    ids = listed_ids(d, cat)
                    self.assertEqual(len(ids), len(set(ids)), where)
                    self.assertEqual(set(ids), expected, where)

    def test_the_failing_transcript(self):
        d = decide("我有哪些youtube频道告诉我。", TODAY)
        self.assertEqual(d.kind, "list")
        self.assertEqual(d.reply, "YouTube 频道有 3 个")
        self.assertEqual([c["do"]["channel"] for c in d.choices], ["up03", "up04", "sr03"])
        self.assertEqual(d.choices[0], {"label": "Dulaa Music", "lead": f"{TODAY.channel('up03').number:02d}",
                                        "do": {"channel": "up03"}})

    def test_replies(self):
        self.assertEqual(decide("我订阅了哪些UP主", TODAY).reply, "UP 主有 9 个")
        self.assertEqual(decide("油管的up主有哪些", TODAY).reply, "YouTube UP 主有 2 个")
        self.assertEqual(decide("B站的UP主有哪些", TODAY).reply, "B站 UP 主有 7 个")
        self.assertEqual(decide("B站有哪些频道", TODAY).reply, "B站频道有 9 个")
        self.assertEqual(decide("有哪些卫视", TODAY).reply, "卫视有 2 个台")
        self.assertEqual(decide("有哪些YouTube的剧", NO_YT_SERIES).reply, "还没有 YouTube 剧集")
        self.assertEqual(decide("都有什么台", TODAY).reply, f"一共 {len(TODAY_CHANNELS)} 个台，分 9 组")

    def test_paging(self):
        d = decide("央视有哪些台", TODAY)
        self.assertEqual(d.reply, "央视有 17 个台")
        self.assertEqual(len(d.choices), intent.CHOICES_MAX)
        self.assertEqual([c["label"] for c in d.choices[:5]], ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV5"])
        self.assertEqual(d.choices[5], {"label": "下一页 · 还有 12 个", "lead": "", "ref": "ls:..2:5"})
        d = intent.list_page("ls:..2:5", TODAY)
        self.assertEqual(d.reply, "央视 第 6-10 个，共 17 个")
        self.assertEqual(d.choices[0]["label"], "CCTV6")
        self.assertEqual(d.choices[5]["ref"], "ls:..2:10")
        d = intent.list_page("ls:..2:10", TODAY)
        self.assertEqual(d.choices[5]["label"], "下一页 · 还有 2 个")
        d = intent.list_page("ls:..2:15", TODAY)
        self.assertEqual([c["label"] for c in d.choices], ["CCTV16", "CCTV17"])
        self.assertEqual(d.reply, "央视 第 16-17 个，共 17 个")
        # Six fit without a "next page" row; seven do not.
        self.assertEqual(len(decide("B站UP主", TODAY).choices), 6)
        self.assertEqual(decide("B站UP主", TODAY).choices[5]["ref"], "ls:bl.up.:5")
        six = decide("B站UP主", Catalog([c for c in TODAY_CHANNELS if c.id != "up09"])).choices
        self.assertEqual(len(six), 6)
        self.assertTrue(all("do" in c for c in six))

    def test_groups_open_their_lists(self):
        d = decide("有什么可以看的", TODAY)
        self.assertEqual(d.choices[0], {"label": "我的投屏 · 1 个台", "lead": "", "ref": "ls:..0:0"})
        self.assertEqual(d.choices[2]["label"], "央视 · 17 个台")
        self.assertEqual(d.choices[5], {"label": "下一页 · 还有 4 个", "lead": "", "ref": "ls:..:5"})
        rest = intent.list_page("ls:..:5", TODAY)
        self.assertEqual([c["label"].split(" · ")[0] for c in rest.choices], ["国际", "音乐", "动画影视", "其他"])
        opened = intent.list_page(d.choices[1]["ref"], TODAY)
        self.assertEqual(opened.reply, "UP 主有 9 个台")
        self.assertEqual(opened.choices[0]["do"], {"channel": "up01"})

    def test_refs_fit_the_device(self):
        """The firmware keeps a ref in 32 bytes (printable, no quote or backslash), a label in 48."""
        for site in ("", "youtube", "bilibili"):
            for kind in ("", "up", "series"):
                for group in ("",) + intent.GROUP_ORDER:
                    ref = intent.list_ref(intent.Category(site, kind, group), 125)
                    self.assertLessEqual(len(ref), 31)
                    self.assertTrue(all(" " <= ch < "\x7f" and ch not in '"\\' for ch in ref), ref)
                    self.assertEqual(intent.parse_list_ref(ref), (intent.Category(site, kind, group), 125))
        for text, cat, _ in LIST_CASES:
            for choice in decide(text, cat).choices or []:
                self.assertLessEqual(len(choice["label"].encode("utf-8")), 47, choice)

    def test_stale_and_bad_refs(self):
        # The list got shorter between the pages: start again.
        d = intent.list_page("ls:..2:40", TODAY)
        self.assertEqual(d.choices[0]["label"], "CCTV1")
        for bad in ("ls:", "ls:xx..:0", "ls:..99:0", "ls:..2:", "ep:sr01:3", "ls:..2:5;"):
            self.assertIsNone(intent.list_page(bad, TODAY), bad)


class NotFoundTests(unittest.TestCase):
    def test_a_named_series_or_channel_that_is_not_there(self):
        for text, name in (("葫芦娃第三集", "葫芦娃"), ("葫芦娃第3集", "葫芦娃"), ("葫芦娃最新一集", "葫芦娃"),
                           ("我要看葫芦娃", "葫芦娃"), ("播放小猪佩奇", "小猪佩奇"), ("打开小猪佩奇", "小猪佩奇"),
                           ("切到葫芦娃", "葫芦娃"), ("看熊大", "熊大")):
            with self.subTest(text=text):
                d = decide(text, LIVE)
                self.assertEqual((d.kind, d.do, d.relay), ("none", None, None))
                self.assertEqual(d.reply, f"没有找到「{name}」")

    def test_plain_nonsense_is_still_not_understood(self):
        for text in ("今天天气怎么样", "那个那个什么来着", "看什么好", "看不了", "什么", "看看", "打开", "来点",
                     "我要看电视"):
            with self.subTest(text=text):
                d = decide(text, LIVE)
                if d.kind == "none":
                    self.assertEqual(d.reply, intent.REPLY_UNKNOWN)


class NumberTests(unittest.TestCase):
    def test_numerals(self):
        for text, value in (("中央五套", "中央5套"), ("第十二集", "第12集"), ("一百", "100"),
                            ("一百五", "150"), ("三百零五", "305"), ("二十", "20"), ("十", "10"),
                            ("两千零一", "2001"), ("一二", "12"), ("第一个台", "第1个台")):
            self.assertEqual(numbers.to_digits(text), value)

    def test_soft_one_is_not_a_number(self):
        for text in ("大声一点", "下一集", "上一个", "换一个台", "等一下", "一些"):
            self.assertEqual(numbers.to_digits(text), text)

    def test_parse_rejects_nonsense(self):
        self.assertIsNone(numbers.parse("十十"))
        self.assertIsNone(numbers.parse("五五十"))


class MatchTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(match.normalize("ＣＣＴＶ－５＋ 体育！"), "cctv5加体育")
        self.assertEqual(match.normalize("<|zh|>湖南卫视。"), "湖南卫视")

    def test_pinyin_hears_homophones_and_polyphones(self):
        self.assertEqual(match.pinyin_ratio("湖南为是", "湖南卫视"), 1.0)
        self.assertEqual(match.pinyin_ratio("重庆", "崇庆"), 1.0)      # zhong/chong
        self.assertEqual(match.pinyin_ratio("小林说", "小lin说"), 1.0)
        self.assertEqual(match.pinyin_ratio("5套", "舞套"), 1.0)
        self.assertGreater(match.pinyin_ratio("四", "十"), 0.4)       # si / shi: half an error
        self.assertLess(match.pinyin_ratio("四", "十"), 1.0)

    def test_pinyin_table_covers_gb2312(self):
        """The device's font and SenseVoice's output are GB2312 hanzi: every one has a reading."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from make_pinyin_table import gb2312_hanzi
        hanzi = gb2312_hanzi()
        self.assertEqual(len(hanzi), 6763)
        missing = [c for c in hanzi if c not in match._READINGS]
        self.assertEqual(missing, [])
        self.assertEqual(set(match._READINGS["行"]), {"xing", "hang", "heng"})

    def test_aliases(self):
        names = dict(intent.aliases("CCTV-5+ 体育赛事"))
        self.assertIn("中央5加", names)
        self.assertIn("5加", names)
        names = dict(intent.aliases("湖南卫视"))
        self.assertIn("湖南", names)


if __name__ == "__main__":
    unittest.main()
