"""The channel table the relay writes for the TV server."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import live
from server.relay import table
from server.relay.state import Creator


def creator(cid, name):
    return Creator(id=cid, site="bilibili", source="1", name=name)


class TableTests(unittest.TestCase):
    def test_format_parses(self):
        text = table.render([creator("up01", "老番茄"), creator("up02", "影视飓风")])
        channels, labels, _ = live.parse_channels(text)
        self.assertEqual(list(channels), ["up01", "up02"])
        self.assertEqual(labels["up01"], "老番茄")
        self.assertEqual(channels["up02"], "http://127.0.0.1:8099/ch/up02")

    def test_name_cut_and_cleaned(self):
        text = table.render([creator("up01", "一二三四五六七八九十十一"),
                             creator("up02", "a|b\nc"), creator("up03", "  ")])
        _, labels, _ = live.parse_channels(text)
        self.assertEqual(labels["up01"], "一二三四五六七八九十")
        self.assertEqual(labels["up02"], "a b c")
        self.assertEqual(labels["up03"], "up03")

    def test_empty_table_is_comment_only(self):
        self.assertFalse([l for l in table.render([]).splitlines() if l and not l.startswith("#")])

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
