"""Extraction des coordonnées publiées et des personnes explicitement présentées."""

import json
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import utc_now, web_url
from .pages import host, link_base
from .verification import contact_relevance, normalize_phone, normalized, phone_type, within

EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?)+",
    re.I,
)
ROLE = re.compile(
    r"\b(?:responsable\s+(?:du\s+|des\s+)?(?:recrutement|ressources humaines|rh|technique|ingenierie)|direct(?:eur|rice)\s+(?:des\s+)?(?:ressources humaines|technique|rh)|charge[e]?\s+(?:de\s+)?recrutement|recruteur|recruteuse|recruiter|talent acquisition|human resources|engineering manager|head of engineering|cto|ceo|pdg|president[e]?|gerant[e]?|cofondateur|cofondatrice|fondateur|fondatrice|dirigeant[e]?|ingenieur[e]?)\b",
    re.I,
)
NAME = re.compile(r"^[A-ZÀ-ÖØ-Ý][\wÀ-ÿ'’-]+(?:\s+[A-ZÀ-ÖØ-Ý][\wÀ-ÿ'’-]+){1,4}$")
PHONE = re.compile(
    r"(?<![\w+])(?:\+33[\s.-]*(?:\(0\)[\s.-]*)?[1-9](?:[\s.-]*[0-9]{2}){4}|0[1-9](?:[\s.-]*[0-9]{2}){4}|\+[1-9](?:[\s.()-]*[0-9]){6,14})(?![\w]|[\s.()-]*[0-9])"
)
SERVICE = re.compile(
    r"\b(?:service\s+(?:des\s+)?(?:ressources humaines|rh|recrutement|technique|communication|presse|achats|comptabilite|dpo|dpm|rgpd)|ressources humaines|recrutement|standard|accueil|dpo|dpm|rgpd|communication|presse|achats|comptabilite|agence(?:\s+(?:de|du|d)\s+[a-z]+)?)\b",
    re.I,
)
VENDOR = re.compile(
    r"\b(?:hebergement|hebergeur|hosted by|hosting|realisation du site|web agency|web design)\b"
)


def contact_block(node):
    """Contexte borné à une carte ; ne pas hériter du rôle d'une navigation voisine."""
    block = node.parent
    for parent in node.parents:
        if parent.name in ("main", "body", "html", "[document]"):
            break
        if len(parent.get_text(" ", strip=True)) > 1200:
            break
        block = parent
        if parent.name in ("article", "li", "div", "section", "address", "tr"):
            break
    return block


def unrelated_block(block, company, email=""):
    text = normalized(block.get_text(" ", strip=True))
    employer = block.select_one('[itemprop="worksFor"], .employer, .company')
    own_email = bool(email and within(email.rsplit("@", 1)[-1].lower(), {host(company.website)}))
    return bool(
        employer
        and normalized(employer.get_text(" ", strip=True)) != normalized(company.name)
        or not own_email
        and (
            VENDOR.search(text)
            or normalized(company.name) not in text
            and re.search(r"\b(?:ag|gmbh|sarl|sas|llc)\b", text)
        )
    )


def phones_in(block):
    if block.name in ("body", "html", "[document]"):
        return []
    values = [unquote(a["href"]) for a in block.select('a[href^="tel:"]')]
    values.extend(PHONE.findall(block.get_text(" ", strip=True)))
    return list(dict.fromkeys(phone for value in values if (phone := normalize_phone(value))))


def service_in(block):
    if block.name in ("body", "html", "[document]"):
        return ""
    for node in block.select("h1, h2, h3, h4, strong, .service, .department"):
        label = node.get_text(" ", strip=True)
        if len(label) <= 100 and SERVICE.search(normalized(label)):
            return label
    match = SERVICE.search(normalized(block.get_text(" ", strip=True)))
    return match.group(0) if match else ""


def structured(soup):
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            if "@type" in value:
                yield value
            for item in value.values():
                if isinstance(item, (dict, list)):
                    yield from walk(item)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, RecursionError):
            continue


def has_type(record, kind):
    value = record.get("@type", [])
    return kind in (value if isinstance(value, list) else [value])


def valid_email(value):
    return bool(len(value) <= 254 and EMAIL.fullmatch(value) and len(value.split("@")[0]) <= 64)


