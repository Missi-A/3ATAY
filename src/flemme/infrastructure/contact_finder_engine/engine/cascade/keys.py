"""Clés de dédoublonnage : une même information trouvée par plusieurs sources a une seule clé.

Une personne n'est jamais identifiée par son seul nom : la clé reprend son e-mail ou son
téléphone, sinon nom + rôle + domaine de l'entreprise (deux homonymes restent distincts).
"""

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..verification import normalize_phone

# Suffixes publics à deux niveaux courants : « ex.co.uk » et « ville.gouv.fr » restent distincts.
TWO_LEVEL = {
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.au",
    "net.au",
    "co.jp",
    "co.nz",
    "co.za",
    "com.br",
    "com.cn",
    "com.tr",
    "co.in",
    "gouv.fr",
    "asso.fr",
    "nom.fr",
    "tm.fr",
    "com.fr",
    "presse.fr",
    "cci.fr",
    "gov.fr",
}
# Hébergements partagés : chaque sous-domaine est un éditeur distinct (a.github.io ≠ b.github.io).
SHARED_HOSTS = {
    "github.io",
    "gitlab.io",
    "netlify.app",
    "vercel.app",
    "herokuapp.com",
    "pages.dev",
    "wixsite.com",
    "wordpress.com",
    "blogspot.com",
    "webflow.io",
    "azurewebsites.net",
    "site-solocal.com",
    "jimdofree.com",
    "business.site",
    "recruitee.com",
    "teamtailor.com",
}
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def plain(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9@.+ -]", " ", text.lower())).strip()


def email_key(value):
    value = str(value or "").strip().lower()
    value = value.removeprefix("mailto:").split("?")[0].strip().strip(".")
    return value if EMAIL.match(value) else ""


# Longueur du numéro national (indicatif exclu) pour les pays rencontrés ; ailleurs, E.164 seul.
NATIONAL_LENGTHS = {
    "1": (10, 10), "33": (9, 9), "41": (9, 9), "44": (9, 10), "32": (8, 9), "34": (9, 9),
    "39": (6, 11), "49": (6, 13), "91": (10, 10), "65": (8, 8), "86": (9, 12), "212": (9, 9),
    "216": (8, 8), "213": (8, 9), "352": (6, 11), "377": (8, 9), "269": (7, 7),
    # Outre-mer français (Mayotte : +262 ; +269 = Comores, étranger).
    "262": (9, 9), "590": (9, 9), "594": (9, 9), "596": (9, 9), "508": (6, 6), "681": (6, 6),
    "687": (6, 6), "689": (8, 8),
}  # fmt: skip
FRENCH_CODES = {"33", "262", "508", "590", "594", "596", "681", "687", "689"}


def country_code(value):
    """Indicatif d'un numéro E.164 (+33…), parmi les indicatifs connus ; "" sinon."""
    digits = str(value or "").removeprefix("+")
    known = set(NATIONAL_LENGTHS) | FRENCH_CODES
    return next((digits[:n] for n in (3, 2, 1) if digits[:n] in known), "")


def phone_key(value):
    """Format international E.164 (``normalize_phone`` de l'itération 2) ; numéro invalide → "".

    Contrôle du plan national pour les pays connus (longueur, pas de « 0 » de préfixe recopié
    après l'indicatif, sauf l'Italie où il fait partie du numéro)."""
    value = normalize_phone(str(value or ""))
    digits = value.removeprefix("+")
    if not value.startswith("+") or not digits.isdigit() or digits.startswith("0"):
        return ""
    if not (8 <= len(digits) <= 15 and len(set(digits)) > 1):
        return ""
    code = country_code(value)
    national = digits[len(code) :]
    if code and code != "39" and national.startswith("0"):
        return ""
    low, high = NATIONAL_LENGTHS.get(code, (1, 15))
    return value if low <= len(national) <= high else ""


def foreign_phone(value):
    """Numéro valide d'un autre pays que la France (outre-mer compris)."""
    key = phone_key(value)
    return bool(key) and country_code(key) not in FRENCH_CODES


TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|_hs|ref$|source$)", re.I)


def url_key(value, keep_query=False):
    """URL sans schéma ni « www. », sans fragment ni barre finale. ``keep_query`` : paramètres
    d'identité conservés (offre ``?id=1`` ≠ ``?id=2``), paramètres de suivi retirés."""
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path or "").rstrip("/")
    query = ""
    if keep_query:
        pairs = sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING.match(k))
        query = "?" + urlencode(pairs) if pairs else ""
    return f"{host}{path}{query}" if host else ""


def domain_key(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if "@" in value and "://" not in value:
        value = value.rsplit("@", 1)[1]
    host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").lower()
    labels = [x for x in host.removeprefix("www.").split(".") if x]
    if len(labels) < 2:
        return ""
    suffix = ".".join(labels[-2:])
    size = 3 if (suffix in TWO_LEVEL or suffix in SHARED_HOSTS) and len(labels) >= 3 else 2
    return ".".join(labels[-size:])


def company_key(siren="", siret="", name="", city=""):
    siren = re.sub(r"\D", "", str(siren or ""))
    siret = re.sub(r"\D", "", str(siret or ""))
    if len(siren) == 9:
        return f"siren:{siren}"
    if len(siret) == 14:
        return f"siren:{siret[:9]}"
    return f"nom:{plain(name)}|{plain(city)}"


def person_key(name, email="", phone="", role="", domain=""):
    if email_key(email):
        return f"email:{email_key(email)}"
    if phone_key(phone):
        return f"personne:{plain(name)}|{phone_key(phone)}"
    if not plain(name) or not (plain(role) and domain_key(domain)):
        return ""  # jamais une personne identifiée par son seul nom
    return f"personne:{plain(name)}|{plain(role)}|{domain_key(domain)}"


def finding_key(kind, value, **extra):
    """Clé unique d'une information, selon sa nature."""
    if kind == "email":
        key = email_key(value)
    elif kind == "phone":
        key = phone_key(value)
    elif kind == "site":
        key = domain_key(value)
    elif kind == "person":
        key = person_key(value, **extra)
    elif kind in ("form", "portal", "offer", "page", "profile", "lead_portal", "establishment"):
        key = url_key(value, keep_query=kind == "offer")
    else:
        key = plain(value)
    return f"{kind}:{key}" if key else ""
