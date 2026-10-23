"""本机地址不走代理（server/proxyenv.py）：ffmpeg 只认小写 no_proxy。"""

import unittest

from server.proxyenv import loopback_bypass


class LoopbackBypassTests(unittest.TestCase):
    def test_no_proxy_untouched(self):
        env = {"PATH": "/bin", "NO_PROXY": "example.org"}
        self.assertEqual(loopback_bypass(env), env)

    def test_uppercase_no_proxy_copied_to_lowercase_with_loopback(self):
        env = {"http_proxy": "http://127.0.0.1:7897", "NO_PROXY": "localhost,127.0.0.1,::1,.local"}
        result = loopback_bypass(env)
        self.assertEqual(result["no_proxy"], "localhost,127.0.0.1,::1,.local")
        self.assertEqual(result["NO_PROXY"], result["no_proxy"])
        self.assertEqual(result["http_proxy"], env["http_proxy"])

    def test_loopback_added_when_missing(self):
        result = loopback_bypass({"HTTPS_PROXY": "http://proxy:3128", "no_proxy": "corp.lan"})
        self.assertEqual(result["no_proxy"].split(","), ["corp.lan", "localhost", "127.0.0.1", "::1"])

    def test_input_not_modified(self):
        env = {"all_proxy": "socks5://127.0.0.1:7897"}
        loopback_bypass(env)
        self.assertNotIn("no_proxy", env)


if __name__ == "__main__":
    unittest.main()
