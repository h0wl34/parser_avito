from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse


_PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0\u202f]?\d{3})+|\d{4,7})\s*(?:₽|р\.?|руб(?:\.|лей)?)",
    re.IGNORECASE,
)
_AVITO_ID_RE = re.compile(r"_(\d{6,})(?=[/?#]|$)")
_AVITO_HOST_RE = re.compile(r"(^|\.)avito\.ru$", re.IGNORECASE)
_GENERIC_TITLE = {
    "открыть",
    "посмотреть",
    "смотреть",
    "перейти",
    "подробнее",
    "avito",
    "авито",
}


@dataclass(slots=True)
class ListingCandidate:
    """Source-neutral listing event consumed by the deal intelligence layer.

    The producer can be an email notification, an authorized external feed,
    stdin/JSONL, or another integration.  No Avito request is needed here.
    """

    avito_id: int
    title: str
    price: int
    url: str
    description: str = ""
    published_at: datetime | None = None
    seller_id: str | None = None
    profile: str = "feed-fast"
    mode: str = "fast"
    source: str = "feed"

    def __post_init__(self) -> None:
        if self.avito_id <= 0:
            raise ValueError("avito_id must be positive")
        if self.price <= 0:
            raise ValueError("price must be positive")
        self.mode = self.mode.strip().lower()
        if self.mode not in {"fast", "market"}:
            raise ValueError("mode must be 'fast' or 'market'")
        self.title = " ".join(self.title.split()).strip()
        self.description = " ".join(self.description.split()).strip()
        self.profile = self.profile.strip() or f"feed-{self.mode}"
        self.source = self.source.strip() or "feed"

    @property
    def baseline_eligible(self) -> bool:
        return self.mode == "market"

    @classmethod
    def from_mapping(cls, raw: dict) -> "ListingCandidate":
        published_at = raw.get("published_at")
        if isinstance(published_at, str) and published_at.strip():
            value = published_at.strip().replace("Z", "+00:00")
            published_at = datetime.fromisoformat(value)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        elif not isinstance(published_at, datetime):
            published_at = None

        avito_id = raw.get("avito_id", raw.get("id"))
        return cls(
            avito_id=int(avito_id),
            title=str(raw.get("title", "")).strip(),
            price=int(raw["price"]),
            url=str(raw.get("url", "")).strip(),
            description=str(raw.get("description", "") or ""),
            published_at=published_at,
            seller_id=(
                str(raw["seller_id"]).strip()
                if raw.get("seller_id") is not None
                else None
            ),
            profile=str(raw.get("profile", "feed-fast")),
            mode=str(raw.get("mode", "fast")),
            source=str(raw.get("source", "feed")),
        )

    @classmethod
    def from_json(cls, line: str) -> "ListingCandidate":
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("feed JSON must be an object")
        return cls.from_mapping(raw)

    def to_item(self):
        """Create the upstream Item lazily so feed parsing stays dependency-free."""
        from models import Item, PriceDetailed

        timestamp_ms = None
        if self.published_at is not None:
            dt = self.published_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            timestamp_ms = int(dt.timestamp() * 1000)

        return Item(
            id=self.avito_id,
            title=self.title,
            description=self.description,
            urlPath=self.url,
            sortTimeStamp=timestamp_ms,
            sellerId=self.seller_id,
            priceDetailed=PriceDetailed(
                enabled=True,
                fullString=f"{self.price} ₽",
                hasValue=True,
                postfix="",
                string=f"{self.price} ₽",
                stringWithoutDiscount=None,
                title={},
                titleDative="",
                value=self.price,
                wasLowered=False,
                exponent="0",
            ),
        )


@dataclass(slots=True)
class _Anchor:
    href: str
    text: str
    start_token: int
    end_token: int


class _MailHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self.anchors: list[_Anchor] = []
        self._href: str | None = None
        self._anchor_parts: list[str] = []
        self._anchor_start = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if href:
            self._href = str(href)
            self._anchor_parts = []
            self._anchor_start = len(self.tokens)

    def handle_data(self, data: str) -> None:
        text = " ".join(unescape(data).split()).strip()
        if not text:
            return
        self.tokens.append(text)
        if self._href is not None:
            self._anchor_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        self.anchors.append(
            _Anchor(
                href=self._href,
                text=" ".join(self._anchor_parts).strip(),
                start_token=self._anchor_start,
                end_token=len(self.tokens),
            )
        )
        self._href = None
        self._anchor_parts = []


