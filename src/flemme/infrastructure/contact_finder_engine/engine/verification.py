"""Association prudente : le nom seul ne suffit pas à distinguer les homonymes."""

import re
import unicodedata
from urllib.parse import urlsplit

from .pages import host

RELEVANCES = ("rh", "technique", "management", "direction", "general", "autre")
CONFIDENCES = ("verifie", "probable", "incertain")


def company_input_matches(previous, current):
    """Les sessions 1.0 antérieures ne transmettaient pas les téléphones des contacts existants."""
    if previous == current:
        return True
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    old, new = previous.get("contacts"), current.get("contacts")
    if not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new):
        return False
    contacts = []
    for left, right in zip(old, new, strict=True):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        contact = dict(left)
        if "phone" not in contact and "phone" in right:
            contact["phone"] = right["phone"]
        contacts.append(contact)
    return {**previous, "contacts": contacts} == current


def normalize_phone(value):
    """Format international comparable ; aucune preuve d'attribution ou de joignabilité."""
    if not isinstance(value, str):
        return ""
    value = value.strip().removeprefix("tel:").split(";", 1)[0].strip()
    if not re.fullmatch(r"\+?[0-9\s().-]+", value):
        return ""
    value = re.sub(r"[\s().-]", "", value)
    if value.startswith("00"):
        value = "+" + value[2:]
    if re.fullmatch(r"0[1-9][0-9]{8}", value):
        value = "+33" + value[1:]
    if value.startswith("+330"):
        value = "+33" + value[4:]
    if value.startswith("+33"):
        return value if re.fullmatch(r"\+33[1-9][0-9]{8}", value) else ""
    return value if re.fullmatch(r"\+[1-9][0-9]{6,14}", value) else ""


def phone_type(phone):
    phone = normalize_phone(phone)
    if phone.startswith("+33"):
        return "mobile" if phone[3] in "67" else "special" if phone[3] == "8" else "fixe"
    return ""


def contact_relevance(contact):
    """Le rôle explicite prime sur la boîte et son contexte local, jamais sur la fiabilité."""
    for value in (
        contact.get("role", ""),
        contact.get("email", "").split("@", 1)[0],
        contact.get("context", ""),
    ):
        text = normalized(value)
        groups = (
            (
                "autre",
                r"\b(?:dpm|dpo|rgpd|privacy|donnees personnelles|communication|presse|press|achats?|comptabilite|accountant|abuse|no ?reply)\b",
            ),
            (
                "rh",
                r"\b(?:rh|drh|ressources humaines|human resources|recrut\w*|recruit\w*|jobs?|careers?|talent\w*)\b",
            ),
            (
                "technique",
                r"\b(?:techni\w*|ingenieur\w*|ingenierie|engineering|engineer|cto|develop\w*|informatique)\b",
            ),
            (
                "direction",
                r"\b(?:direction|directeur|directrice|dirigeant\w*|fondateur|fondatrice|cofondateur|cofondatrice|president\w*|ceo|gerant\w*|pdg)\b",
            ),
            ("management", r"\b(?:management|manager|responsable|chef|head)\b"),
            ("general", r"\b(?:contact\w*|info|accueil|standard|agence|secretariat|hello)\b"),
        )
        for relevance, pattern in groups:
            if re.search(pattern, text):
                return relevance
    # Personne nommée sans fonction connue : contact général (une boîte inconnue reste « autre »).
    return "general" if contact.get("name") else "autre"


def normalized(text):
    return " ".join(
        re.sub(
            r"[^\w]+",
            " ",
            unicodedata.normalize("NFKD", str(text).translate(str.maketrans("’‘‐‑‒–—", "''-----")))
            .encode("ascii", "ignore")
            .decode()
            .lower(),
        ).split()
    )


def association(company, text):
    plain = normalized(text)
    identifier = company.siren or company.siret[:9]
    if identifier and re.search(
        rf"(?<!\d){identifier}(?:\d{{5}})?(?!\d)", re.sub(r"[ .\-]", "", text)
    ):
        return True, "SIREN publié sur la source"
    name = normalized(company.name)
    city = normalized(company.city or company.location)
    if (
        name
        and re.search(rf"\b{re.escape(name)}\b", plain)
        and city
        and re.search(rf"\b{re.escape(city)}\b", plain)
    ):
        return True, "Nom et localisation publiés sur la source"
    return False, "Association insuffisante : vérifier le SIREN ou le nom et la localisation"


def legal_identifiers(text):
    identifiers = set()
    for match in re.finditer(
        r"\b(?:siren|siret|rcs|r c s|registre du commerce(?: et des societes)?|companies trade register|company register|trade register)\b[^\d]{0,80}((?:\d[ .\-]*){9,14})",
        normalized(text),
    ):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) in (9, 14):
            identifiers.add(digits[:9])
    return identifiers


HOSTING = re.compile(r"h[ée]berg|hosting|hosted by|host provider", re.I)


def identifiers_near_name(company, text, window=300):
    """Identifiants légaux publiés près du nom, hors passages sur l'hébergeur du site."""
    kept = " ".join(part for part in re.split(r"[.;\n|]", text) if not HOSTING.search(part))
    plain, name = normalized(kept), normalized(company.name)
    found = set()
    for match in re.finditer(rf"\b{re.escape(name)}\b", plain) if name else ():
        found |= legal_identifiers(plain[max(0, match.start() - window) : match.end() + window])
    return found


