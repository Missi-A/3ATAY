"""Primitives HTTP sans accès réseau direct : page lue, erreur, URL canonique, hôte."""

from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import web_url
from .network import public_addresses
from .privacy import redact

USER_AGENT = "ContactFinder/1.0 (public professional research; robots respected)"


def canonical(url):
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def public_url(url):
    if not web_url(url):
        raise ValueError("URL publique HTTP(S) sans identifiants et port standard requise.")
    parts = urlsplit(url)
    public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))


class FetchError(Exception):
    def __init__(self, message):
        super().__init__(redact(str(message)))


@dataclass
class Page:
    url: str
    body: str
    content_type: str = "text/html"
    headers: dict = field(default_factory=dict)

    @cached_property
    def soup(self):
        return BeautifulSoup(self.body, "html.parser")

    @cached_property
    def base_url(self):
        """Base des liens relatifs : ``<base href>`` publiée (HTML seulement), sinon l'URL lue."""
        if "html" not in self.content_type:
            return self.url
        tag = self.soup.find("base", href=True)
        try:
            url = urljoin(self.url, tag["href"].strip()) if tag else self.url
            return url if web_url(url) else self.url
        except ValueError:  # <base> malformé (https://[broken) : page lue comme base
            return self.url


def link_base(page):
    return page.base_url if isinstance(page, Page) else page.url