def _embedded_avito_url(value: str) -> str | None:
    candidate = unescape(value or "").strip()
    if not candidate:
        return None

    # Tracking links often contain a percent-encoded destination URL. Decode a
    # few layers, but never make a network request to resolve redirects.
    variants = [candidate]
    for _ in range(3):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(decoded)

    for variant in variants:
        parsed = urlparse(variant)
        if parsed.hostname and _AVITO_HOST_RE.search(parsed.hostname):
            return variant

        for values in parse_qs(parsed.query).values():
            for nested in values:
                nested_url = _embedded_avito_url(nested)
                if nested_url:
                    return nested_url

        match = re.search(r"https?://(?:www\.)?avito\.ru/[^\s\"'<>]+", variant)
        if match:
            return match.group(0)
    return None


def _listing_id(url: str) -> int | None:
    parsed = urlparse(url)
    if not parsed.hostname or not _AVITO_HOST_RE.search(parsed.hostname):
        return None
    match = _AVITO_ID_RE.search(parsed.path)
    return int(match.group(1)) if match else None


def _extract_price(text: str) -> int | None:
    match = _PRICE_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _clean_title(anchor_text: str, context: str, price: int) -> str:
    title = " ".join(anchor_text.split()).strip(" -–—|•")
    if title.lower() not in _GENERIC_TITLE and len(title) >= 3 and not _PRICE_RE.fullmatch(title):
        return title[:300]

    price_text = re.sub(r"\s+", " ", context)
    price_text = _PRICE_RE.sub(" ", price_text)
    chunks = [c.strip(" -–—|•") for c in re.split(r"[\n|•]", price_text)]
    chunks = [c for c in chunks if len(c) >= 3 and c.lower() not in _GENERIC_TITLE]
    if chunks:
        return max(chunks, key=len)[:300]
    return f"Avito listing {price} ₽"


def candidates_from_html(
    html: str,
    *,
    profile: str = "imap-fast",
    mode: str = "fast",
    source: str = "imap",
) -> list[ListingCandidate]:
    parser = _MailHtmlParser()
    parser.feed(html or "")

    result: list[ListingCandidate] = []
    seen: set[int] = set()
    for anchor in parser.anchors:
        url = _embedded_avito_url(anchor.href)
        if not url:
            continue
        avito_id = _listing_id(url)
        if not avito_id or avito_id in seen:
            continue

        left = max(0, anchor.start_token - 8)
        right = min(len(parser.tokens), anchor.end_token + 12)
        context = " | ".join(parser.tokens[left:right])
        price = _extract_price(" ".join((anchor.text, context)))
        if not price:
            continue

        title = _clean_title(anchor.text, context, price)
        result.append(
            ListingCandidate(
                avito_id=avito_id,
                title=title,
                price=price,
                url=url,
                profile=profile,
                mode=mode,
                source=source,
            )
        )
        seen.add(avito_id)
    return result


def candidates_from_email(
    message: Message,
    *,
    profile: str = "imap-fast",
    mode: str = "fast",
    source: str = "imap",
) -> list[ListingCandidate]:
    html_parts: list[str] = []
    text_parts: list[str] = []

    for part in message.walk() if message.is_multipart() else (message,):
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/html", "text/plain"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload()
            decoded = raw if isinstance(raw, str) else ""
        else:
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
        if content_type == "text/html":
            html_parts.append(decoded)
        else:
            text_parts.append(decoded)

    candidates: list[ListingCandidate] = []
    for html in html_parts:
        candidates.extend(
            candidates_from_html(html, profile=profile, mode=mode, source=source)
        )

    # Plain text fallback: URLs and nearby price/title text. This is deliberately
    # conservative; events without a reliable listing id and price are skipped.
    if not candidates:
        for text in text_parts:
            for match in re.finditer(r"https?://(?:www\.)?avito\.ru/\S+", text):
                url = match.group(0).rstrip(").,;>\"]'")
                avito_id = _listing_id(url)
                if not avito_id:
                    continue
                context = text[max(0, match.start() - 300): match.end() + 300]
                price = _extract_price(context)
                if not price:
                    continue
                lines = [" ".join(line.split()) for line in context.splitlines()]
                lines = [line for line in lines if line and not _PRICE_RE.search(line) and "http" not in line]
                title = max(lines, key=len)[:300] if lines else f"Avito listing {price} ₽"
                candidates.append(
                    ListingCandidate(
                        avito_id=avito_id,
                        title=title,
                        price=price,
                        url=url,
                        profile=profile,
                        mode=mode,
                        source=source,
                    )
                )
    deduped: dict[int, ListingCandidate] = {}
    for candidate in candidates:
        deduped.setdefault(candidate.avito_id, candidate)
    return list(deduped.values())


def candidates_from_json_lines(lines: Iterable[str]) -> Iterable[ListingCandidate]:
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        yield ListingCandidate.from_json(value)
