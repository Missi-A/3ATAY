"""Même validation ROME pour la saisie, les sources et les offres enregistrées."""

import re


def parse_romes(text):
    codes = list(dict.fromkeys(code for code in re.split(r"[,;\s]+", text.strip().upper()) if code))
    if text.strip() and not codes:
        raise ValueError("Saisissez un code ROME, par exemple M1805.")
    invalid = [code for code in codes if not re.fullmatch(r"[A-Z][0-9]{4}", code)]
    if invalid:
        raise ValueError("Code ROME invalide : " + ", ".join(invalid) + ". Exemple : M1805.")
    return tuple(codes)
