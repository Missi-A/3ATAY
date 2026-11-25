"""Texte d'une annonce : HTML → texte structuré (paragraphes, listes, rubriques) et détails publiés.

Le texte des annonces est relevé par `company_portal_collector` ; ce module n'accède pas au réseau.
"""

from __future__ import annotations

import html as H
import re

MAX_TEXT = 20_000  # un texte plus long est coupé à la fin d'un paragraphe
NOISE = r"script|style|nav|header|footer|aside|form|noscript|svg|iframe"  # menus et contenu générique des portails
FACTS = ("duree", "debut", "teletravail")
SECTIONS = {
    "missions": r"(?:vos |les |nos )?missions?(?: principales?)?|responsabilit[ée]s?|activit[ée]s|descriptif(?: du (?:poste|stage))?"
                r"|le poste|ce que vous ferez|your (?:role|mission)s?|what you(?:'ll| will) do",
    "profil": r"(?:votre |le )?profil(?: recherch[ée])?|vous [êe]tes|qualifications?|formation|requirements?|who you are",
    "competences": r"(?:les |vos )?comp[ée]tences?(?: requises| cl[ée]s| techniques)?|skills|savoir[- ]faire|outils",
}
# Rubriques qui ferment une section sans être un détail recherché (avantages, présentation de l'employeur…).
STOP = (r"avantages?|r[ée]mun[ée]ration|processus de recrutement|[àa] propos(?: de nous)?|pourquoi nous rejoindre|"
        r"informations? compl[ée]mentaires?|about us|benefits|nous rejoindre|l'entreprise|notre entreprise|"
        r"notre offre|pour postuler|postuler|candidater|contact")
HEADING = re.compile(r"^(?:%s)$" % "|".join([*SECTIONS.values(), STOP]), re.I)
FACT_LINE = re.compile(r"^\W*(?:dur[ée]e|date de d[ée]but|d[ée]but|d[ée]marrage|start date|t[ée]l[ée]travail)\b", re.I)
FACT_RE = {
    "duree": re.compile(r"dur[ée]e(?: du (?:stage|contrat))?\s*:\s*(.{2,80})|(?:stage|alternance|contrat)[^.\n]{0,25}?"
                        r"(\d+(?:\s*(?:à|-|ou|to)\s*\d+)?\s*(?:mois|semaines|ans))", re.I),
    "debut": re.compile(r"(?:date de d[ée]but|d[ée]but(?: du (?:stage|contrat))?|d[ée]marrage|start date)\s*:\s*(?:le |en )?(.{3,50})"
                        r"|(?:[àa] pourvoir|d[ée]marrage|d[ée]but)[^.\n]{0,15}(?:[àa] partir d[eu]|d[èe]s)\s+(.{3,40})", re.I),
    "teletravail": re.compile(r"(.{0,40}(?:t[ée]l[ée]travail|remote|hybride|hybrid|work from home).{0,60})", re.I),
}


def html_to_text(source: str, limit: int = MAX_TEXT) -> str:
    """Texte lisible : un retour à la ligne par bloc, une ligne vide entre paragraphes, listes en « - »."""
    h = re.sub(rf"<({NOISE})\b[^>]*>.*?</\1>", " ", source or "", flags=re.S | re.I)
    h = re.sub(r"<li\b[^>]*>", "\n- ", h, flags=re.I)
    h = re.sub(r"</?(?:p|div|br|ul|ol|h[1-6]|tr|section|article|table)\b[^>]*>", "\n", h, flags=re.I)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in H.unescape(re.sub(r"<[^>]+>", "", h)).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0].rstrip()
    return text


def main_content(page: str) -> str:
    """Contenu principal d'une page sans bloc JobPosting : <main> ou <article> s'il existe, sinon le corps sans menus."""
    m = re.search(r"<(main|article)\b[^>]*>(.*)</\1>", page or "", re.S | re.I)
    return html_to_text(m.group(2) if m else page)


def _heading(line: str) -> str:
    return re.sub(r"^[\s\-*•#]+|[\s:：]+$", "", line)


def extract_details(text: str, explicit: dict | None = None) -> dict[str, str]:
    """Détails publiés : missions, profil, compétences (rubriques du texte), durée, début et télétravail.
    Les valeurs fournies par la source (`explicit`) priment ; une rubrique absente n'est jamais inventée.
    shortcut: rubriques reconnues par leur titre usuel seulement, un titre inhabituel ne donne aucun détail."""
    lines = (text or "").split("\n")
    details: dict[str, str] = {}
    current, body = "", []

    def close():
        if current and current not in details and (value := " ".join(body).strip()):
            details[current] = value[:1500]

    for line in lines:
        title = _heading(line)
        if title and len(title) <= 60 and HEADING.match(title):
            close()
            current = next((k for k, p in SECTIONS.items() if re.fullmatch(p, title, re.I)), "")
            body = []
        elif FACT_LINE.match(line):  # un détail chiffré ferme la rubrique en cours
            close()
            current, body = "", []
        elif current and line.strip():
            body.append(line.lstrip("- ").strip())
    close()
    for fact in FACTS:
        for line in lines:
            if m := FACT_RE[fact].search(line):
                details.setdefault(fact, next(g for g in m.groups() if g).strip(" .;,"))
                break
    details.update({k: v for k, v in (explicit or {}).items() if v})
    return details