def visible_soup(page):
    if hasattr(page, "_visible_soup"):
        return page._visible_soup
    soup = BeautifulSoup(page.body, "html.parser")
    for node in soup.select("script, style, noscript, template, [hidden], [aria-hidden='true']"):
        node.decompose()
    for node in soup.select("[style]"):
        if node.attrs is not None and re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", node["style"], re.I
        ):
            node.decompose()
    page._visible_soup = soup
    return soup


def person_in(block):
    if block.name in ("body", "html", "[document]"):
        return "", ""
    text = block.get_text(" ", strip=True)
    if len(text) > 1200:
        return "", ""
    match = ROLE.search(normalized(text))
    if not match:
        return "", ""
    role_node = block.select_one('[itemprop="jobTitle"], .role, .job-title, .position, .fonction')
    if role_node is None and not any(
        ROLE.match(normalized(line)) for line in block.stripped_strings if len(line) < 100
    ):
        return "", ""
    role = role_node.get_text(" ", strip=True) if role_node else match.group(0)
    for node in block.select('[itemprop="name"], h2, h3, h4, strong, .name'):
        name = node.get_text(" ", strip=True)
        if NAME.fullmatch(name) and not ROLE.search(normalized(name)):
            return name, role
    # shortcut: only explicit name/role cards and simple name–role lines; add a local NLP model if unstructured biographies become necessary.
    for line in block.stripped_strings:
        name = re.split(r"\s+[—–|:]\s*", line)[0].strip()
        if NAME.fullmatch(name) and not ROLE.search(normalized(name)):
            return name, role
    return "", role


def contact_form(page):
    """Formulaire de message (zone de texte + e-mail), hors recherche et connexion."""
    for form in page.soup.select("form"):
        fields = " ".join(
            f"{node.get('type', '')} {node.get('name', '')}" for node in form.select("input")
        ).lower()
        if (
            form.select("textarea")
            and re.search(r"\bemail\b|mail", fields)
            and not re.search(r"password|passwd|search|\bq\b", fields)
        ):
            return True
    return False


