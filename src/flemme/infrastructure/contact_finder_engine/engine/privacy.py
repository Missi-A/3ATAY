"""Masquage local aux sorties Finder, sans modifier les URL utilisées pour lire."""

import os
import re
from urllib.parse import quote, quote_plus, unquote_plus

SENSITIVE = {"key", "token", "api_key", "access_token", "password", "secret"}
PARAMETER = re.compile(r"([?&](?:amp;)?)([^?&=\s\"'<>]+)=([^&#\s\"'<>\\]*)")


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    key = os.getenv("CONTACT_FINDER_LBA_API_KEY", "").strip()
    for secret in (key, *secrets):
        if secret:
            for variant in {secret, quote(secret, safe=""), quote_plus(secret)}:
                value = value.replace(variant, "[REDACTED]")
    return PARAMETER.sub(
        lambda match: (
            match[1] + match[2] + "=[REDACTED]"
            if unquote_plus(match[2]).lower() in SENSITIVE
            else match[0]
        ),
        value,
    )
