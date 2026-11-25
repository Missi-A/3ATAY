"""Texte d'un PDF d'annonce (fiche de poste liée depuis une page Carrières), borné en taille et en pages.

`pypdf` est un module Python pur (aucune DLL, même installation sous Windows que les autres dépendances).
Un document illisible (corrompu, chiffré, sans texte) se distingue d'un document lu : `error` n'est vide que si le
texte a été lu. Ce module n'accède pas au réseau.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

MAX_BYTES = 3_000_000
MAX_PAGES = 12
MAX_CHARS = 20_000

logging.getLogger("pypdf").setLevel(logging.ERROR)  # un PDF imparfait n'est pas une erreur de l'application


@dataclass(frozen=True)
class PdfText:
    text: str = ""
    pages: int = 0
    error: str = ""


def clean(text: str) -> str:
    """Puces de PDF → « - », caractères de contrôle retirés, lignes vides réduites."""
    text = re.sub(r"[⟶➔➜→●•▪■◦]\s*", "- ", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.replace("\xa0", " "))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:MAX_CHARS]


def read_pdf(data: bytes) -> PdfText:
    if len(data) > MAX_BYTES:
        return PdfText(error="document trop volumineux")
    if b"%PDF-" not in data[:1024]:
        return PdfText(error="document illisible (ce n'est pas un PDF)")
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return PdfText(pages=len(reader.pages), error="document chiffré")
        total = len(reader.pages)
        text = clean("\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PAGES]))
    except Exception as exc:  # noqa: BLE001 - tout PDF défectueux (pypdf lève des erreurs variées) reste un détail illisible
        return PdfText(error=f"document illisible ({type(exc).__name__})")
    if not text:
        return PdfText(pages=total, error="document sans texte lisible (image ?)")
    return PdfText(text=text, pages=total)
