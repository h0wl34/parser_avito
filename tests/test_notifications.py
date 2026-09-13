import unittest

from integrations.notifications.telegram import TelegramNotifier


class TelegramNotifierProxyTests(unittest.TestCase):
    def test_empty_proxy_returns_none(self):
        self.assertIsNone(TelegramNotifier.get_proxy(None))
        self.assertIsNone(TelegramNotifier.get_proxy(""))
        self.assertIsNone(TelegramNotifier.get_proxy("   "))

    def test_legacy_proxy_without_scheme_stays_http_compatible(self):
        proxies = TelegramNotifier.get_proxy("user:pass@127.0.0.1:8080")
        self.assertEqual(
            proxies,
            {
                "http": "http://user:pass@127.0.0.1:8080",
                "https": "http://user:pass@127.0.0.1:8080",
            },
        )

    def test_socks5_proxy_scheme_is_preserved(self):
        proxies = TelegramNotifier.get_proxy("socks5://user:pass@127.0.0.1:1080")
        self.assertEqual(
            proxies,
            {
                "http": "socks5://user:pass@127.0.0.1:1080",
                "https": "socks5://user:pass@127.0.0.1:1080",
            },
        )

    def test_socks5h_proxy_scheme_is_preserved(self):
        proxies = TelegramNotifier.get_proxy("socks5h://user:pass@127.0.0.1:1080")
        self.assertEqual(
            proxies,
            {
                "http": "socks5h://user:pass@127.0.0.1:1080",
                "https": "socks5h://user:pass@127.0.0.1:1080",
            },
        )

    def test_http_and_https_proxy_schemes_are_preserved(self):
        self.assertEqual(
            TelegramNotifier.get_proxy("http://127.0.0.1:8080")["https"],
            "http://127.0.0.1:8080",
        )
        self.assertEqual(
            TelegramNotifier.get_proxy("https://127.0.0.1:8443")["https"],
            "https://127.0.0.1:8443",
        )

    def test_unsupported_proxy_scheme_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported notifier proxy scheme"):
            TelegramNotifier.get_proxy("ftp://127.0.0.1:21")


if __name__ == "__main__":
    unittest.main()
