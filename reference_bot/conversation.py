"""Deterministic conversation memory and buyer-authority checks.

Model output is only a proposal. These helpers inspect buyer turns, never
assistant-authored quantities, before the bot can draft or place an order.
"""
from __future__ import annotations

from datetime import timedelta
import math
import re

from reference_bot.providers import local_today


ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "ensimmäinen": 1, "ensimmainen": 1, "toinen": 2, "kolmas": 3,
    "neljäs": 4, "neljas": 4, "viides": 5, "kuudes": 6,
}
BUYING_INTENT = re.compile(
    r"\b(?:order|buy|purchase|need|want|get|take|deliver|send|i'll take|give me|i'd like|i would like|we'll have|anna|laita|"
    r"same as last week|usual (?:order|basket)|tilaa|tilaan|tilata|tarvitsen|"
    r"tarvitsen|osta|oston|haluan|toimita|sama kuin viime viikolla)\b",
    re.I,
)
NEGATIVE = re.compile(r"^(?:no|no thanks|cancel|don't|do not|stop|ei|ei kiitos|älä|ala|peruuta)\b[.! ]*", re.I)
AFFIRMATIVE = re.compile(
    r"^(?:yes(?:,\s*place it)?|yes please|place it|place the order|confirm|go ahead|ok(?:ay)?|kyllä|joo|vahvista|tilaa se)[.! ]*$",
    re.I,
)
PRODUCT_WORDS = re.compile(
    r"milk|maito|maitoa|maiton|maitoa|mjölk|cheese|juusto|butter|\bvoi\b|"
    r"oat drink|oat|kaura|juoma|rice|riisi|flour|jauho|pasta|cream|kerma|"
    r"sugar|sokeri|egg|kananmuna|coffee|kahvi|oil|öljy|oljy|chicken|kana|tomato|tomaatti",
    re.I,
)


def buyer_texts(message: str, history: list[dict]) -> list[str]:
    """Only current and earlier user turns can authorize purchasing details."""
    return [turn.get("text", "") for turn in history if turn.get("role") == "user" and isinstance(turn.get("text"), str)] + [message]


# Mirror door.catalog.normalize_property here: the portable bot deliberately
# imports no door internals. These are claim identifiers, not inferred claims.
_PROPERTY_ALIASES = {
    "FREE_FROM_LACTOSE": "lactose-free",
    "FREE_FROM_GLUTEN": "gluten-free",
    "ML": "lactose-free",
    "AW": "gluten-free",
    "LACTOSE": "lactose-free",
    "GLUTEN": "gluten-free",
}
_PROPERTY_PATTERNS = {
    "lactose-free": re.compile(r"(?<![\w])(?:lactose[ _-]*free|free[ _-]*from[ _-]*lactose|laktoosit\w*)(?![\w])", re.I),
    "gluten-free": re.compile(r"(?<![\w])(?:gluten[ _-]*free|free[ _-]*from[ _-]*gluten|gluteeni\w*|gluteenit\w*)(?![\w])", re.I),
    "organic": re.compile(r"(?<![\w])(?:organic|luomu\w*)(?![\w])", re.I),
}


def property_is_stated(value: str, texts: list[str]) -> bool:
    """Recognize only a property the buyer wrote, in either supported language."""
    raw = str(value)
    prop = _PROPERTY_ALIASES.get(raw.upper(), raw.lower().replace("_", "-").replace(" ", "-"))
    pattern = _PROPERTY_PATTERNS.get(prop)
    if pattern is None:
        words = [re.escape(word) for word in prop.split("-") if word]
        if not words:
            return False
        pattern = re.compile(r"(?<![\w])" + r"[ _-]+".join(words) + r"(?![\w])", re.I)
    return any(pattern.search(text) for text in texts)


def stated_properties(properties: list[str], texts: list[str]) -> list[str]:
    """Drop model-invented requirements before they reach matching or display."""
    kept = []
    for value in properties:
        if property_is_stated(value, texts):
            prop = _PROPERTY_ALIASES.get(str(value).upper(), str(value).lower().replace("_", "-").replace(" ", "-"))
            if prop not in kept:
                kept.append(prop)
    return kept


