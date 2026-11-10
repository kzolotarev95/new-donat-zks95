"""An allowlisted, site-wide theme stored with the other portable settings."""
from .settings import get_runtime_setting

SITE_THEMES = {
    "classic": "Red / Black — текущий дизайн",
    "cyber": "Premium Cyber — новый дизайн",
}


def normalize_site_theme(value):
    return value if value in SITE_THEMES else "classic"


def get_site_theme():
    return normalize_site_theme(get_runtime_setting("SITE_THEME", "classic"))


def cyber_amount_options(minimum, maximum):
    amounts = sorted({min(max(amount, minimum), maximum) for amount in (500, 1000, 2000, 5000)})
    if len(amounts) == 1 and minimum < maximum:
        amounts = sorted({minimum, maximum})
    return amounts
