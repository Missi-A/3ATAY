"""Repère l'invitation à candidater sur une page officielle de recrutement."""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ...shared.text_normalization import citation_key
from ..contact_finder_engine.engine.models import web_url
from .inline_offers import NOISE_TAGS, SPONTANEOUS, base_url, host_of, same_site

DIRECT_CHANNEL = "spontaneous_application"
DIRECT_ONLY = "candidature spontanée"
EMPTY_PORTAL = "aucune offre publiée"
EMPTY_LISTING = re.compile(
    r"(?:(?:nos |les )?(?:aucune|0) offres?(?: d'emploi)?(?: (?:disponibles?|actives?|ouvertes?|publiees?|a pourvoir|en cours))?"
    r"(?: (?:pour le moment|pour l'instant|actuellement|en ce moment))?"
    r"|no (?:active |current |open )?(?:job offers|openings|vacancies)(?: at (?:the moment|this time))?)[.!]?"
)
INVITATION = re.compile(
    r"(?:recherchons|recrutons) (?:en permanence|continuellement)|en constante recherche|"
    r"liste de postes ouverts.{0,35}vous n'en trouverez pas|"
    r"aucun(?:e)? (?:poste|offre)(?:\s|\b)|"
    r"envoyez[- ]nous (?:votre |un )?(?:cv|candidature)|"
    r"always (?:hiring|looking for)|no (?:current |open )?(?:vacancies|positions|job (?:listings|openings|descriptions))\b"
)
REFUSAL = re.compile(
    r"(?:n[' ]acceptons (?:pas|aucune)|ne traitons pas|n[' ]etudions pas|pas de|no|do not accept)\b[^.!?]{0,50}$"
)
BY_MAIL = re.compile(r"envoyez|adressez|transmettez|ecrivez|(?:par|un) (?:e-?)?mail|contactez|send|email|write to us")
SEND_CV = re.compile(r"\b(?:envoyez|adressez|transmettez)(?:[- ]nous)? (?:votre |un )?cv\b")
CONTACT_TO_APPLY = re.compile(r"\b(?:contactez-nous|nous contacter) pour postuler\b|\bcontact us to apply\b")
APPLY_LINK = re.compile(r"^(?:postuler|je postule|deposer (?:mon|une) candidature|envoyer (?:mon|son|un) cv|apply(?: now)?)$")
APPLY_PATH = re.compile(r"(?:/|[-_])(?:contact|postul\w*|candidatur\w*|apply|application)(?:/|[-_]|$)")
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I)
UNRELATED = re.compile(r"^(?:privacy|dpo|rgpd|presse|press|support|sales|commercial|newsletter|webmaster)(?:[._+-]|@)")


def _text(node) -> str:
    return citation_key(node.get_text(" ", strip=True))


def _emails(text):
    return {address.lower() for address in EMAIL.findall(text) if not UNRELATED.match(address.lower())}


def _visible_content(body: str):
    soup = BeautifulSoup(body, "html.parser")
    content = soup.select_one("main, [role=main], article") or soup.body or soup
    for node in content.find_all((*NOISE_TAGS, "template")):
        node.decompose()
    for node in content.select('[hidden], [aria-hidden="true"], [style]'):
        if not node.attrs or not (
            node.has_attr("hidden") or node.get("aria-hidden") == "true"
            or re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", node.get("style", ""), re.I)
        ):
            continue
        node.decompose()
    return content


def _invitations(text):
    mentions = list(SPONTANEOUS.finditer(text))
    return mentions, [m for m in mentions if not REFUSAL.search(text[max(0, m.start() - 65):m.start()])]


def empty_listing_state(body: str) -> str:
    """État établi par une mention visible de liste vide, ou chaîne vide sans preuve."""
    content = _visible_content(body)
    if not any(EMPTY_LISTING.fullmatch(_text(node)) for node in (
        content, *content.select("p, div, h1, h2, h3, span, li, [role=status]"),
    )):
        return ""
    mentions, accepted = _invitations(_text(content))
    return "complet" if mentions and not accepted else EMPTY_PORTAL


def extract_spontaneous_application(body: str, page_url: str, *, listing: bool) -> dict:
    """Renvoie la page et un canal explicite, ou seulement la page si le contact reste à vérifier."""
    content = _visible_content(body)
    text = _text(content)
    mentions, accepted = _invitations(text)
    if not accepted and (mentions or listing or not (
            INVITATION.search(text) or SEND_CV.search(text) or CONTACT_TO_APPLY.search(text))):
        return {}

    # Un mail générique fait foi quand la page le désigne dans une consigne de candidature.
    emails = set()
    for mention in accepted:
        vicinity = text[max(0, mention.start() - 70):mention.end() + 190]
        if BY_MAIL.search(vicinity):
            emails.update(_emails(vicinity))
    if not listing:
        for instruction in SEND_CV.finditer(text):
            emails.update(_emails(text[instruction.end():instruction.end() + 85]))

    forms = set()
    for anchor in content.find_all("a", href=True):
        label = _text(anchor)
        context = _text(anchor.parent)[:350]
        contact_to_apply = not listing and bool(CONTACT_TO_APPLY.fullmatch(label))
        apply = bool(APPLY_LINK.fullmatch(label) or SPONTANEOUS.search(label) or contact_to_apply)
        contact = not listing and label.startswith(("contactez-nous", "contact us", "nous contacter"))
        target = anchor["href"].strip()
        if target.lower().startswith("mailto:"):
            if apply or contact or (SPONTANEOUS.search(context) and BY_MAIL.search(context)):
                emails.update(_emails(unquote(target[7:].partition("?")[0])))
            continue
        url = urljoin(base_url(body, page_url), target)
        own_entry = bool(SPONTANEOUS.search(label) and urlsplit(url).query)  # entrée « Candidature spontanée » d'une liste
        if (apply and target and not target.startswith("#") and web_url(url)
                and same_site(host_of(url), host_of(page_url))
                and (contact_to_apply or own_entry or APPLY_PATH.search(urlsplit(url).path)
                     or SPONTANEOUS.search(urlsplit(url).path))):
            forms.add(url)
    if not forms and any(form.select_one('input[type="file"]') and (
            SPONTANEOUS.search(_text(form)) or (not listing and re.search(r"\b(?:cv|candidat\w*)\b", _text(form))))
            for form in content.find_all("form")):
        forms.add(page_url)

    if len(emails) == 1:
        return {"page": page_url, "target": "mailto:" + next(iter(emails))}
    if len(forms) == 1:
        return {"page": page_url, "target": next(iter(forms))}
    return {"page": page_url, "target": ""} if accepted or emails or forms else {}
