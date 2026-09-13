from __future__ import annotations

from email.message import EmailMessage
import unittest
from urllib.parse import quote

from deal_watcher.feed import (
    ListingCandidate,
    candidates_from_email,
    candidates_from_html,
    candidates_from_json_lines,
)


class FeedCandidateTests(unittest.TestCase):
    def test_json_candidate_preserves_market_semantics(self):
        line = (
            '{"id":1234567890,"title":"Lenovo Legion RTX 4070",'
            '"price":99000,"url":"https://www.avito.ru/moskva/noutbuki/x_1234567890",'
            '"profile":"market-new-4070","mode":"market","source":"fixture"}'
        )
        candidate = next(iter(candidates_from_json_lines([line])))
        self.assertEqual(candidate.avito_id, 1234567890)
        self.assertEqual(candidate.price, 99000)
        self.assertEqual(candidate.profile, "market-new-4070")
        self.assertTrue(candidate.baseline_eligible)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mode"):
            ListingCandidate(
                avito_id=1234567890,
                title="x",
                price=100000,
                url="https://www.avito.ru/x_1234567890",
                mode="stealth",
            )


class MailFeedTests(unittest.TestCase):
    def test_extracts_listing_title_price_and_id_from_html(self):
        html = """
        <html><body>
          <div>
            <a href="https://www.avito.ru/moskva/noutbuki/lenovo_legion_1234567890">
              Lenovo Legion 5 RTX 4070 32GB
            </a>
            <span>99 000 ₽</span>
          </div>
        </body></html>
        """
        candidates = candidates_from_html(html)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.avito_id, 1234567890)
        self.assertEqual(candidate.price, 99000)
        self.assertEqual(candidate.title, "Lenovo Legion 5 RTX 4070 32GB")
        self.assertFalse(candidate.baseline_eligible)

    def test_extracts_percent_encoded_avito_target_from_tracking_url(self):
        target = "https://www.avito.ru/moskva/noutbuki/asus_rog_1987654321"
        tracking = "https://mail.example/click?target=" + quote(target, safe="")
        html = f'<a href="{tracking}">ASUS ROG RTX 5070</a><b>119 990 ₽</b>'
        candidates = candidates_from_html(html, mode="market", profile="mail-market")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].avito_id, 1987654321)
        self.assertEqual(candidates[0].price, 119990)
        self.assertTrue(candidates[0].baseline_eligible)

    def test_email_parser_skips_unreliable_events_without_price(self):
        message = EmailMessage()
        message.set_content("New listing https://www.avito.ru/moskva/noutbuki/x_1234567890")
        self.assertEqual(candidates_from_email(message), [])

    def test_email_html_part_is_parsed(self):
        message = EmailMessage()
        message.set_content("fallback")
        message.add_alternative(
            '<a href="https://www.avito.ru/moskva/noutbuki/x_1234567890">'
            'ThinkBook 16p RTX 4060</a><span>85 000 ₽</span>',
            subtype="html",
        )
        candidates = candidates_from_email(message, profile="fast-any-4060")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].profile, "fast-any-4060")
        self.assertEqual(candidates[0].price, 85000)


if __name__ == "__main__":
    unittest.main()