def siren_contradicted(company, text):
    """Identifiant publié près du nom sans celui de l'entreprise, nulle part sur la page."""
    expected = company.siren or company.siret[:9]
    if not expected or association(company, text)[1] == "SIREN publié sur la source":
        return set()
    published = identifiers_near_name(company, text)
    return set() if not published or expected in legal_identifiers(text) else published


def within(domain, domains):
    """Même domaine ou sous-domaine (frontière « . ») : recrutement.atelier.example."""
    return any(domain == item or domain.endswith("." + item) for item in domains if item)


def email_domain(email):
    return host("https://" + email.rsplit("@", 1)[-1]) if "@" in email else ""


def brand_domain(company, email):
    """Domaine dont le nom (avant l'extension) est exactement la marque : arep.fr pour AREP."""
    labels = email_domain(email).split(".")
    brand = re.sub(r"\W", "", normalized(company.name))
    return bool(brand and len(labels) >= 2 and re.sub(r"\W", "", labels[-2]) == brand)


def foreign_email(contact, company, site):
    """Adresse d'un autre domaine que l'entreprise, sans rattachement indépendant établi."""
    email = contact.get("email", "")
    domains = {host(url) for url in (site, company.website) if url}
    return bool(
        email
        and not within(email_domain(email), domains)
        and not brand_domain(company, email)
        and not contact.get("association_verified")
    )


GENERIC_BOX = re.compile(
    r"^(?:contact|info|infos|accueil|hello|bonjour|bienvenue|standard|secretariat|agence|"
    r"service|support|commercial|sales|admin|direction|communication|presse|dpm|dpo|rgpd|"
    r"webmaster|noreply|no-reply|compta\w*)(?:[._-]|$)",
    re.I,
)
CATEGORIES = ("nominatif", "rh", "generique", "telephone", "personne")


def contact_category(contact):
    """Nature réelle d'une coordonnée : e-mail RH, nominatif ou générique, téléphone seul, ou
    personne identifiée sans coordonnées (jamais un contact directement exploitable)."""
    email = contact.get("email", "")
    if email:
        local = email.split("@", 1)[0]
        if contact.get("relevance") == "rh":
            return "rh"
        if not GENERIC_BOX.match(local) and (
            contact.get("name")
            or re.fullmatch(r"[a-z]+[._-][a-z]+(?:[._-][a-z]+)?\d*", local, re.I)
        ):
            return "nominatif"
        return "generique"
    return "telephone" if contact.get("phone") else "personne"


def same_employer(employer, company, names=()):
    """Employeur publié = entreprise : même nom, nom contenu (ALTRAD ENDEL pour ENDEL), sigle
    ou enseigne déclarés au registre."""
    employer = normalized(employer)
    if not employer:
        return False
    for name in (company.name, *names):
        name = normalized(name)
        if name and (employer == name or re.search(rf"\b{re.escape(name)}\b", employer)):
            return True
    return False


def verify_contact(contact, company, site, site_verified, site_level=""):
    """``site_level`` : confiance du site source ; un contact ne la dépasse jamais."""
    local_source = site and host(contact["source_url"]) == host(site)
    site_proof = bool(site_verified and local_source)
    matched = site_proof or contact.get("association_verified", False)
    email = contact.get("email", "")
    phone = normalize_phone(contact.get("phone", ""))
    contact["phone"] = phone
    contact["phone_type"] = phone_type(phone)
    if contact.get("relevance") not in RELEVANCES:
        contact["relevance"] = contact_relevance(contact)
    domains = {host(url) for url in (site, company.website) if url}
    professional = (
        not email
        or within(email_domain(email), domains)
        or site_proof
        and brand_domain(company, email)
    )
    team_page = any(
        part in urlsplit(contact["source_url"]).path.lower().split("/")
        for part in ("team", "equipe", "about", "a-propos", "our-team")
    )
    declared = f"Employeur déclaré : {company.name}" in contact.get("evidence", [])
    if (
        contact.get("name")
        and not (email or phone)
        and not (team_page or declared or contact.get("association_verified"))
    ):
        matched = False
    contact["reliability"] = "reliable" if matched and professional else "uncertain"
    candidate = site or company.website
    probable = bool(
        professional
        and (site_verified or company.website or site_level in ("", "probable"))
        and candidate
        and host(contact["source_url"]) == host(candidate)
        and (phone or email and within(email_domain(email), {host(candidate)}))
        and contact.get("company_name_present")
        and contact.get("contact_page")
    )
    contact["confidence"] = (
        "verifie"
        if contact["reliability"] == "reliable"
        else "probable"
        if probable
        else "incertain"
    )
    contact["email_verification"] = "published_not_mailbox_verified" if email else "not_available"
    if not matched:
        association = "Association à confirmer"
    elif site_proof:
        association = "Site associé à l'entreprise"
    elif contact.get("registry_siren"):
        association = "Rattachement établi par le registre public (SIREN)"
    else:
        association = "Association établie par une source indépendante"
    contact["evidence"] = list(
        dict.fromkeys(
            contact.get("evidence", [])
            + [
                "Coordonnée publiée ; existence de la boîte non vérifiée"
                if email
                else "Profil public sans e-mail"
                if not phone
                else "Contact téléphonique sans e-mail",
                *(["Numéro publié ; attribution et joignabilité non vérifiées"] if phone else []),
                association,
            ]
        )
    )
    return contact
