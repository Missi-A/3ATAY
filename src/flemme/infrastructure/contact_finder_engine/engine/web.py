"""HTTP public borné : robots, quotas, redirections validées et refus des réseaux privés."""

import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .models import utc_now
from .network import PublicAdapter
from .pages import USER_AGENT, FetchError, Page, canonical, host, public_url  # noqa: F401
from .privacy import redact
from .runtime import CompanyControl, DeadlineExceeded, StageExceeded


def robots_allowed(parser, url):
    if not parser.can_fetch("ContactFinder", url):
        return False
    entries = [entry for entry in parser.entries if entry.applies_to("ContactFinder")]
    if not entries and parser.default_entry:
        entries = [parser.default_entry]
    target = unquote(urlsplit(url).path or "/")
    if urlsplit(url).query:
        target += "?" + unquote(urlsplit(url).query)
    for entry in entries:
        for rule in entry.rulelines:
            # shortcut: Python <3.15 lacks wildcard rules; conservatively refuse matching exclusions, even with an Allow exception.
            if rule.allowance or hasattr(rule, "fullmatch"):
                continue
            path = unquote(rule.path)
            if "*" in path or path.endswith("$"):
                pattern = re.escape(path.rstrip("$")).replace(r"\*", ".*")
                if re.match(pattern + ("$" if path.endswith("$") else ""), target):
                    return False
    return True


