"""
What a group's admins can type to change their filters.

Every function takes the chat id and the text after the command and returns
the reply. Nothing here talks to Telegram, so it is easy to test, and the
Controller stays a router.
"""

import geo
import registry
import subscriptions
from subscriptions import LimitError

SETUP_USAGE = "Send /setup <code> with the invite code the bot owner gave you."

SETUP_DONE = (
    "✅ This group is set up. Alerts will arrive {where}.\n\n"
    "Next: tell me where you are looking.\n"
    "/city add Utrecht 10 - a city and, optionally, a radius in km\n"
    "/price 800 1800 - minimum and maximum rent (use - for no limit)\n"
    "/panel - switch sources on or off\n"
    "/help - everything else\n\n"
    "Until you add a city, nothing will be sent."
)

HELP_GROUP = (
    "🎛 Rental notifier\n\n"
    "/city - list your cities; /city add Utrecht 10; /city remove Utrecht\n"
    "/price 800 1800 - rent range (- means no limit, /price clear removes it)\n"
    "/filters - show all current settings\n"
    "/panel - buttons for sources and pause\n"
    "/status - what is on\n"
    "/last - when each source was last checked\n"
    "/timezone - show or set the clock, e.g. /timezone Europe/Amsterdam\n"
    "/settopic - send alerts to the topic you type this in\n"
    "/pause, /resume - stop or restart alerts for this group\n"
    "/check - run a check now\n\n"
    "Holland2Stay matches the exact city name; the other sources also use the radius."
)

HELP_OWNER = (
    "🛠 Owner commands (private chat)\n\n"
    "/invite - make a one-time code for a friend's group\n"
    "/groups - list active groups\n"
    "/revoke <chat id> - switch a group off\n\n"
    "A friend adds the bot to their group and, as a group admin, sends /setup <code>."
)

CITY_USAGE = "Usage: /city add <name> [km] · /city remove <name> · /city"


def parse_radius(token):
    try:
        value = float(token.lower().removesuffix("km"))
    except ValueError:
        return None
    return value if 0 < value <= 100 else None


def city(chat_id, argument):
    parts = (argument or "").split()
    if not parts:
        return _cities_line(subscriptions.get(chat_id))
    action, rest = parts[0].lower(), parts[1:]

    if action == "add":
        radius = None
        if len(rest) > 1 and parse_radius(rest[-1]) is not None:
            radius = parse_radius(rest[-1])
            rest = rest[:-1]
        name = " ".join(rest).strip()
        if not name:
            return CITY_USAGE
        if not geo.locate_town(name):
            return (f"❓ I could not find a Dutch town called {name!r}. "
                    "Check the spelling (or the lookup service may be down - try again).")
        try:
            subscriptions.add_location(chat_id, name, radius)
        except LimitError as exc:
            return f"⛔ {exc}"
        reach = f" within {radius:g} km" if radius else ""
        return f"✅ Watching {name}{reach}."

    if action in ("remove", "rm", "del"):
        name = " ".join(rest).strip()
        if not name:
            return CITY_USAGE
        if subscriptions.remove_location(chat_id, name):
            return f"✅ Removed {name}."
        return f"{name} is not on your list."

    return CITY_USAGE


def _amount(token):
    if token in ("-", "any"):
        return None
    value = int(token.replace("€", "").replace(".", "").replace(",", ""))
    if value < 0:
        raise ValueError(token)
    return value


PRICE_USAGE = "❓ Usage: /price <min> <max>, for example /price 800 1800 (use - for no limit, /price clear to remove)."


def price(chat_id, argument):
    parts = (argument or "").split()
    if parts and parts[0].lower() in ("clear", "off", "none"):
        subscriptions.set_price(chat_id, None, None)
        return "✅ Price filter removed."
    if len(parts) != 2:
        return PRICE_USAGE
    try:
        low, high = (_amount(part) for part in parts)
    except ValueError:
        return PRICE_USAGE
    if low is not None and high is not None and low > high:
        return "❓ The minimum is higher than the maximum."
    subscriptions.set_price(chat_id, low, high)
    return f"✅ Rent range: {_price_line(low, high)}."


def _price_line(low, high):
    if low is None and high is None:
        return "any price"
    if low is None:
        return f"up to €{high}"
    if high is None:
        return f"from €{low}"
    return f"€{low} - €{high}"


def _place(loc):
    text = loc.city
    if loc.radius_km:
        text += f" ({loc.radius_km:g} km)"
    if loc.source:
        text += f" [{registry.label(loc.source)} only]"
    return text


def _cities_line(sub):
    if sub is None or not sub.locations:
        return "No cities yet. Add one with /city add Utrecht 10"
    return "📍 " + ", ".join(_place(loc) for loc in sub.locations)


def filters_text(sub):
    sources = [registry.label(key) for key in registry.CONFIG_KEYS if key in sub.sources]
    lines = [
        "🔎 Filters for this group",
        "",
        _cities_line(sub),
        f"💶 {_price_line(sub.price_min, sub.price_max)}",
        f"📡 Sources: {', '.join(sources) if sources else 'none'}",
    ]
    if sub.paused:
        lines.append("⏸ Alerts are paused")
    if not sub.locations:
        lines.append("\nNo alerts will arrive until you add a city.")
    return "\n".join(lines)