def is_affirmative(message: str) -> bool:
    return bool(AFFIRMATIVE.fullmatch(message.strip()))


def is_negative(message: str) -> bool:
    return bool(NEGATIVE.match(message.strip()))


def has_buying_intent(texts: list[str]) -> bool:
    joined = "\n".join(texts)
    # A bare "2 litres of cream by Monday" is an instruction too, unless it is a question.
    bare_quantity = any(QUANTITY_WITH_UNIT.search(text) and not is_question(text) for text in texts)
    return bool(BUYING_INTENT.search(joined) or re.search(r"\bsome\s+\w+(?:\s+\w+)?\s+please\b", joined, re.I) or bare_quantity)


def latest_draft(history: list[dict]) -> dict | None:
    for turn in reversed(history):
        if turn.get("role") == "assistant" and turn.get("action") == "draft" and isinstance(turn.get("draft"), dict):
            return turn["draft"]
    return None


def latest_products(history: list[dict]) -> list[dict]:
    for turn in reversed(history):
        if turn.get("role") == "assistant" and turn.get("action") == "products" and isinstance(turn.get("products"), list):
            return [item for item in turn["products"] if isinstance(item, dict)]
    return []


def selected_product_id(message: str, history: list[dict]) -> str | None:
    products = latest_products(history)
    if not products:
        return None
    folded = message.casefold()
    position = None
    match = re.search(r"\b(?:the\s+)?(first|second|third|fourth|fifth|sixth|ensimmäinen|ensimmainen|toinen|kolmas|neljäs|neljas|viides|kuudes)(?:\s+one)?\b", folded)
    if match:
        position = ORDINALS[match.group(1)]
    else:
        match = re.search(r"\b(?:number|#|numero)\s*([1-6])\b", folded)
        if match:
            position = int(match.group(1))
    if position is None or position > len(products):
        return None
    product_id = products[position - 1].get("product_id") or products[position - 1].get("id")
    return str(product_id) if product_id else None


def _number(raw: str) -> float | None:
    raw = re.sub(r"\s", "", raw)
    try:
        if "," in raw and "." in raw:
            if raw.rfind(",") > raw.rfind("."):
                raw = raw.replace(".", "").replace(",", ".")
            else:
                raw = raw.replace(",", "")
        elif "," in raw:
            raw = raw.replace(",", "") if re.search(r",\d{3}$", raw) else raw.replace(",", ".")
        value = float(raw)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, OverflowError):
        return None


def quantity_is_stated(quantity: float, unit: str, texts: list[str]) -> bool:
    units = {
        "L": r"(?:litres?|liters?|litraa|litra|l)\b",
        "KG": r"(?:kg|kilograms?|kilogrammes?|kiloa|kilo)\b",
        "PACK": r"(?:sales\s+packs?|packs?|pakkausta|pakkaus|pkt)\b",
    }
    pattern = re.compile(rf"(?<![\d.,])(?P<number>\d+(?:[.,\s]\d+)*)(?=\s*{units.get(unit, units['PACK'])})", re.I)
    for text in texts:
        for match in pattern.finditer(text):
            stated = _number(match.group("number"))
            if stated is not None and math.isclose(float(quantity), stated, rel_tol=0, abs_tol=1e-9):
                return True
    if unit == "PACK":
        # "3 mops" or "12 of the first one": a count of items with no unit word is a count of packs.
        counted = re.compile(r"(?<![\d.,€$])(?P<number>\d+(?:[.,]\d+)?)(?!\s*(?:%|€|eur|litres?|liters?|litraa|l\b|kg|kilo|g\b|ml|dl|cl|st\b|nd\b|rd\b|th\b|[.,:/-]\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|guests?|people|persons?|days?|weeks?|henkil|vieras|päiv))\s+[a-zäöå]", re.I)
        for text in texts:
            for match in counted.finditer(text):
                stated = _number(match.group("number"))
                if stated is not None and math.isclose(float(quantity), stated, rel_tol=0, abs_tol=1e-9):
                    return True
    return False


