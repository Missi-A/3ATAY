"""Chromium sélectif, identifiable, sans compte, clic, formulaire ou résolution de défi."""

import time
from urllib.parse import urlsplit

from .models import utc_now
from .pages import USER_AGENT, FetchError, Page, public_url
from .privacy import redact
from .runtime import CompanyControl

USEFUL = r"""() => [...document.querySelectorAll('a[href]')].some(a => /\/(jobs?|offres?|poste|vacancy|annonces|j)\/[^/?]+/.test(a.href)) || [...document.querySelectorAll('script[type="application/ld+json"]')].some(s => s.textContent.includes('JobPosting')) || /aucune offre|no open positions|no vacancies/i.test(document.body.innerText)"""


class Browser:
    def __init__(self):
        self.manager = self.browser = None

    def start(self):
        if self.browser:
            return
        from playwright.sync_api import sync_playwright

        self.manager = sync_playwright().start()
        try:
            self.browser = self.manager.chromium.launch(headless=True)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.browser:
            self.browser.close()
            self.browser = None
        if self.manager:
            self.manager.stop()
            self.manager = None

    def render(self, web, url):
        previous = web.control
        web.control = CompanyControl(
            previous,
            min(
                web.config.source_seconds,
                previous.remaining()
                if hasattr(previous, "remaining")
                else web.config.source_seconds,
            ),
        )
        try:
            return self._render(web, url)
        except InterruptedError:
            raise
        except Exception as exc:
            previous.check()
            if time.monotonic() >= web.control.deadline:
                raise FetchError(
                    "Chromium : délai de la source atteint ; autres sources conservées."
                ) from None
            raise FetchError(str(exc)) from None
        finally:
            web.control = previous

    def _render(self, web, url):
        web.control.check()
        public_url(url)
        web._allowed(url)
        cache_key = "render:" + url
        if web.cache:
            cached = web.cache.get(cache_key)
            if cached:
                if cached.get("restrictions"):
                    web.log.append(
                        dict(
                            url=redact(url),
                            purpose="browser_blocked_resources",
                            status="partial",
                            error=cached["restrictions"],
                            at=utc_now(),
                        )
                    )
                web.log.append(
                    dict(url=redact(url), purpose="browser", status=200, cached=True, at=utc_now())
                )
                return Page(cached["url"], cached["body"])
        self.start()
        started = time.monotonic()
        remaining = (
            web.control.remaining()
            if hasattr(web.control, "remaining")
            else web.config.source_seconds
        )
        timeout = min(web.config.source_seconds, remaining)
        deadline = time.monotonic() + timeout
        context = self.browser.new_context(
            user_agent=USER_AGENT, service_workers="block", accept_downloads=False
        )
        count = 0
        blocked = []

        def guard(route):
            nonlocal count
            request = route.request
            if request.resource_type in ("image", "media", "font") or any(
                t in request.url
                for t in (
                    "google-analytics",
                    "googletagmanager",
                    "facebook.net",
                    "hotjar",
                    "clarity.ms",
                )
            ):
                route.abort()
                return
            try:
                web.control.check()
                if (
                    time.monotonic() >= deadline
                    or count >= 40
                    or request.method not in ("GET", "HEAD")
                ):
                    raise FetchError("Ressource hors budget ou opération non autorisée.")
                public_url(request.url)
                # shortcut: Chromium resolves again after this guard; require an isolated
                # network/proxy with IP pinning before enabling it for untrusted sites.
                web._allowed(request.url)
                if web.policy:
                    with web.policy.request(
                        urlsplit(request.url).netloc.lower(),
                        web.control,
                        0.05
                        if request.resource_type in ("script", "stylesheet")
                        else web.config.interval,
                    ):
                        pass
                # Playwright dispatches nested routes here; never hold the domain lock across it.
                route.continue_()
                count += 1
            except Exception as exc:
                blocked.append(redact(str(exc)))
                route.abort()

        def response_received(response):
            web.log.append(
                dict(
                    url=redact(response.url),
                    purpose="browser_resource",
                    status=response.status,
                    at=utc_now(),
                )
            )
            if response.status in (401, 403, 429):
                web.suspend(response.url, f"Chromium : HTTP {response.status} ; source suspendue.")

        try:
            context.route("**/*", guard)
            context.route_web_socket("**/*", lambda socket: socket.close())
            context.on("page", lambda page: page.close() if len(context.pages) > 1 else None)
            page = context.new_page()
            page.on("response", response_received)
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=max(1, int(timeout * 1000))
            )
            web.control.check()
            if response and response.status >= 400:
                raise FetchError(f"Chromium HTTP {response.status}")
            remaining_ms = int(max(0.01, deadline - time.monotonic()) * 1000)
            page.wait_for_function(USEFUL, timeout=remaining_ms)
            body = page.content()
            if len(body.encode()) > web.config.max_bytes:
                raise FetchError("DOM rendu trop volumineux.")
            result = Page(page.url, body)
            title = result.soup.title.get_text().lower() if result.soup.title else ""
            if result.soup.select_one(
                "#challenge-form, #challenge-running, #cf-challenge-running"
            ) or any(
                t in title
                for t in ("just a moment", "access denied", "verify you are human", "captcha")
            ):
                web.suspend(url, "Défi anti-robot dans Chromium ; aucun contournement.")
                raise FetchError("Défi anti-robot dans Chromium ; aucun contournement.")
            if blocked:
                web.log.append(
                    dict(
                        url=redact(url),
                        purpose="browser_restrictions",
                        status="partial",
                        error="; ".join(dict.fromkeys(blocked))[:1500],
                        at=utc_now(),
                    )
                )
            web.log.append(
                dict(
                    url=redact(result.url),
                    purpose="browser",
                    status=200,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    resources=count,
                    at=utc_now(),
                )
            )
            if web.cache:
                web.cache.put(
                    cache_key,
                    dict(
                        url=result.url,
                        body=body,
                        restrictions="; ".join(dict.fromkeys(blocked))[:1500],
                    ),
                    min(web.config.cache_seconds, 3600),
                )
            return result
        except InterruptedError:
            raise
        except Exception as exc:
            raise FetchError(f"Chromium : {exc}") from None
        finally:
            if blocked:
                web.log.append(
                    dict(
                        url=redact(url),
                        purpose="browser_blocked_resources",
                        status="partial",
                        error="; ".join(dict.fromkeys(blocked))[:1500],
                        at=utc_now(),
                    )
                )
            context.close()
