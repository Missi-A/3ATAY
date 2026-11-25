"""Vérification des sites candidats et domaines plausibles (jamais vérifiés sans preuve).

Un domaine plausible n'est qu'une piste : il n'est « confirmé » que par le SIREN publié ; nom,
localisation et secteur concordants le rendent seulement « probable ».
"""

import re

from .extraction import visible_soup
from .verification import association, legal_identifiers, normalized

LEGAL_FORMS = {"sa", "sas", "sasu", "sarl", "eurl", "sci", "snc", "scop", "groupe", "group"}
# Premiers mots trop génériques pour désigner seuls une marque ou un groupe.
GENERIC_FIRST = {
    "grand",
    "societe",
    "ste",
    "compagnie",
    "cie",
    "etablissements",
    "ets",
    "entreprise",
    "cabinet",
    "agence",
    "bureau",
    "centre",
    "institut",
    "association",
    "les",
    "la",
    "le",
    "atelier",
    "direction",
    "services",
    "international",
    "france",
}
SECTOR_STOP = {"activites", "autres", "travaux", "services", "conseil", "commerce", "gestion"}


def name_words(name):
    return [w for w in normalized(name).split() if w not in LEGAL_FORMS]


# Prénoms courants : un nom « PRÉNOM NOM » désigne une personne, pas une marque ou un groupe.
FIRST_NAMES = {
    "alain", "alexandre", "andre", "antoine", "arnaud", "benoit", "bernard", "bruno", "camille",
    "catherine", "cedric", "charles", "christian", "christophe", "claude", "damien", "daniel",
    "david", "denis", "dominique", "emmanuel", "eric", "fabien", "florent", "francois",
    "franck", "frederic", "gabriel", "georges", "gerard", "gilles", "guillaume", "henri",
    "hugo", "jacques", "jean", "jerome", "joseph", "julien", "laurent", "louis", "lucas",
    "marc", "marcel", "mathieu", "maxime", "michel", "nicolas", "olivier", "pascal", "patrick",
    "paul", "philippe", "pierre", "raphael", "remi", "richard", "robert", "roger", "sebastien",
    "serge", "stephane", "thierry", "thomas", "vincent", "yves", "anne", "annie", "aurelie",
    "brigitte", "caroline", "celine", "christine", "claire", "corinne", "delphine", "elise",
    "emilie", "florence", "francoise", "isabelle", "julie", "laura", "laurence", "lucie",
    "magali", "marie", "martine", "monique", "nathalie", "nicole", "patricia", "sandrine",
    "sophie", "stephanie", "sylvie", "valerie", "veronique", "virginie",
}  # fmt: skip


# Mots d'activité : « PAUL BOULANGERIE » est une enseigne, pas une personne.
DESCRIPTORS = {
    "boulangerie", "patisserie", "industrie", "industries", "services", "service", "conseil",
    "consulting", "immobilier", "transport", "transports", "construction", "batiment", "energie",
    "electricite", "informatique", "technologies", "technologie", "solutions", "ingenierie",
    "automobile", "restaurant", "pharmacie", "garage", "assurances", "formation", "securite",
    "logistique", "environnement", "systemes", "groupe", "group", "holding", "france",
    "engineering", "engineers", "tech", "traiteur", "peinture", "plomberie", "menuiserie",
    "maconnerie", "paysage", "coiffure", "taxi", "auto", "btp", "travaux", "renovation",
    "decoration", "design", "digital", "media", "sante", "medical", "habitat", "location",
    "voyages", "voyage", "traduction", "comptabilite", "expertise", "audit", "finance",
    "gestion", "agence", "cabinet", "bureau", "atelier", "ateliers", "entreprise", "ets",
}  # fmt: skip


def brand_word(company):
    """Premier mot distinctif d'un nom composé (DEKRA INDUSTRIAL → dekra) : marque ou groupe.

    Aucune marque pour un nom de personne (RICHARD PALMER) : le prénom ne désigne pas un groupe."""
    words = name_words(company.name)
    if 2 <= len(words) <= 3 and not DESCRIPTORS.intersection(words):
        if words[0] in FIRST_NAMES or words[-1] in FIRST_NAMES:
            return ""  # PRÉNOM NOM, JEAN-PIERRE NOM, NOM PRÉNOM : une personne, pas une marque
    if len(words) > 1 and len(words[0]) >= 3 and words[0] not in GENERIC_FIRST:
        return words[0]
    return ""


def plausible_domains(company, limit=6, names=()):
    """Au plus ``limit`` domaines .fr/.com : nom complet, marque (premier mot), sigle/enseignes.

    Simples pistes : un domaine deviné n'est jamais confirmé sans le SIREN publié."""
    words = name_words(company.name)
    if not words:
        return []
    stems = ["".join(words)]
    if brand_word(company):
        stems.append(brand_word(company))
    for other in names:
        other_words = name_words(other)
        if other_words and len("".join(other_words)) >= 3:
            stems.append("".join(other_words))
    if 1 < len(words) <= 3:
        stems.append("-".join(words))
    domains = [
        f"https://www.{stem}.{tld}/" for stem in dict.fromkeys(stems) for tld in ("fr", "com")
    ]
    return [d for d in domains if re.fullmatch(r"https://www\.[a-z0-9-]{2,63}\.(fr|com)/", d)][
        :limit
    ]


def commune(company):
    found = re.search(r"\b\d{5}\s+([^\d,]+)", company.city or "")
    words = normalized(found.group(1) if found else company.city or "").split()
    return words[0] if words else ""


def site_hints(company, text):
    """Indices concordants publiés par une page : nom, localisation, secteur."""
    plain = normalized(text)
    name = normalized(company.name)
    sector = [w for w in normalized(company.sector).split() if len(w) >= 6 and w not in SECTOR_STOP]
    place = commune(company)
    return sorted(
        label
        for label, found in (
            ("nom", name and re.search(rf"\b{re.escape(name)}\b", plain)),
            ("localisation", place and re.search(rf"\b{re.escape(place)}\b", plain)),
            ("secteur", any(re.search(rf"\b{re.escape(w)}", plain) for w in sector)),
        )
        if found
    )


def site_confidence(verified, hints, declared=False):
    """confirme (preuve), probable (nom ou groupe + localisation/secteur, ou source publique),
    incertain sinon ; un SIREN publié différent (groupe ou homonyme) reste incertain."""
    if verified:
        return "confirme"
    if "siren_different" in hints:
        return "incertain"
    if declared or ({"nom", "groupe"} & set(hints) and {"localisation", "secteur"} & set(hints)):
        return "probable"
    return "incertain"


def verify_site(company, pages, registry_ids=()):
    evidence = []
    verified = False
    for page in pages:
        text = visible_soup(page).get_text(" ", strip=True)
        matched, reason = association(company, text)
        identifiers = legal_identifiers(text)
        if (
            not matched
            and identifiers.intersection(registry_ids)
            and re.search(rf"\b{re.escape(normalized(company.name))}\b", normalized(text))
        ):
            matched, reason = True, "SIREN concordant entre site et registre public"
        if matched:
            verified = True
            evidence.append({"url": page.url, "reason": reason, "sirens": sorted(identifiers)})
    return verified, evidence