def extract_contacts(page, company):
    soup = visible_soup(page)
    contacts = []
    vendor_emails = set()
    for node in soup.find_all(string=EMAIL):
        context = node.parent
        for parent in node.parents:
            text = parent.get_text(" ", strip=True)
            if parent.name in ("body", "html", "[document]") or len(text) > 600:
                break
            context = parent
            if not EMAIL.fullmatch(text):
                break
        text = normalized(context.get_text(" ", strip=True))
        vendor = re.search(
            r"hebergement|hebergeur|hosted by|hosting|realisation du site|web agency|web design",
            text,
        )
        foreign_legal_entity = normalized(company.name) not in text and re.search(
            r"\b(?:ag|gmbh|sarl|sas|llc)\b", text
        )
        if vendor or foreign_legal_entity:
            official_domain = host(company.website) if company.website else ""
            for email in EMAIL.findall(node):
                domain = email.rsplit("@", 1)[-1].lower()
                if official_domain and (
                    domain == official_domain or domain.endswith("." + official_domain)
                ):
                    continue
                vendor_emails.add(email.lower())

    page_text = normalized(soup.get_text(" ", strip=True))
    company_named = bool(
        normalized(company.name)
        and re.search(rf"\b{re.escape(normalized(company.name))}\b", page_text)
    )
    contact_page = bool(
        re.search(r"\b(?:contacts?|contactez|contacter)\b", normalized(urlsplit(page.url).path))
        or any(
            re.search(
                r"\b(?:contacts?|contactez|contacter)\b", normalized(node.get_text(" ", strip=True))
            )
            for node in soup.select("h1, title")
        )
    )

    def add(name="", role="", email="", profile="", evidence=None, phone="", context=""):
        if normalized(name) == normalized(company.name):
            name, role = "", ""
        if email:
            email = email.removeprefix("mailto:").split("?", 1)[0].strip().lower()
            if not valid_email(email):
                return
            if email in vendor_emails:
                return
        phone = normalize_phone(phone)
        if not (email or name or phone):
            return
        contacts.append(
            dict(
                name=name,
                role=role,
                email=email,
                phone=phone,
                phone_type=phone_type(phone),
                relevance=contact_relevance(dict(role=role, email=email, context=context)),
                confidence="incertain",
                company_name_present=company_named,
                contact_page=contact_page,
                profile_url=profile if web_url(profile) else "",
                company_id=company.id,
                company=company.name,
                source_url=page.url,
                discovered_at=utc_now(),
                reliability="uncertain",
                evidence=(evidence or ["Coordonnée publiée dans une page publique"])
                + (["Nom de l'entreprise publié sur la page"] if company_named else [])
                + (["Page contact du site candidat"] if contact_page else []),
            )
        )

    for record in structured(page.soup):
        if has_type(record, "Person"):
            employer = record.get("worksFor", {})
            employer = employer.get("name", "") if isinstance(employer, dict) else str(employer)
            if employer and normalized(employer) != normalized(company.name):
                continue
            name = record.get("name", "")
            if not isinstance(name, str):
                continue
            role = str(record.get("jobTitle") or "")
            if not ROLE.search(normalized(role)):
                continue
            email = record.get("email", "")
            links = record.get("sameAs", [])
            links = links if isinstance(links, list) else [links]
            profile = next((u for u in links if isinstance(u, str) and web_url(u)), "")
            add(
                name,
                role,
                email if isinstance(email, str) else "",
                profile,
                [
                    "Fiche Person structurée publiée",
                    f"Employeur déclaré : {employer or 'non précisé'}",
                ],
                phone=record.get("telephone", ""),
            )

    for block in soup.select(
        "article, li, [itemtype*='Person'], .team-member, .person, .contact-card"
    ):
        if unrelated_block(block, company):
            continue
        name, role = person_in(block)
        if not name:
            continue
        emails = EMAIL.findall(block.get_text(" ", strip=True))
        for anchor in block.select('a[href^="mailto:"]'):
            emails.extend(EMAIL.findall(unquote(anchor["href"])))
        profile = next(
            (
                urljoin(link_base(page), a["href"])
                for a in block.select("a[href]")
                if "linkedin.com/in/" in a["href"]
            ),
            "",
        )
        phones = phones_in(block)
        phone = phones[0] if len(phones) == 1 else ""
        if emails:
            for email in dict.fromkeys(emails):
                add(name, role, email, profile, phone=phone)
        else:
            add(name, role, profile=profile, phone=phone)

    seen = {c["email"] for c in contacts if c["email"]}
    for anchor in soup.select('a[href^="mailto:"]'):
        for email in EMAIL.findall(unquote(anchor["href"].split("?", 1)[0])):
            if email.lower() in seen:
                continue
            parent = contact_block(anchor)
            if unrelated_block(parent, company, email):
                continue
            name, role = (
                person_in(parent) if parent.name not in ("body", "html", "[document]") else ("", "")
            )
            phones = [] if unrelated_block(parent, company) else phones_in(parent)
            add(
                name,
                role or service_in(parent),
                email,
                phone=phones[0] if len(phones) == 1 else "",
                context=parent.get_text(" ", strip=True),
            )
            seen.add(email.lower())
    for node in soup.find_all(string=EMAIL):
        block = contact_block(node)
        for email in EMAIL.findall(node):
            if unrelated_block(block, company, email):
                continue
            if email.lower() not in seen:
                name, role = person_in(block)
                phones = [] if unrelated_block(block, company) else phones_in(block)
                add(
                    name,
                    role or service_in(block),
                    email=email,
                    phone=phones[0] if len(phones) == 1 else "",
                    context=block.get_text(" ", strip=True),
                )
                seen.add(email.lower())

    phone_seen = {c["phone"] for c in contacts if c["phone"]}
    phone_nodes = list(soup.select('a[href^="tel:"]')) + list(soup.find_all(string=PHONE))
    for node in phone_nodes:
        block = contact_block(node)
        if unrelated_block(block, company):
            continue
        values = (
            [unquote(node["href"])] if getattr(node, "name", None) == "a" else PHONE.findall(node)
        )
        for value in values:
            phone = normalize_phone(value)
            if not phone or phone in phone_seen:
                continue
            name, role = person_in(block)
            add(
                name,
                role or service_in(block) or "Standard",
                phone=phone,
                context=block.get_text(" ", strip=True),
            )
            phone_seen.add(phone)
    return contacts