def stated_deadlines(texts: list[str]) -> set[str]:
    today = local_today()
    dates = set()
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    finnish = ("maanantai", "tiistai", "keskiviik", "torstai", "perjantai", "lauantai", "sunnuntai")
    for text in texts:
        dates.update(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
        folded = text.casefold()
        for index, forms in enumerate(zip(weekdays, finnish)):
            if any(form in folded for form in forms):
                dates.add((today + timedelta(days=(index - today.weekday()) % 7 or 7)).isoformat())
        if re.search(r"\b(?:today|tänään|tanaan)\b", folded):
            dates.add(today.isoformat())
        if re.search(r"\b(?:tomorrow|huomenna)\b", folded):
            dates.add((today + timedelta(days=1)).isoformat())
    return dates


def deadline_is_stated(deadline: str, texts: list[str]) -> bool:
    dates = stated_deadlines(texts)
    return deadline in dates if dates else False


def product_is_stated(need: dict, texts: list[str], history: list[dict]) -> bool:
    joined = "\n".join(texts)
    if product_is_selected(need, history) or PRODUCT_WORDS.search(joined):
        return True
    words = [word for word in re.findall(r"[a-zA-ZäöåÄÖÅ]+", str(need.get("product_type", ""))) if word not in {"drinking", "plain", "other", "products", "usual", "basket"}]
    # Singular and plural count as the same word: "mops" states the product type "mop".
    stem = lambda word: word.casefold()[:-1] if len(word) > 3 and word.casefold().endswith("s") else word.casefold()
    spoken = {stem(word) for word in re.findall(r"[a-zA-ZäöåÄÖÅ]+", joined)}
    return any(stem(word) in spoken for word in words)


def product_is_selected(need: dict, history: list[dict]) -> bool:
    product_id = str(need.get("product_id") or "")
    return bool(product_id and any(str(item.get("product_id") or item.get("id") or "") == product_id for item in latest_products(history)))


QUANTITY_WITH_UNIT = re.compile(r"\d[\d.,\u00a0 ]*\s*(?:l|litres?|liters?|litraa|kg|kilos?|kilograms?|kiloa|packs?|sales packs?|pcs|kpl)\b", re.I)
USUAL_BASKET = re.compile(r"\b(?:same as last week|the usual|usual order|usual basket|sama kuin viime viikolla|tavallinen tilaus)\b", re.I)


def is_complete_instruction(message: str) -> bool:
    """True when the message alone states buying intent, a quantity and a delivery date, or asks for the usual basket."""
    if is_question(message):
        return False
    if USUAL_BASKET.search(message):
        return True
    if len(QUANTITY_WITH_UNIT.findall(message)) > 1:
        return False  # several products in one message: the bot asks which one to start with
    return has_buying_intent([message]) and bool(QUANTITY_WITH_UNIT.search(message)) and bool(stated_deadlines([message]))


QUESTION = re.compile(r"^\s*(?:what|which|who|when|where|why|how|is|are|do|does|did|can|could|would|should|will|mitä|mikä|mitkä|milloin|onko|paljonko|kuinka|voinko|voitko|saanko)\b", re.I)
UNDO = re.compile(r"\b(?:undo|cancel|revert|take (?:it|that) back|peruuta|peru|kumoa)\b", re.I)
LIMITS_QUESTION = re.compile(r"\b(?:limits?|spend(?:ing)? cap|how much can i spend|budget|mandate|rajat|rajani|budjetti|valtuutus)\b", re.I)
CHEAPEST = re.compile(r"\b(?:cheapest|least expensive|lowest price|halvin|edullisin)\b", re.I)


def is_question(message: str) -> bool:
    return message.strip().endswith("?") or bool(QUESTION.match(message))


def is_undo_request(message: str) -> bool:
    return bool(UNDO.search(message)) and not QUANTITY_WITH_UNIT.search(message)


def asks_about_limits(message: str) -> bool:
    """A question about the buyer's own limits, not an instruction to change them or to order."""
    return bool(LIMITS_QUESTION.search(message)) and is_question(message) and not QUANTITY_WITH_UNIT.search(message)


def asks_about_usual(message: str) -> bool:
    return is_question(message) and bool(USUAL_BASKET.search(message) or re.search(r"\b(?:usual|regular|last week'?s?)\b.*\b(?:order|basket)\b", message, re.I))


def asks_for_cheapest(message: str) -> bool:
    return bool(CHEAPEST.search(message))
