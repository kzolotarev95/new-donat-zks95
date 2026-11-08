"""Safe global visual effect choices shared by public pages and admin."""

VISUAL_EFFECTS = {
    "none": "Выключено",
    "snow": "Снег",
    "space": "Космос",
    "aurora": "Аврора",
    "grid": "Техно-сетка",
}
VISUAL_SPEEDS = {
    "very-slow": "Очень медленно",
    "slow": "Медленно",
    "normal": "Нормально",
    "fast": "Быстро",
    "very-fast": "Очень быстро",
}


def normalize_visual_effect(value):
    value = (value or "none").strip().lower()
    return value if value in VISUAL_EFFECTS else "none"


def normalize_visual_speed(value):
    value = (value or "normal").strip().lower()
    return value if value in VISUAL_SPEEDS else "normal"
