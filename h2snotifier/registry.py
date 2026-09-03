"""
The one place that knows which sources exist.

Two names are in play for each source and they are not interchangeable: the
config key names the block in config.json, while the store key is what is
written into the database's `source` column. They differ for historical
reasons - the first source shipped as "h2s" - and renaming the store key
would orphan every row already recorded.

Which name a table wants is decided by whoever writes it: `listings` is
filed under the store key, while `runs` and `settings` are written by the
scheduler and so use the config key. Reading one with the other silently
returns nothing rather than failing, so it is worth checking.
"""

SOURCES = (
    {"config": "holland2stay", "store": "h2s", "label": "Holland2Stay"},
    {"config": "funda", "store": "funda", "label": "Funda"},
    {"config": "huurwoningen", "store": "huurwoningen", "label": "Huurwoningen"},
    {"config": "ikwilhuren", "store": "ikwilhuren", "label": "ikwilhuren.nu"},
)

CONFIG_KEYS = tuple(source["config"] for source in SOURCES)
LABELS = {source["store"]: source["label"] for source in SOURCES}
CONFIG_LABELS = {source["config"]: source["label"] for source in SOURCES}


def label(config_key):
    return CONFIG_LABELS.get(config_key, config_key)
