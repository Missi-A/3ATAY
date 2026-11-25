"""Dépliage des listes d'annonces que le portail charge par JavaScript (« Tout afficher », « Voir plus », pages
numérotées, défilement infini).

`expand` ne connaît que l'interface minimale d'une page (`hrefs`, `controls`, `click`, `scroll`, `signature`, `total`) :
la logique se teste sans navigateur. `expand_in_browser` branche Chromium (lecture publique : requêtes GET seulement,
aucun formulaire ni compte, seuls les boutons de dépliage ou de pagination sont cliqués).
"""
from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

MAX_STEPS = 60
MAX_SECONDS = 90
MAX_BROWSER_REQUESTS = 200

EXPAND = re.compile(r"^(?:tout afficher|afficher tout|voir tout(?:es)?(?: les (?:offres|annonces|postes))?|"
                    r"(?:voir|afficher|charger|consulter) (?:plus|davantage)(?: d.(?:offres|annonces|postes))?|"
                    r"plus d.(?:offres|annonces)|show (?:all|more)|view (?:all|more)|see (?:all|more)|load more)\b", re.I)
NEXT = re.compile(r"suivant|next|^[›»>]+$", re.I)
INFINITE = re.compile(r"infinite[-_ ]?scroll|load[-_ ]?more|loadmore", re.I)
COUNT_TEXT = re.compile(r"\b(\d{1,5})\s+(?:offres?|annonces?|postes?)\s+(?:d.emploi\s+)?"
                        r"(?:disponibles?|ouverte?s?|à pourvoir|trouvée?s?)\b|\b(\d{1,5})\s+résultats?\b", re.I)


@dataclass
class Control:
    kind: str  # « expand », « number », « next », ou « current » (numéro de la page affichée, jamais cliqué)
    label: str
    key: str  # identifiant opaque transmis à `click`


@dataclass
class Expansion:
    hrefs: list[str] = field(default_factory=list)
    complete: bool = True
    reason: str = ""  # cause de l'incomplétude, lisible dans le bilan du portail
    steps: int = 0
    total: int | None = None  # total annoncé par le portail, s'il le publie


def link_key(href: str) -> str:
    return href.split("#")[0].rstrip("/")


