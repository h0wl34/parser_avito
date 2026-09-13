from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from .feed import ListingCandidate


_PRICE_DIGITS_RE = re.compile(r"\d+")


def verify_avigram_signature(
    raw_body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    max_skew_seconds: int = 300,
    now_ms: int | None = None,
) -> bool:
    """Verify Avigram's documented HMAC-SHA256 callback signature."""
    if not timestamp or not signature or not secret:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False

    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if abs(current_ms - ts) > int(max_skew_seconds) * 1000:
        return False

    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(signature, expected)


def _price_to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid boolean price")
    if isinstance(value, int):
        price = value
    elif isinstance(value, float):
        price = int(value)
    elif isinstance(value, str):
        digits = "".join(_PRICE_DIGITS_RE.findall(value))
        if not digits:
            raise ValueError("price contains no digits")
        price = int(digits)
    else:
        raise ValueError("unsupported price type")
    if price <= 0:
        raise ValueError("price must be positive")
    return price


def candidate_from_avigram_payload(
    payload: dict[str, Any],
    *,
    market_name_prefixes: tuple[str, ...] = ("market-", "market_", "market "),
) -> ListingCandidate:
    if not isinstance(payload, dict):
        raise ValueError("webhook payload must be an object")

    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    seller = payload.get("seller") if isinstance(payload.get("seller"), dict) else {}
    parameters = payload.get("parameters")
    parameters = parameters if isinstance(parameters, list) else []

    search_name = str(info.get("searchName", "") or "").strip()
    search_id = info.get("searchId")
    profile = search_name or (
        f"avigram-{search_id}" if search_id is not None else "avigram-fast"
    )
    lowered = profile.lower()
    mode = (
        "market"
        if any(lowered.startswith(prefix.lower()) for prefix in market_name_prefixes)
        else "fast"
    )

    description_parts: list[str] = []
    raw_description = str(payload.get("description", "") or "").strip()
    if raw_description:
        description_parts.append(raw_description)
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        title = str(parameter.get("title", "") or "").strip()
        value = str(parameter.get("description", "") or "").strip()
        if title and value:
            description_parts.append(f"{title}: {value}")

    published_at = None
    timestamp_ms = payload.get("timestamp")
    if isinstance(timestamp_ms, (int, float)) and timestamp_ms > 0:
        published_at = datetime.fromtimestamp(
            float(timestamp_ms) / 1000,
            tz=timezone.utc,
        )

    return ListingCandidate(
        avito_id=int(payload["id"]),
        title=str(payload.get("title", "") or "").strip(),
        price=_price_to_int(payload.get("price")),
        url=str(payload.get("link", "") or "").strip(),
        description=" | ".join(description_parts),
        published_at=published_at,
        seller_id=(
            str(seller.get("id")).strip()
            if seller.get("id") is not None
            else None
        ),
        profile=profile,
        mode=mode,
        source="avigram",
    )


def candidate_to_json(candidate: ListingCandidate) -> str:
    raw = asdict(candidate)
    if candidate.published_at is not None:
        raw["published_at"] = candidate.published_at.isoformat()
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def append_candidate_durable(candidate: ListingCandidate, path: str | Path) -> None:
    """Append one normalized event and force it to stable storage before ACK."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = candidate_to_json(candidate) + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
