"""Requêtes bornées ; aucune redirection de jeton vers un autre domaine."""

import json
import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ConnectorError(ValueError):
    def __init__(self, message, *, status=None, reason="", reasons=(), retryable=False):
        super().__init__(message)
        self.status, self.reason = status, reason
        self.reasons, self.retryable = tuple(reasons), retryable


def wait_until(until, cancel):
    while time.monotonic() < until:
        if cancel():
            raise ConnectorError("Opération annulée.")
        time.sleep(min(0.1, max(0, until - time.monotonic())))


class RateLimiter:
    def __init__(self, interval=0.25):
        self.interval, self.next_request = interval, 0

    def wait(self, cancel):
        wait_until(self.next_request, cancel)
        self.next_request = time.monotonic() + self.interval


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


GOOGLE_REASONS = {
    "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
    "quotaExceeded", "backendError", "internalError", "authError", "forbidden",
    "insufficientPermissions", "notFound", "invalidArgument", "badRequest",
}


def error_details(error):
    api_message, reasons = "", set()
    try:
        details = json.loads(error.read(16384) or b"{}")
        if isinstance(details, dict) and isinstance(details.get("message"), str):
            api_message = details["message"][:1000]
        for item in details.get("error", {}).get("errors", []):
            reason = item.get("reason")
            reasons.add(reason if reason in GOOGLE_REASONS else "unknown")
    except (ValueError, AttributeError, TypeError, OSError):
        pass
    return api_message, tuple(sorted(reasons))


def log_google_error(status, reasons, operation, attempt, delay):
    logging.getLogger(__name__).warning(
        "Google HTTP=%s reasons=%s operation=%s attempt=%s wait=%.3fs",
        status, ",".join(reasons) or "unspecified", operation.removesuffix(".full"), attempt, delay,
    )


def retry_delay(attempt, headers):
    delay = 2**attempt + random.uniform(0, 1)
    value = (headers or {}).get("Retry-After", "0")
    try:
        retry_after = float(value)
    except (ValueError, TypeError):
        try:
            retry_after = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            retry_after = 0
    return min(64, max(delay, retry_after))


def get_json(url, headers=None, cancel=lambda: False, *, quota=None):
    if quota is not None:
        from .gmail_quota import operation_for

        operation = operation_for(url)
    else:
        operation = "api.read"
    for attempt in range(3):
        if cancel():
            raise ConnectorError("Opération annulée.")
        if quota is not None:
            quota.wait(operation, cancel)
        try:
            request = Request(url, headers={"Accept": "application/json", **(headers or {})})
            with build_opener(NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(10 * 1024 * 1024 + 1)
                if len(raw) > 10 * 1024 * 1024:
                    raise ConnectorError("Réponse distante trop volumineuse.")
                return json.loads(raw)
        except HTTPError as error:
            api_message, reasons = error_details(error)
            limited = error.code == 429 or (
                error.code == 403
                and bool(
                    set(reasons)
                    & {
                        "rateLimitExceeded",
                        "userRateLimitExceeded",
                        "dailyLimitExceeded",
                    }
                )
            )
            daily = "dailyLimitExceeded" in reasons
            temporary = (limited or 500 <= error.code < 600) and not daily
            delay = retry_delay(attempt, error.headers) if temporary and attempt < 2 else 0
            log_google_error(error.code, reasons, operation, attempt + 1, delay)
            if daily and quota is not None:
                quota.block_today()
            if temporary and attempt < 2:
                wait_until(time.monotonic() + delay, cancel)
                continue
            if limited:
                raise ConnectorError(
                    "Quota du service temporairement atteint. Réessayez plus tard ; la connexion est conservée."
                    if not daily
                    else "Quota quotidien du service atteint. Réessayez demain ; la connexion est conservée.",
                    status=error.code,
                    reason="quota",
                    reasons=reasons,
                    retryable=temporary,
                ) from None
            messages = {
                401: "Connexion expirée ou clé invalide.",
                403: "Accès API non autorisé.",
                429: "Quota API atteint.",
            }
            raise ConnectorError(
                messages.get(error.code, f"Le service a répondu HTTP {error.code}."),
                status=error.code,
                reason="" if quota is not None else api_message,
                reasons=reasons,
                retryable=temporary,
            ) from None
        except (URLError, TimeoutError, OSError):
            delay = retry_delay(attempt, {}) if attempt < 2 else 0
            log_google_error("network", (), operation, attempt + 1, delay)
            if attempt < 2:
                wait_until(time.monotonic() + delay, cancel)
                continue
            raise ConnectorError(
                "Service inaccessible. Vérifiez la connexion et réessayez.", retryable=True
            ) from None
        except ConnectorError:
            raise
        except (ValueError, UnicodeError):
            raise ConnectorError("Le service a renvoyé des données invalides.") from None


def safe_url(value):
    value = value or ""
    parsed = urlsplit(value)
    return (
        value
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username
        else ""
    )
