"""Comparaison de textes commune aux filtres et à la recherche."""

import html
import re
import unicodedata


def normalized(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


PARTICLES = {"en", "de", "du", "des", "la", "le", "les", "sur", "sous", "et", "lès"}


def comparable(text: str) -> str:
    """Espaces, apostrophes typographiques et formes Unicode unifiés ; les mots restent exacts."""
    text = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def commune_name(address: str) -> str:
    """Nom de commune lisible à partir d'une adresse V4/LBA (« 6 PLACE X 13001 MARSEILLE » → « Marseille »)."""
    text = " ".join(address.split())
    parts = re.split(r"\b\d{5}\b", text)
    if len(parts) > 1:
        text = parts[-1].strip(" ,-") or parts[-2].strip(" ,-")
    text = re.sub(r"\s+(?:cedex\b.*|\d+(?:e|er|ème)\s+arrondissement)$", "", text, flags=re.I).strip(" ,-")
    if text.isupper():
        words = re.split(r"([\s-])", text.lower())
        text = "".join(w if w in PARTICLES and i else w[:1].upper() + w[1:] for i, w in enumerate(words))
    return text


_TIRETS = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_GUILLEMETS = dict.fromkeys(map(ord, "«»“”„‟"), '"')
_APOSTROPHES = dict.fromkeys(map(ord, "’‘‛ʼ′`´"), "'")


def citation_key(text: str) -> str:
    """Clé de comparaison d'une citation : entités HTML (aussi numériques, &nbsp;), NFKC, apostrophes, guillemets,
    tirets, espaces et accents unifiés. Les mots restent exacts : aucune reformulation n'est admise."""
    text = text or ""
    for _ in range(3):  # entités parfois encodées deux fois
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = unicodedata.normalize("NFKC", text).translate(_TIRETS).translate(_GUILLEMETS).translate(_APOSTROPHES)
    text = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)  # « mot , mot » : espace avant la ponctuation sans effet
    return re.sub(r"\s+", " ", text).strip()
