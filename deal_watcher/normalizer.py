from __future__ import annotations

import re

from .models import Condition, LaptopSpecs, RiskAssessment


_BRANDS = {
    "asus": "ASUS",
    "lenovo": "Lenovo",
    "msi": "MSI",
    "acer": "Acer",
    "hp": "HP",
    "dell": "Dell",
    "gigabyte": "Gigabyte",
    "huawei": "Huawei",
    "honor": "Honor",
}

_FAMILY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bthinkbook\s*16\s*\+", re.I), "ThinkBook 16+"),
    (re.compile(r"\bthinkbook\s*16p\b", re.I), "ThinkBook 16p"),
    (re.compile(r"\blegion\s*(?:pro\s*)?7\b", re.I), "Legion 7"),
    (re.compile(r"\blegion\s*(?:slim\s*)?5\b", re.I), "Legion 5"),
    (re.compile(r"\b(?:rog\s*)?zephyrus\b", re.I), "ROG Zephyrus"),
    (re.compile(r"\byoga\s*pro\b", re.I), "Yoga Pro"),
    (re.compile(r"\bproart\b", re.I), "ProArt"),
    (re.compile(r"\btuf(?:\s+gaming)?(?:\s+[af]\d{2})?\b", re.I), "TUF"),
    (re.compile(r"\bomen\b", re.I), "Omen"),
    (re.compile(r"\bpredator\b", re.I), "Predator"),
    (re.compile(r"\bkatana\b", re.I), "MSI Katana"),
    (re.compile(r"\bcyborg\b", re.I), "MSI Cyborg"),
    (re.compile(r"\bmsi\s+thin\b|\bthin\s+\d", re.I), "MSI Thin"),
]

_GPU_RE = re.compile(
    r"\b(?:geforce\s*)?(?:rtx\s*[- ]?)?(?P<gpu>50[5-9]0|40[5-9]0|30[5-9]0)\b",
    re.I,
)
_CPU_PATTERNS = [
    re.compile(r"\b(?P<cpu>ryzen\s+(?:ai\s+)?[3579](?:\s+hx)?\s*\d{2,4}\w*)\b", re.I),
    re.compile(r"\b(?P<cpu>core\s+ultra\s+[579]\s+\d{3}\w*)\b", re.I),
    re.compile(r"\b(?P<cpu>i[3579]-?\d{4,5}[A-Z]{0,2})\b", re.I),
    re.compile(r"\b(?P<cpu>[3579]\s*\d{3,4}(?:HX|HS|H|U|X3D)?)\b", re.I),
]
_SKU_PATTERNS = [
    re.compile(r"\b(?P<sku>FA\d{3}[A-Z]{1,3})\b", re.I),
    re.compile(r"\b(?P<sku>GU\d{3}[A-Z]{1,3})\b", re.I),
    re.compile(r"\b(?P<sku>G\d{2,3}[A-Z]{1,3})\b", re.I),
    re.compile(r"\b(?P<sku>\d{2}[A-Z]{2,4}\d{2,4}[A-Z0-9]*)\b", re.I),
]

_RAM_RE = re.compile(r"\b(?P<ram>8|16|24|32|48|64|96|128)\s*(?:gb|гб)\b", re.I)
_STORAGE_RE = re.compile(
    r"\b(?P<size>256|512|1024|2048|4096)\s*(?:gb|гб)\b|"
    r"\b(?P<tb>[124])\s*(?:tb|тб)\b",
    re.I,
)
_SLASH_RE = re.compile(
    r"\b(?P<ram>16|24|32|48|64|96|128)\s*/\s*"
    r"(?P<storage>512|1024|2048|[124])(?:\s*(?:tb|тб))?\b",
    re.I,
)

_BROKEN = (
    "не включается",
    "не работает",
    "на запчасти",
    "разбит",
    "кирпич",
    "дефект",
)
_REFURB = (
    "восстановлен",
    "refurb",
    "после ремонта",
    "ремонтирован",
    "замена платы",
)
_USED = (
    "б/у",
    "бу ",
    "бывший в употреблении",
    "пользовался",
    "пользовались",
    "есть следы использования",
)
_NEW_STRONG = (
    "не активирован",
    "неактивирован",
    "запечатан",
    "sealed",
    "не вскрыт",
)
_NEW_WEAK = ("новый", "new", "полный комплект", "гарантия")

