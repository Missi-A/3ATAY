"""Catalogue local des métiers ROME, indépendant de l'interface et du réseau."""

import json
import unicodedata
from pathlib import Path

_catalog = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
ROME_LABELS = _catalog["codes"]
DOMAINS = _catalog["categories"]


def label_of(code: str) -> str:
    return ROME_LABELS.get(code, code)


def _normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if not unicodedata.combining(c))


def search(text: str) -> dict[str, str]:
    needle = _normalized(text.strip())
    return {code: name for code, name in ROME_LABELS.items()
            if needle in _normalized(f"{code} {name}")}


def domain_codes(ids) -> list[str]:
    return list(dict.fromkeys(code for category in DOMAINS for domain in category["domains"]
                              if domain["id"] in ids for code in domain["romes"]))