class Web:
    def __init__(self, config, control, log):
        self.config, self.control, self.log = config, control, log
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("http://", PublicAdapter())
        self.session.mount("https://", PublicAdapter())
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json,application/rss+xml,text/plain",
            }
        )
        self.robots = {}
        self.last = {}
        self.delays = {}
        self.policy = None
        self.cache = None
        self.browser_runtime = None

    def close(self):
        if self.browser_runtime:
            self.browser_runtime.close()
        self.session.close()

    def render(self, url):
        from .browser import Browser

        if self.browser_runtime is None:
            self.browser_runtime = Browser()
        return self.browser_runtime.render(self, url)

    def fetch_api(self, url, headers=None):
        from .connectors import DOCUMENTATION, api_provider

        provider = api_provider(url)
        if not provider:
            raise FetchError("Endpoint hors des API publiques GET documentées.")
        if headers and provider != "lba":
            raise FetchError("Identifiant réservé à l'API LBA.")
        previous = self.control
        self.control = CompanyControl(
            previous,
            min(
                self.config.source_seconds,
                previous.remaining()
                if hasattr(previous, "remaining")
                else self.config.source_seconds,
            ),
        )
        self.api_headers = {}
        try:
            self._allowed(url)
            self.api_headers = headers or {}
            status, target, body, content_type = self._request(url, "connector:" + provider)
            if self.log:
                self.log[-1]["documentation"] = DOCUMENTATION[provider]
            if target or status >= 400:
                raise FetchError(
                    f"API {provider} : HTTP {status}, aucune redirection authentifiée suivie."
                )
            return Page(url, body, content_type, dict(self.response_headers))
        except (DeadlineExceeded, FetchError) as exc:
            previous.check()
            if time.monotonic() >= self.control.deadline:
                raise FetchError(
                    "Délai de la source atteint ; autres sources conservées."
                ) from None
            raise FetchError(redact(str(exc), tuple((headers or {}).values()))) from None
        except (InterruptedError, StageExceeded):
            raise
        except Exception as exc:
            raise FetchError(redact(str(exc), tuple((headers or {}).values()))) from None
        finally:
            self.api_headers = {}
            self.control = previous

    def _request(self, url, purpose):
        self.control.check()
        origin = urlsplit(url).netloc.lower()
        cache = self.cache if not getattr(self, "api_headers", {}) and redact(url) == url else None
        if cache:
            cached = self.cache.get("http:" + url)
            if cached:
                self.response_headers = cached.get("headers", {})
                self.log.append(
                    dict(
                        url=redact(url),
                        purpose=purpose,
                        status=cached["response"][0],
                        cached=True,
                        at=utc_now(),
                        fetched_at=cached["at"],
                    )
                )
                return tuple(cached["response"])
            blocked = self.cache.get("blocked:" + origin)
            if blocked:
                raise FetchError(blocked["reason"])
        if self.policy:
            with self.policy.request(
                origin, self.control, max(self.config.interval, self.delays.get(origin, 0))
            ):
                if cache:
                    cached = cache.get("http:" + url)
                    if cached:
                        self.response_headers = cached.get("headers", {})
                        self.log.append(
                            dict(
                                url=redact(url),
                                purpose=purpose,
                                status=cached["response"][0],
                                cached=True,
                                at=utc_now(),
                            )
                        )
                        return tuple(cached["response"])
                result = self._request_http(url, purpose)
                return self._remember(url, result, cache)
        else:
            result = self._request_http(url, purpose)
        return self._remember(url, result, cache)

    def _remember(self, url, result, cache):
        if cache:
            ttl = (
                self.config.cache_seconds
                if result[0] < 400
                else 3600
                if result[0] in (401, 403)
                else 900
            )
            if ttl:
                response = result
                if (
                    urlsplit(url).hostname == "recherche-entreprises.api.gouv.fr"
                    and result[0] == 200
                ):
                    import json

                    payload = json.loads(result[2])
                    for record in payload.get("results", []):
                        record.pop("finances", None)
                        for person in record.get("dirigeants", []):
                            for field in ("date_de_naissance", "date_naissance", "nationalite"):
                                person.pop(field, None)
                    response = (
                        result[0],
                        result[1],
                        json.dumps(payload, ensure_ascii=False),
                        result[3],
                    )
                cache.put(
                    "http:" + url,
                    dict(response=response, headers=self.response_headers, at=utc_now()),
                    ttl,
                )
        if result[0] in (401, 403):
            self.suspend(url, f"HTTP {result[0]} ; source suspendue pendant une heure.")
        return result

    def suspend(self, url, reason, seconds=3600):
        origin = urlsplit(url).netloc.lower()
        if self.policy:
            self.policy.block(origin, seconds)
        if self.cache and not getattr(self, "api_headers", {}):
            self.cache.put("blocked:" + origin, dict(reason=redact(reason)), seconds)

    def _request_http(self, url, purpose):
        self.control.check()
        public_url(url)
        origin = urlsplit(url).netloc.lower()
        interval = max(self.config.interval, self.delays.get(origin, 0))
        self.control.wait(max(0, self.last.get(origin, 0) + interval - time.monotonic()))
        self.last[origin] = time.monotonic()
        try:
            with self.session.get(
                url,
                timeout=min(self.config.timeout, self.control.remaining())
                if hasattr(self.control, "remaining")
                else self.config.timeout,
                stream=True,
                allow_redirects=False,
                headers=getattr(self, "api_headers", None),
            ) as response:
                status = response.status_code
                self.response_headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in ("x-wp-totalpages",)
                }
                entry = dict(url=redact(url), purpose=purpose, status=status, at=utc_now())
                self.log.append(entry)
                if status in (301, 302, 303, 307, 308):
                    target = response.headers.get("Location")
                    if not target:
                        raise FetchError("Redirection sans destination.")
                    return status, urljoin(url, target), "", ""
                if status == 429 or status == 503:
                    retry = response.headers.get("Retry-After", "3")
                    try:
                        wait = max(1, float(retry))
                    except ValueError:
                        try:
                            wait = max(1, parsedate_to_datetime(retry).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            wait = 60
                    self.last[origin] = time.monotonic() + wait
                    self.suspend(url, f"HTTP {status} ; délai Retry-After actif.", wait)
                    raise FetchError(
                        f"HTTP {status} ; nouvelle requête après {wait:.0f} s minimum."
                    )
                if status >= 400:
                    return status, "", "", ""
                length = 0
                chunks = []
                started = time.monotonic()
                for chunk in response.iter_content(64 * 1024):
                    self.control.check()
                    if time.monotonic() - started > self.config.timeout:
                        raise FetchError("Délai total de lecture dépassé.")
                    length += len(chunk)
                    if length > self.config.max_bytes:
                        raise FetchError("Réponse trop volumineuse.")
                    chunks.append(chunk)
                charset = response.encoding
                if not charset or charset.lower() == "iso-8859-1":
                    charset = "utf-8"
                return (
                    status,
                    "",
                    b"".join(chunks).decode(charset, errors="replace"),
                    response.headers.get("Content-Type", ""),
                )
        except requests.RequestException as exc:
            message = redact(str(exc), tuple(getattr(self, "api_headers", {}).values()))
            self.log.append(
                dict(url=redact(url), purpose=purpose, status="error", error=message, at=utc_now())
            )
            raise FetchError(message) from None

    def _allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            self.robots[origin] = None
            robots_url = origin + "/robots.txt"
            for _ in range(6):
                try:
                    status, target, body, _ = self._request(robots_url, "robots")
                except (FetchError, DeadlineExceeded):
                    # Panne passagère : robots.txt sera relu plus tard, jamais tenu pour un refus.
                    self.robots.pop(origin, None)
                    raise
                if target:
                    robots_url = target
                    continue
                if status in (404, 410):
                    body = "User-agent: *\nAllow: /"
                elif status != 200:
                    raise FetchError(f"robots.txt inaccessible : HTTP {status}.")
                if "<html" in body.lower() or "<!doctype html" in body.lower():
                    raise FetchError("robots.txt renvoie du HTML ; autorisation non vérifiable.")
                parser = RobotFileParser()
                parser.parse(body.splitlines())
                self.robots[origin] = parser
                delay = parser.crawl_delay("ContactFinder") or parser.crawl_delay("*") or 0
                rate = parser.request_rate("ContactFinder") or parser.request_rate("*")
                if rate:
                    delay = max(delay, rate.seconds / rate.requests)
                self.delays[parts.netloc.lower()] = delay
                if self.policy:
                    self.policy.delay[parts.netloc.lower()] = delay
                break
        parser = self.robots[origin]
        if parser is None or not robots_allowed(parser, url):
            raise FetchError("Exploration refusée ou robots.txt non vérifiable.")

    def crawl_delay(self, url):
        """Délai entre deux requêtes imposé par robots.txt (Crawl-delay, Request-rate) pour l'hôte
        de ``url`` ; robots.txt lu comme pour toute requête. None s'il est illisible."""
        try:
            self._allowed(url)
        except (FetchError, DeadlineExceeded):
            pass  # exploration refusée ou robots.txt illisible : le délai lu, s'il existe, vaut
        return self.delays.get(urlsplit(url).netloc.lower())

    def fetch(self, url, purpose="page"):
        previous = self.control
        self.control = CompanyControl(
            previous,
            min(
                self.config.source_seconds,
                previous.remaining()
                if hasattr(previous, "remaining")
                else self.config.source_seconds,
            ),
        )
        try:
            return self._fetch(url, purpose)
        except (DeadlineExceeded, FetchError):
            previous.check()
            if time.monotonic() >= self.control.deadline:
                raise FetchError(
                    "Délai de la source atteint ; autres sources conservées."
                ) from None
            raise
        finally:
            self.control = previous

    def _fetch(self, url, purpose="page"):
        url = canonical(url)
        try:
            for _ in range(6):
                self.control.check()
                public_url(url)
                self._allowed(url)
                for attempt in range(self.config.attempts):
                    try:
                        status, target, body, content_type = self._request(url, purpose)
                        if status >= 500:
                            raise FetchError(f"HTTP {status}")
                        break
                    except FetchError:
                        if attempt + 1 == self.config.attempts:
                            raise
                        origin = urlsplit(url).netloc.lower()
                        delay = max(1, self.last.get(origin, 0) - time.monotonic())
                        if delay > 30:
                            raise
                        self.control.wait(delay)
                if target:
                    url = canonical(target)
                    continue
                if status >= 400:
                    raise FetchError(f"HTTP {status}")
                if not any(
                    t in content_type.lower() for t in ("html", "json", "xml", "text/plain")
                ):
                    raise FetchError(f"Format non exploré : {content_type}")
                soup = (
                    BeautifulSoup(body, "html.parser") if "html" in content_type.lower() else None
                )
                title = soup.title.get_text(" ", strip=True).lower() if soup and soup.title else ""
                if bool(
                    soup
                    and soup.select_one(
                        "#challenge-running, #cf-challenge-running, #challenge-form"
                    )
                ) or any(
                    marker in title
                    for marker in (
                        "just a moment",
                        "access denied",
                        "verify you are human",
                        "attention required",
                        "captcha",
                    )
                ):
                    self.suspend(
                        url, "Défi anti-robot ; source suspendue sans recours au navigateur."
                    )
                    if self.cache:
                        self.cache.put("http:" + url, {}, 0)
                    raise FetchError("Protection anti-robot ; exploration arrêtée.")
                return Page(url, body, content_type, dict(getattr(self, "response_headers", {})))
            raise FetchError("Trop de redirections.")
        except (ValueError, OSError) as exc:
            raise FetchError(str(exc)) from None