_RISK_TERMS: list[tuple[str, int, str]] = [
    ("trade-in", 18, "trade-in"),
    ("trade in", 18, "trade-in"),
    ("трейд-ин", 18, "trade-in"),
    ("цена от", 14, "цена «от»"),
    ("уточняйте цену", 16, "цена требует уточнения"),
    ("уточняйте стоимость", 16, "цена требует уточнения"),
    ("при оформлении кредита", 24, "цена при кредите"),
    ("при покупке в кредит", 24, "цена при кредите"),
    ("первоначальный взнос", 35, "первоначальный взнос вместо цены"),
    ("под заказ", 12, "под заказ"),
    ("предоплата", 18, "предоплата"),
    ("цена указана за", 30, "цена может быть не за ноутбук"),
    ("без видеокарты", 50, "без видеокарты"),
    ("на запчасти", 50, "на запчасти"),
    ("не включается", 50, "не включается"),
    ("после ремонта", 22, "после ремонта"),
    ("восстановлен", 22, "восстановленный"),
    ("витринный", 9, "витринный"),
]


def _text(title: str | None, description: str | None) -> str:
    return " ".join(filter(None, (title, description))).strip()


def extract_specs(title: str | None, description: str | None = None) -> LaptopSpecs:
    raw = _text(title, description)
    lower = raw.lower()

    brand = next((pretty for token, pretty in _BRANDS.items() if token in lower), None)
    family = next(
        (name for pattern, name in _FAMILY_PATTERNS if pattern.search(raw)),
        None,
    )

    gpu = None
    if match := _GPU_RE.search(raw):
        gpu = f"RTX {match.group('gpu')}"

    cpu = None
    for pattern in _CPU_PATTERNS:
        if match := pattern.search(raw):
            value = re.sub(r"\s+", " ", match.group("cpu"))
            cpu = value.upper()
            if value.lower().startswith("i"):
                cpu = "i" + cpu[1:]
            break

    sku = None
    for pattern in _SKU_PATTERNS:
        if match := pattern.search(raw):
            candidate = match.group("sku").upper()
            if any(ch.isalpha() for ch in candidate) and any(ch.isdigit() for ch in candidate):
                sku = candidate
                break

    ram_gb = None
    storage_gb = None
    if slash := _SLASH_RE.search(raw):
        ram_gb = int(slash.group("ram"))
        value = int(slash.group("storage"))
        storage_gb = value * 1024 if value in {1, 2, 4} else value

    if ram_gb is None and (ram := _RAM_RE.search(raw)):
        ram_gb = int(ram.group("ram"))

    if storage_gb is None:
        storage_candidates: list[int] = []
        for match in _STORAGE_RE.finditer(raw):
            if match.group("tb"):
                storage_candidates.append(int(match.group("tb")) * 1024)
            elif match.group("size"):
                size = int(match.group("size"))
                if size != ram_gb:
                    storage_candidates.append(size)
        if storage_candidates:
            storage_gb = max(storage_candidates)

    return LaptopSpecs(
        brand=brand,
        family=family,
        sku=sku,
        cpu=cpu,
        gpu=gpu,
        ram_gb=ram_gb,
        storage_gb=storage_gb,
        condition=detect_condition(raw),
    )


def detect_condition(text: str) -> Condition:
    lower = text.lower()
    if any(term in lower for term in _BROKEN):
        return Condition.BROKEN
    if any(term in lower for term in _REFURB):
        return Condition.REFURBISHED
    if any(term in lower for term in _USED):
        return Condition.USED

    strong = sum(term in lower for term in _NEW_STRONG)
    weak = sum(term in lower for term in _NEW_WEAK)
    if strong >= 1 and weak >= 1:
        return Condition.NEW_CONFIRMED
    if strong >= 1 or weak >= 1:
        return Condition.NEW_LIKELY
    return Condition.UNKNOWN


def assess_risk(title: str | None, description: str | None = None) -> RiskAssessment:
    raw = _text(title, description).lower()
    risk = RiskAssessment()
    for needle, penalty, flag in _RISK_TERMS:
        if needle in raw:
            risk.add(penalty, flag)
    return risk
