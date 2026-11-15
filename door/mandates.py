"""Shared demo mandate scopes and category-prefix matching."""

DEMO_ALLOWED_CATEGORIES = (
    "104", "105", "106", "109", "110", "172", "173", "174", "187",
    "211", "212", "213", "214", "215", "216", "219", "221", "223",
    "224", "225", "226", "227", "417", "418", "522", "849", "850",
)

DEMO_MUST_HOLD_CATEGORIES = ("10400", "10401", "10405", "1041", "1050", "106")


def category_matches(category, prefixes) -> bool:
    """Match a complete category code by prefix; a full code matches itself."""
    value = str(category or "")
    return any(str(prefix) and value.startswith(str(prefix)) for prefix in (prefixes or []))


def demo_scope_limits() -> dict:
    """Return fresh mutable values for a newly signed demo mandate."""
    return {
        "allowed_categories": list(DEMO_ALLOWED_CATEGORIES),
        "allowed_categories_label": "Food and drink",
        "must_hold": ["lactose-free"],
        "must_hold_categories": list(DEMO_MUST_HOLD_CATEGORIES),
        "must_hold_label": "Lactose-free for dairy products",
    }
