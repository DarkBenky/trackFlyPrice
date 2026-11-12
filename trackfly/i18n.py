"""Tiny message catalogue (English / Russian) for alerts, stats and help."""

from __future__ import annotations

from datetime import date, datetime, timezone

SUPPORTED = ("en", "ru")

MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ru": ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"),
}
WEEKDAYS = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "new_low": "✈️ New lowest price: {origin} → {dest}",
        "watching": "✈️ Watching {origin} → {dest}",
        "round_trip": "{cur} {price} round trip",
        "was": "  (was {cur} {prev}, −{pct}%)",
        "window_low": "🏆 Lowest for the whole trip window",
        "first_obs": " (first observation)",
        "stats_title": "✈️ {origin} → {dest} price tracker",
        "lowest_now": "Lowest now: {cur} {price} · {dep} → {ret}",
        "all_time": "All-time low: {cur} {price} · {dep} → {ret}",
        "no_prices": "No prices yet - first poll still running.",
        "pairs_line": "{pairs} · {runs} · last check {when}",
        "cheapest": "Cheapest date pairs:",
        "by_date": "Date pairs by departure:",
        "only_nonstop": "Date pairs (nonstop):",
        "max_stops": "Date pairs (up to {n} stop{s}):",
        "no_match": "No date pair matches that filter.",
        "no_pairs": "No priced date pairs yet.",
        "checks_every": "Checks every {minutes} min",
        "stops_none": "nonstop",
        "stops_one": "1 stop",
        "stops_many": "{n} stops",
        "help_intro": "Send one of these words:",
        "help_stats": "  stats          - current prices + chart link",
        "help_nonstop": "  stats nonstop  - only nonstop flights",
        "help_max": "  stats 1        - at most 1 stop",
        "help_date": "  stats date     - sorted by date, not price",
        "help_help": "  help           - this message",
        "help_footer": "Checks every {minutes} min, alerts on new lows.",
    },
    "ru": {
        "new_low": "✈️ Новая минимальная цена: {origin} → {dest}",
        "watching": "✈️ Слежу за {origin} → {dest}",
        "round_trip": "{cur} {price} туда-обратно",
        "was": "  (было {cur} {prev}, −{pct}%)",
        "window_low": "🏆 Минимум за весь период",
        "first_obs": " (первая проверка)",
        "stats_title": "✈️ {origin} → {dest} — мониторинг цен",
        "lowest_now": "Сейчас минимум: {cur} {price} · {dep} → {ret}",
        "all_time": "Исторический минимум: {cur} {price} · {dep} → {ret}",
        "no_prices": "Цен пока нет — идёт первая проверка.",
        "pairs_line": "{pairs} · {runs} · последняя проверка {when}",
        "cheapest": "Самые дешёвые варианты:",
        "by_date": "Варианты по дате вылета:",
        "only_nonstop": "Варианты (без пересадок):",
        "max_stops": "Варианты (до {n} {s}):",
        "no_match": "Нет вариантов под этот фильтр.",
        "no_pairs": "Пока нет цен.",
        "checks_every": "Проверка каждые {minutes} мин",
        "stops_none": "без пересадок",
        "stops_one": "1 пересадка",
        "stops_many": "{n} пересадки",
        "help_intro": "Отправьте одну из команд:",
        "help_stats": "  stats          — текущие цены и ссылка на график",
        "help_nonstop": "  stats nonstop  — только прямые рейсы",
        "help_max": "  stats 1        — максимум 1 пересадка",
        "help_date": "  stats date     — по дате вылета, а не по цене",
        "help_help": "  help           — это сообщение",
        "help_alias": "  цены, помощь   — то же самое по-русски",
        "help_footer": "Проверка каждые {minutes} мин, уведомления о новых минимумах.",
    },
}


def normalize(lang: str | None) -> str:
    code = (lang or "").strip().lower()[:2]
    return code if code in SUPPORTED else "en"


def get(lang: str, key: str) -> str:
    return STRINGS[normalize(lang)].get(key) or STRINGS["en"][key]


def _plural_ru(number: int, one: str, few: str, many: str) -> str:
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def stops(lang: str, count: int | None) -> str:
    if count is None:
        return ""
    lang = normalize(lang)
    if lang == "ru":
        if count == 0:
            return get(lang, "stops_none")
        return f"{count} {_plural_ru(count, 'пересадка', 'пересадки', 'пересадок')}"
    if count == 0:
        return get(lang, "stops_none")
    if count == 1:
        return get(lang, "stops_one")
    return get(lang, "stops_many").format(n=count)


def duration(lang: str, minutes: int | None) -> str:
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if normalize(lang) == "ru":
        return f"{hours} ч {mins:02d} мин" if hours else f"{mins} мин"
    return f"{hours}h{mins:02d}" if hours else f"{mins}m"


def pairs_phrase(lang: str, pairs: int) -> str:
    if normalize(lang) == "ru":
        return f"{pairs} {_plural_ru(pairs, 'вариант дат', 'варианта дат', 'вариантов дат')}"
    return f"{pairs} date pairs"


def polls_phrase(lang: str, polls: int) -> str:
    if normalize(lang) == "ru":
        return f"{polls} {_plural_ru(polls, 'проверка', 'проверки', 'проверок')}"
    return f"{polls} polls"


def max_stops_phrase(lang: str, count: int) -> str:
    if normalize(lang) == "ru":
        return get(lang, "max_stops").format(
            n=count, s=_plural_ru(count, "пересадки", "пересадок", "пересадок")
        )
    return get(lang, "max_stops").format(n=count, s="" if count == 1 else "s")


def fmt_day(lang: str, value: str | date) -> str:
    moment = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{moment.day} {MONTHS[normalize(lang)][moment.month - 1]}"


def fmt_long(lang: str, moment: date) -> str:
    code = normalize(lang)
    return f"{WEEKDAYS[code][moment.weekday()]} {moment.day} {MONTHS[code][moment.month - 1]} {moment.year}"


def fmt_when(lang: str, iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    return f"{local.day} {MONTHS[normalize(lang)][local.month - 1]} {local:%H:%M}"
