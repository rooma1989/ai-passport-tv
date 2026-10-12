"""Channel groups: the automatic classification and the phone's overrides.

The expectations below are the live table on the server on 2026-10-10, so a
change to the rules shows here as a moved channel rather than as a surprise on
the device.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import channel_groups as cg

EXPECTED = {
    "央视": [f"CCTV{n}" for n in range(1, 18)],
    "卫视": ["湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "东方卫视4K", "浙江卫视4K",
             "江苏卫视4K", "河北4K", "苏州4K", "南国都市4K"],
    "港澳台": ["香港卫视", "凤凰中文", "凤凰资讯", "凤凰香港", "台视新闻", "三立戏剧", "耀才财经",
               "TVBS新闻", "东森新闻", "华视新闻", "民视新闻", "三立新闻", "中视新闻", "中天新闻"],
    "国际": ["CGTN英语", "CGTN记录", "CNA", "ABC News Live", "WionNews", "Arirang", "Trace Sports",
             "RT News（英语国际）", "RT Documentary（英语纪录）", "Newsmax", "Wild Earth",
             "NHK World-Japan", "TRT World", "Al Jazeera English (1080p)", "ABC News (720p)",
             "Africa 24 English (1080p)", "BBC News (1080p)", "BBC News North America (1080p)",
             "France 24 English (1080p)"],
    "音乐": ["风尚音乐", "薛之谦精选30首(2小时)", "五月天精选37首(2小时)", "邓紫棋精选30首(2小时)",
             "周深精选72首(5小时)", "张杰精选40首(2小时)", "Beyond精选40首(3小时)",
             "周传雄精选84首(6小时)", "王菲精选145首(10小时)", "张惠妹精选50首(2小时)"],
    "动画影视": ["猫和老鼠", "哆啦A梦", "海绵宝宝", "中华小当家", "七龙珠", "让子弹飞", "周星驰电影",
                 "止戈电影"],
    "其他": ["环球电视", "熊猫直播", "新纪实", "阿良说美剧", "大象放映室", "陆家嘴", "老高与小茉",
             "自说自话的总裁"],
}


class ClassifyTests(unittest.TestCase):
    def test_live_table(self):
        for group, names in EXPECTED.items():
            for name in names:
                self.assertEqual(cg.classify("ch000", name), group, name)

    def test_relay_channels(self):
        self.assertEqual(cg.classify("cast", "我的投屏"), "我的投屏")
        self.assertEqual(cg.classify("up03", "老高与小茉新视频"), "UP 主")
        self.assertEqual(cg.classify("sr01", "海尔兄弟"), "动画影视")
        # A series channel can be moved like a live one.
        self.assertEqual(cg.group_of("sr01", "海尔兄弟", {"sr01": "其他"}), "其他")

    def test_config_bytes_counts_escaped_names(self):
        one = cg.config_bytes([("sr01", "AB")])
        self.assertEqual(cg.config_bytes([("sr01", "AB"), ("sr02", "海尔")]) - one,
                         len('{"id":"sr02","name":"\\u6d77\\u5c14","g":0},'))

    def test_group_order(self):
        self.assertEqual(cg.GROUPS[:2], ("我的投屏", "UP 主"))
        self.assertEqual(cg.GROUPS[-1], "其他")
        self.assertEqual(set(EXPECTED) | {"我的投屏", "UP 主"}, set(cg.GROUPS))


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "channel-groups.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_override_wins_and_unknown_groups_dropped(self):
        self.path.write_text(json.dumps({"ch064": "音乐", "ch001": "不存在的组", "ch002": 3}),
                             encoding="utf-8")
        overrides = cg.load_overrides(self.path)
        self.assertEqual(overrides, {"ch064": "音乐"})
        self.assertEqual(cg.group_of("ch064", "老高与小茉", overrides), "音乐")
        self.assertEqual(cg.group_of("ch001", "CCTV2", overrides), "央视")

    def test_missing_or_broken_file(self):
        self.assertEqual(cg.load_overrides(self.path), {})
        self.path.write_text("{oops", encoding="utf-8")
        self.assertEqual(cg.load_overrides(self.path), {})
        self.assertEqual(cg.load_overrides(None), {})

    def test_save_round_trip(self):
        cg.save_overrides(self.path, {"ch064": "音乐"})
        self.assertEqual(cg.load_overrides(self.path), {"ch064": "音乐"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_relay_ids_cannot_be_moved(self):
        """Casting and creator channels always sit in their own groups."""
        self.assertEqual(cg.group_of("cast", "我的投屏", {"cast": "音乐"}), "我的投屏")
        self.assertEqual(cg.group_of("up01", "x", {"up01": "音乐"}), "UP 主")


class TableTests(unittest.TestCase):
    def test_read_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.txt"
            path.write_text("# comment\n\nch000 | CCTV1 | https://x/1.m3u8\n"
                            "ch001 | 湖南卫视 | https://x/2.m3u8 | UA\nbroken line\n", encoding="utf-8")
            self.assertEqual(cg.read_table(path), [("ch000", "CCTV1"), ("ch001", "湖南卫视")])
            self.assertEqual(cg.read_table(Path(tmp) / "missing.txt"), [])


class NowPlayingTests(unittest.TestCase):
    def test_round_trip_and_staleness(self):
        from server import now_playing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "now-playing.json"
            self.assertEqual(now_playing.read(path, 100.0),
                             {"channel": "", "online": False, "switch": False, "since": 0.0, "device": None})
            now_playing.write(path, "ch012", True, True, now=100.0, since=95.0)
            self.assertEqual(now_playing.read(path, 120.0),
                             {"channel": "ch012", "online": True, "switch": True, "since": 95.0, "device": None})
            self.assertFalse(now_playing.read(path, 131.0)["online"])
            now_playing.write(path, "ch012", False, True, now=140.0, since=95.0)
            # Between two sessions -- a channel change, a reconnect -- the device
            # is still there; a switch asked for then is applied when it is back.
            self.assertTrue(now_playing.read(path, 150.0)["online"])
            self.assertFalse(now_playing.read(path, 156.0)["online"])
            self.assertIsNone(now_playing.read(path, 150.0)["device"])
            now_playing.write(path, "ch012", True, True, now=160.0, since=95.0,
                              device={"volume": 70, "brightness": 2, "steps": 5})
            self.assertEqual(now_playing.read(path, 161.0)["device"],
                             {"volume": 70, "brightness": 2, "steps": 5})


class ConfigTests(unittest.TestCase):
    """What the device receives: group names once, one index per channel."""

    def setUp(self):
        import os
        from server import live
        self.live = live
        self.saved = (dict(live._LOADED), dict(live.CHANNELS), dict(live.CHANNEL_LABELS),
                      dict(live.CHANNEL_AGENTS), live.DEFAULT_CHANNEL)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.groups = root / "channel-groups.json"
        self.old_env = os.environ.get("TV_CHANNEL_GROUPS_FILE")
        os.environ["TV_CHANNEL_GROUPS_FILE"] = str(self.groups)
        main = root / "channels.txt"
        names = [n for group in EXPECTED.values() for n in group]
        main.write_text("".join(f"ch{i:03d} | {n} | https://example.com/{i}.m3u8\n"
                                for i, n in enumerate(names)), encoding="utf-8")
        relay = root / "relay-channels.txt"
        relay.write_text("cast | 我的投屏 | http://127.0.0.1:8099/ch/cast\n" +
                         "".join(f"up{i:02d} | 一二三四五六七八九十 | http://127.0.0.1:8099/ch/up{i:02d}\n"
                                 for i in range(1, 13)), encoding="utf-8")
        live.load_channels(main, relay)

    def tearDown(self):
        import os
        live = self.live
        loaded, channels, labels, agents, default = self.saved
        live._LOADED.clear(); live._LOADED.update(loaded)
        for table, saved in ((live.CHANNELS, channels), (live.CHANNEL_LABELS, labels),
                             (live.CHANNEL_AGENTS, agents)):
            table.clear(); table.update(saved)
        live.DEFAULT_CHANNEL = default
        if self.old_env is None:
            os.environ.pop("TV_CHANNEL_GROUPS_FILE", None)
        else:
            os.environ["TV_CHANNEL_GROUPS_FILE"] = self.old_env
        self.tmp.cleanup()

    def test_groups_and_indexes(self):
        config = self.live.channel_config()
        self.assertEqual(config["groups"], list(cg.GROUPS))
        by_id = {c["id"]: c for c in config["channel_list"]}
        self.assertEqual(config["groups"][by_id["cast"]["g"]], "我的投屏")
        self.assertEqual(config["groups"][by_id["up05"]["g"]], "UP 主")
        self.assertEqual(config["groups"][by_id["ch000"]["g"]], "央视")

    def test_override_and_empty_groups_hidden(self):
        cg.save_overrides(self.groups, {"ch000": "其他"})
        config = self.live.channel_config()
        by_id = {c["id"]: c for c in config["channel_list"]}
        self.assertEqual(config["groups"][by_id["ch000"]["g"]], "其他")

    def test_full_list_fits_the_device(self):
        import json
        config = self.live.channel_config()
        size = len(json.dumps(config, ensure_ascii=True, separators=(",", ":")))
        self.assertLess(size + 400, self.live.TV_CONTROL_MAX)


class RemoteUrlTests(unittest.TestCase):
    def test_limit_agrees_with_the_firmware(self):
        import re
        from server import tv_server
        header = (Path(__file__).resolve().parents[1] / "main" / "av_protocol.h").read_text()
        limit = int(re.search(r"#define AV_REMOTE_URL_MAX (\d+)u", header).group(1))
        self.assertEqual(limit, tv_server.REMOTE_URL_MAX)

    def test_relay_address_fits(self):
        import re
        from server import tv_server
        from server.relay import state as relay_state
        url = "HTTPS://IEWWW.COM/TV/R/" + "A" * relay_state.CODE_LENGTH
        self.assertLessEqual(len(url), tv_server.REMOTE_URL_MAX)
        self.assertTrue(tv_server._REMOTE_URL.match(url))


if __name__ == "__main__":
    unittest.main()
