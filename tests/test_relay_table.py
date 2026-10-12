"""The channel table the relay writes for the TV server."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import channel_groups, live
from server.relay import table
from server.relay.state import Creator


def creator(cid, name, site="bilibili"):
    return Creator(id=cid, site=site, source="1", name=name)


class TableTests(unittest.TestCase):
    def test_format_parses(self):
        text = table.render([creator("up01", "老番茄"), creator("up02", "影视飓风")])
        channels, labels, _ = live.parse_channels(text)
        self.assertEqual(list(channels), ["cast", "up01", "up02"])
        self.assertEqual(labels["cast"], "我的投屏")
        self.assertEqual(channels["cast"], "http://127.0.0.1:8099/ch/cast")
        self.assertEqual(labels["up01"], "老番茄(B站)")
        self.assertEqual(channels["up02"], "http://127.0.0.1:8099/ch/up02")

    def test_name_cut_and_cleaned(self):
        text = table.render([creator("up01", "一二三四五六七八九十十一"),
                             creator("up02", "a|b\nc"), creator("up03", "  ")])
        _, labels, _ = live.parse_channels(text)
        # The site mark stays; the name gives way to it.
        self.assertEqual(labels["up01"], "一二三四五六七(B站)")
        self.assertEqual(labels["up02"], "a b c(B站)")
        self.assertEqual(labels["up03"], "up03(B站)")

    def test_latin_names_get_twice_the_letters(self):
        """A Chinese character takes two columns on the panel and six bytes in CONFIG."""
        text = table.render([creator("up01", "Dulaa Music", "youtube"),
                             creator("up02", "Linus Tech Tips Official Channel", "youtube"),
                             creator("up03", "老高与小茉 Mr & Mrs Gao")])
        _, labels, _ = live.parse_channels(text)
        self.assertEqual(labels["up01"], "Dulaa Music(油管)")
        self.assertEqual(labels["up02"], "Linus Tech Tip(油管)")
        self.assertEqual(labels["up03"], "老高与小茉 Mr &(B站)")
        for label in labels.values():
            self.assertLessEqual(table.columns(label), table.NAME_COLUMNS, label)

    def test_site_marks(self):
        """Creators (up..) and series (sr..) carry their site in the device's list; the cast slot
        and live channels do not. The mark comes off again for matching by name."""
        text = table.render([creator("up01", "王志安", "youtube"), creator("sr01", "熊出没"),
                             creator("sr02", "柯南OVA", "youtube")])
        _, labels, _ = live.parse_channels(text)
        self.assertEqual(labels, {"cast": "我的投屏", "up01": "王志安(油管)", "sr01": "熊出没(B站)",
                                  "sr02": "柯南OVA(油管)"})
        for cid, label in labels.items():
            bare, site = channel_groups.unmark(cid, label)
            self.assertNotIn("(", bare)
        self.assertEqual(channel_groups.unmark("up01", "王志安(油管)"), ("王志安", "youtube"))
        self.assertEqual(channel_groups.unmark("sr01", "熊出没(B站)"), ("熊出没", "bilibili"))
        self.assertEqual(channel_groups.unmark("up01", "王志安"), ("王志安", ""), "an older relay")
        self.assertEqual(channel_groups.unmark("ch070", "薛之谦精选30首(2小时)"), ("薛之谦精选30首(2小时)", ""))
        self.assertEqual(channel_groups.unmark("ch001", "某台(B站)"), ("某台(B站)", ""), "live channels keep theirs")
        self.assertEqual(channel_groups.unmark("up01", "(油管)"), ("(油管)", ""), "a mark alone is a name")
        # A plain label for the phone, which shows its own site tag.
        self.assertEqual(table.plain_label(creator("up01", "王志安", "youtube")), "王志安")
        # What a mark costs in the CONFIG packet (JSON escapes every non-ASCII character to 6 bytes).
        for site, cost in (("bilibili", 9), ("youtube", 14)):
            self.assertEqual(channel_groups.config_bytes([("up01", "王志安" + channel_groups.mark(site))])
                             - channel_groups.config_bytes([("up01", "王志安")]), cost)
        # Classification is by id: the mark changes no group.
        self.assertEqual(channel_groups.classify("sr01", "熊出没(B站)"), channel_groups.SERIES)
        self.assertEqual(channel_groups.classify("up01", "Dulaa Music(油管)"), channel_groups.CREATORS)

    def test_cast_is_always_listed(self):
        """The device can only be switched to an id it already has in its list."""
        lines = [l for l in table.render([]).splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines, ["cast | 我的投屏 | http://127.0.0.1:8099/ch/cast"])

    def test_write_if_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-channels.txt"
            self.assertTrue(table.write_if_changed(path, "a\n"))
            os.utime(path, ns=(1, 1))
            self.assertFalse(table.write_if_changed(path, "a\n"))
            self.assertEqual(path.stat().st_mtime_ns, 1)
            self.assertTrue(table.write_if_changed(path, "b\n"))
            self.assertEqual(path.read_text(), "b\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
