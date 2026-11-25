"""Domaine d'un poste, déduit de son intitulé, parmi les domaines du catalogue de métiers de FLEMME."""

from __future__ import annotations

import re

from .rome_catalog import DOMAINS, _normalized

# Mots de l'intitulé (sans accents) ; l'ordre fixe la priorité : le premier domaine reconnu l'emporte.
KEYWORDS = {
    "ia": ("intelligence artificielle", "machine learning", "deep learning", "ia generative", " llm", "data scientist"),
    "cybersecurite": ("cyber", "securite informatique", "securite des systemes", "pentest", "soc analyst", "rssi"),
    "donnees": ("data", "donnees", "business intelligence", "big data", "power bi", "bi analyst"),
    "embarque": ("embarque", "firmware", "temps reel", "fpga"),
    "capteurs": ("capteur", "instrumentation", "metrologie", "mesure physique"),
    "electronique": ("electronique", "circuit imprime", "carte electronique", "radiofrequence", "hyperfrequence"),
    "robotique": ("robot", "cobot"),
    "automatisme": ("automatisme", "automation", "automate", "automaticien", "supervision", "gtc", "regulation", "scada"),
    "renouvelables": ("photovolta", "renouvelable", "eolien", "solaire", "hydrogene", "stockage d'energie",
                      "bess", "biomasse", "geothermie"),
    "reseaux-electriques": ("reseaux electriques", "reseau electrique", "reseaux aerien", "reseau aerien",
                            "raccordement", "poste source", "haute tension", " hta", " htb", "smart grid"),
    "genie-electrique": ("electricite", "electrique", "electricien", "courant fort", "courants forts",
                         "courants faibles", "cvc electrique", "installations electriques"),
    "maintenance": ("maintenance", "gmao", "mainteneur", "fiabilite"),
    "developpement": ("developpeur", "developpement logiciel", "developpement web", "logiciel", "informatique", "fullstack",
                      "full stack", "front end", "frontend", "back end", "backend", "devops", "software", "web ",
                      "applicatif", "application", "systemes d'information", "si "),
}
LABELS = {d["id"]: d["label"] for c in DOMAINS for d in c["domains"]}
UNKNOWN = ""


VAGUE = ("si ", "application")  # trop courants dans un texte libre : lus seulement dans un intitulé
MIN_HITS = 2  # indices distincts du poste pour classer sans signal dans l'intitulé


def _text(value: str) -> str:
    return " " + re.sub(r"[^a-z0-9' ]", " ", _normalized(value or "")) + " "


def _found(text: str, words) -> set[str]:
    return {w for w in words if (w if w.startswith(" ") else " " + w) in text}  # début de mot


def domain_of(title: str, mission: str = "") -> str:
    """Identifiant de domaine, ou chaîne vide si les indices manquent ou se contredisent. L'intitulé tranche s'il
    contient un mot du catalogue (premier domaine reconnu). Sinon, `mission` (missions du poste, à défaut son texte)
    classe seulement si un domaine a au moins deux indices distincts et le double du suivant."""
    head = _text(title)
    for domain, words in KEYWORDS.items():
        if domain in LABELS and _found(head, words):
            return domain
    body = _text(mission)
    scores = sorted(((len(_found(body, [w for w in words if w not in VAGUE])), domain)
                     for domain, words in KEYWORDS.items() if domain in LABELS), reverse=True)
    (best, domain), second = scores[0], scores[1][0]
    return domain if best >= MIN_HITS and best >= 2 * second else UNKNOWN


def grouped_domains() -> list[tuple[str, list[tuple[str, str]]]]:
    """[(catégorie, [(id, libellé), ...])] telles que le sélecteur de métiers les présente."""
    return [(c["label"], [(d["id"], d["label"]) for d in c["domains"]]) for c in DOMAINS]