def announced_total(body: str) -> int | None:
    """Total d'annonces publié par la page : état JSON (`total` à côté de `pageCount`/`limit`/`items`, y compris le
    format compact de Nuxt) ou phrase visible « 25 offres disponibles ». None si rien de fiable."""
    soup = BeautifulSoup(body or "", "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in list(nodes):
            if isinstance(node, dict) and "total" in node and {"pageCount", "limit", "items"} & node.keys():
                total = node["total"]
                if script.get("id") == "__NUXT_DATA__" and isinstance(total, int) and isinstance(data, list):
                    total = data[total] if 0 <= total < len(data) else None  # valeurs compactes : indices dans la liste
                if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                    return total
    for script in soup(["script", "style"]):
        script.decompose()
    found = {int(m.group(1) or m.group(2)) for m in COUNT_TEXT.finditer(soup.get_text(" ", strip=True))}
    return found.pop() if len(found) == 1 else None


def needs_expansion(body: str, links_found: int = 0) -> str:
    """Motif pour lequel la page laisse supposer des annonces non affichées ; vide si rien ne l'indique."""
    soup = BeautifulSoup(body or "", "html.parser")
    for tag in soup.find_all(["button", "a", "input"]):
        label = " ".join((tag.get_text(" ", strip=True) or tag.get("value") or "").split())
        if EXPAND.match(label) and (tag.name != "a" or not (tag.get("href") or "").strip("#")):
            return f"bouton « {label[:40]} »"
    for nav in soup.find_all(attrs={"aria-label": re.compile("pagination", re.I)}) + soup.find_all(
            class_=re.compile("pagination|pager", re.I)):
        if nav.find("button") and not nav.find("a", href=re.compile(r"[^#\s]")):
            return "pagination par boutons"
    if INFINITE.search(str(soup)):
        return "chargement progressif"
    total = announced_total(body)
    if total is not None and total > links_found:
        return f"{total} annonces annoncées, {links_found} liens visibles"
    return ""


def choose(controls: list[Control], used: set[str], visited: set[str]) -> Control | None:
    for control in controls:
        if control.kind == "expand" and control.label.lower() not in used:
            return control
    numbers = sorted((c for c in controls if c.kind == "number" and c.label not in visited),
                     key=lambda c: int(c.label))
    if numbers:
        return numbers[0]
    return next((c for c in controls if c.kind == "next"), None)


def expand(page, is_job, *, max_steps: int = MAX_STEPS, max_seconds: float = MAX_SECONDS,
           clock=time.monotonic) -> Expansion:
    """Parcourt toute la liste. Complète seulement si plus aucun contrôle ni défilement ne révèle d'annonce ; sinon
    (clic impossible, même page renvoyée en boucle, limite atteinte, total annoncé non atteint) l'annonce est
    « incomplète » avec son motif. Les annonces déjà lues sont toujours renvoyées."""
    seen: dict[str, str] = {}

    def gather() -> int:
        before = len(seen)
        for href in page.hrefs():
            if is_job(href):
                seen.setdefault(link_key(href), href)
        return len(seen) - before

    def result(complete: bool, reason: str = "", steps: int = 0) -> Expansion:
        total = page.total()
        if complete and total is not None and len(seen) < total:
            complete, reason = False, f"{len(seen)} annonces lues sur {total} annoncées par le portail"
        if not complete and total is not None and len(seen) == total and reason.startswith("même page"):
            complete, reason = True, ""  # le total annoncé est atteint : la répétition n'a rien caché
        return Expansion(list(seen.values()), complete, reason, steps, total)

    try:
        return _run(page, gather, result, max_steps, max_seconds, clock)
    except Exception as error:  # noqa: BLE001 - page fermée ou bloquée en cours de route : on garde ce qui est lu
        return Expansion(list(seen.values()), False, f"lecture interrompue ({type(error).__name__})")


def _run(page, gather, result, max_steps, max_seconds, clock) -> Expansion:
    gather()
    deadline = clock() + max_seconds
    used, visited, states = set(), set(), {page.signature()}
    steps = 0
    while True:
        if steps >= max_steps or clock() >= deadline:
            return result(False, "limite d'étapes ou de durée atteinte avant la fin de la liste", steps)
        controls = page.controls()
        visited.update(c.label for c in controls if c.kind == "current")  # la page affichée est déjà lue
        control = choose(controls, used, visited)
        steps += 1
        if control is None:  # plus de bouton : le défilement charge peut-être la suite
            before = page.signature()
            try:
                page.scroll()
            except Exception as error:  # noqa: BLE001 - page fermée ou bloquée : lecture partielle
                return result(False, f"défilement impossible ({type(error).__name__})", steps)
            if gather() == 0 and page.signature() == before:
                if any(c.kind == "expand" and c.label.lower() in used for c in page.controls()):
                    return result(False, "même page après « Voir plus » : fin de liste non prouvée", steps)
                return result(True, steps=steps)
            continue
        try:
            page.click(control)
        except Exception as error:  # noqa: BLE001 - bouton disparu, page bloquée ou navigation refusée
            return result(False, f"clic impossible sur « {control.label[:30]} » ({type(error).__name__})", steps)
        new = gather()
        signature = page.signature()
        if control.kind == "expand":
            if not new and signature in states:  # le bouton ne change plus rien : inutile de le reprendre
                used.add(control.label.lower())
        else:
            if control.kind == "number":
                visited.add(control.label)
            if not new and signature in states:
                return result(False, "même page renvoyée en boucle : fin de liste non prouvée", steps)
        states.add(signature)


# -------------------------------------------------------------------------------------------- Chromium
_CONTROLS = r"""() => {
  document.querySelectorAll('[data-flemme-ctl]').forEach(e => e.removeAttribute('data-flemme-ctl'));
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                       return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const out = [];
  document.querySelectorAll('button,[role=button],input[type=button],a:not([href]),a[href^="#"],a[href^="javascript"]')
    .forEach(e => {
      if (!shown(e)) return;
      const cls = String(e.className || '');
      const label = (e.innerText || e.value || '').trim().replace(/\s+/g, ' ').slice(0, 80);
      e.setAttribute('data-flemme-ctl', String(out.length));
      out.push({key: String(out.length), label, aria: (e.getAttribute('aria-label') || '').slice(0, 80),
                disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true',
                active: /active|current|selected/.test(cls) || !!e.getAttribute('aria-current'),
                nav: !!e.closest('nav,[class*=pagination],[class*=pager],[aria-label*=agination]')});
    });
  return out;
}"""
_HREFS = "() => [...document.querySelectorAll('a[href]')].map(a => a.href)"


class ChromiumPage:
    """Page Chromium réelle exposée à `expand`."""

    def __init__(self, page, settle_seconds: float = 8.0):
        self.page, self.settle_seconds = page, settle_seconds

    def hrefs(self):
        return self.page.evaluate(_HREFS)

    def total(self):
        return announced_total(self.page.content())

    def signature(self):
        return (tuple(sorted({link_key(h) for h in self.hrefs()})), self.page.evaluate("document.body.scrollHeight"))

    def controls(self) -> list[Control]:
        found = []
        for item in self.page.evaluate(_CONTROLS):
            label, aria = item["label"], item["aria"]
            if item["disabled"]:
                continue
            if EXPAND.match(label) or EXPAND.match(aria):
                found.append(Control("expand", label or aria, item["key"]))
            elif item["nav"] and label.isdigit():
                found.append(Control("current" if item["active"] else "number", label, item["key"]))
            elif item["nav"] and (NEXT.search(aria) or NEXT.search(label)):
                found.append(Control("next", label or aria, item["key"]))
        return found

    def click(self, control: Control):
        target = self.page.locator(f'[data-flemme-ctl="{control.key}"]').first
        try:
            target.click(timeout=2500)
        except Exception:  # noqa: BLE001 - bandeau de consentement ou calque qui capte le clic (ex. Axeptio) : clic du bouton lui-même
            target.evaluate("e => e.click()")
        self.settle()

    def scroll(self):
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.settle()

    def settle(self):
        """Attend que le réseau se calme puis que le nombre de liens et la hauteur cessent de changer."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001 - un site qui ne se calme jamais : on se fie à la stabilité du DOM
            pass
        end, last = time.monotonic() + self.settle_seconds, None
        while time.monotonic() < end:
            state = self.signature()
            if state == last:
                return
            last = state
            self.page.wait_for_timeout(400)


class BrowserUnavailable(Exception):
    """Chromium absent, page en échec ou hors du périmètre : le motif est celui du bilan du portail."""


def _launch_chromium(chromium):
    """Utilise un navigateur installé si le Chromium fourni avec Playwright manque."""
    from playwright.sync_api import Error

    last_error = None
    for channel in (None, "chrome", "msedge"):
        try:
            return chromium.launch(headless=True, channel=channel)
        except Error as error:
            last_error = error
    raise BrowserUnavailable("Chromium, Chrome et Edge indisponibles pour la lecture automatique") from last_error


BROWSER_SLOTS = threading.BoundedSemaphore(2)  # Chromium coûte cher : deux navigateurs au plus, même avec 6 portails lus à la fois


@contextmanager
def browser_page(*args, **kwargs):
    """Voir `_open_browser_page` ; attend un emplacement libre si deux navigateurs sont déjà ouverts."""
    with BROWSER_SLOTS, _open_browser_page(*args, **kwargs) as page:
        yield page


@contextmanager
def _open_browser_page(url: str, allow, take=lambda: True, post_ok=lambda target: False, on_response=None):
    """Page Chromium ouverte sur `url`. `allow(url)` : la page doit rester dans le périmètre du portail ; `take()` :
    décompte du budget de requêtes du relevé (faux : plus de requête) ; `post_ok(url)` : seules les requêtes POST que
    la page émet elle-même pour s'initialiser sont admises, jamais les autres (aucun formulaire). Lève
    `BrowserUnavailable` avec le motif de l'échec."""
    from ..contact_finder_engine.engine.pages import USER_AGENT, public_url
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserUnavailable("navigateur indisponible (Playwright non installé)") from None
    count = 0

    def guard(route):
        nonlocal count
        request = route.request
        try:
            if request.resource_type in ("image", "media", "font"):
                return route.abort()
            if request.method not in ("GET", "HEAD") and not (request.method == "POST" and post_ok(request.url)):
                return route.abort()
            public_url(request.url)  # shortcut: Chromium résout de nouveau le nom ; réseau isolé exigé pour des sites non fiables
            count += 1
            if count > MAX_BROWSER_REQUESTS or not take():
                return route.abort()
            if request.is_navigation_request() and request.frame.parent_frame is None and not allow(request.url):
                return route.abort()  # la page ne doit pas quitter le périmètre du portail
            return route.continue_()
        except Exception:  # noqa: BLE001 - toute requête douteuse est refusée
            return route.abort()

    async def close_web_socket(socket):
        # Le rappel s'exécute déjà dans la boucle Playwright. La méthode synchrone
        # attendrait une réponse que cette même boucle doit traiter (Playwright 1.64.1).
        await socket._impl_obj.close()

    try:
        runner = sync_playwright().start()
    except Exception as error:  # noqa: BLE001
        raise BrowserUnavailable(f"navigateur : {type(error).__name__}: {str(error)[:80]}") from None
    try:
        browser = _launch_chromium(runner.chromium)
        try:
            context = browser.new_context(user_agent=USER_AGENT, service_workers="block", accept_downloads=False)
            context.route("**/*", guard)
            context.route_web_socket("**/*", close_web_socket)
            page = context.new_page()
            if on_response:
                page.on("response", on_response)
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status >= 400:
                raise BrowserUnavailable(f"page de liste en échec (HTTP {response.status if response else 0})")
            if not allow(page.url):
                raise BrowserUnavailable(f"redirection hors du portail ({urlsplit(page.url).netloc})")
            yield page
            if not allow(page.url):
                raise BrowserUnavailable("la page a quitté le périmètre du portail")
        finally:
            browser.close()
    except BrowserUnavailable:
        raise
    except Exception as error:  # noqa: BLE001 - Chromium absent, délai, page fermée…
        raise BrowserUnavailable(f"navigateur : {type(error).__name__}: {str(error)[:80]}") from None
    finally:
        runner.stop()


def expand_in_browser(url: str, is_job, allow, take=lambda: True) -> Expansion:
    """Ouvre `url` dans Chromium et déplie la liste. Toute panne du navigateur donne une lecture incomplète avec son
    motif, jamais une exception."""
    try:
        with browser_page(url, allow, take) as page:
            driver = ChromiumPage(page)
            driver.settle()
            return expand(driver, is_job)
    except BrowserUnavailable as error:
        return Expansion([], False, str(error))
