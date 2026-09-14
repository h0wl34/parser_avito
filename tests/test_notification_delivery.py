from __future__ import annotations

import unittest

from integrations.notifications.composite import CompositeNotifier, NullNotifier


class GoodNotifier:
    def __init__(self):
        self.calls = 0

    def notify(self, ad=None, message=None):
        self.calls += 1


class BadNotifier:
    def __init__(self):
        self.calls = 0

    def notify(self, ad=None, message=None):
        self.calls += 1
        raise RuntimeError("delivery failed")


class CompositeDeliveryTests(unittest.TestCase):
    def test_any_success_counts_as_delivered(self):
        bad = BadNotifier()
        good = GoodNotifier()
        notifier = CompositeNotifier([bad, good])
        self.assertTrue(notifier.notify(message="x"))
        self.assertEqual(bad.calls, 1)
        self.assertEqual(good.calls, 1)

    def test_all_failures_return_false(self):
        notifier = CompositeNotifier([BadNotifier(), BadNotifier()])
        self.assertFalse(notifier.notify(message="x"))

    def test_null_notifier_is_not_delivery(self):
        self.assertFalse(NullNotifier().notify(message="x"))


if __name__ == "__main__":
    unittest.main()
