import config
import db

KEYS = [
    "ANTHROPIC_API_KEY",
    "KIE_AI_API_KEY",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY",
    "R2_SECRET_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_DOMAIN",
]

_ENV_FALLBACKS = {
    "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
    "KIE_AI_API_KEY": config.KIE_AI_API_KEY,
    "R2_ACCOUNT_ID": config.R2_ACCOUNT_ID,
    "R2_ACCESS_KEY": config.R2_ACCESS_KEY,
    "R2_SECRET_KEY": config.R2_SECRET_KEY,
    "R2_BUCKET": config.R2_BUCKET,
    "R2_PUBLIC_DOMAIN": config.R2_PUBLIC_DOMAIN,
}


def get(key):
    value = db.get_setting(key)
    if value:
        return value
    return _ENV_FALLBACKS.get(key)


def set(key, value):
    if key not in KEYS:
        raise ValueError(f"Unknown setting: {key}")
    db.set_setting(key, value)


def _mask(value):
    if len(value) <= 4:
        return "•" * len(value)
    return f"{'•' * (len(value) - 4)}{value[-4:]}"


def status():
    result = {}
    for key in KEYS:
        stored = db.get_setting(key)
        value = get(key)
        result[key] = {
            "configured": bool(value),
            "masked": _mask(value) if value else None,
            "source": "settings" if stored else ("env" if value else None),
        }
    return result
