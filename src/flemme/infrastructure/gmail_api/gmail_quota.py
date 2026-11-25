"""Coûts Gmail et seau partagé par compte dans ce processus."""

import threading
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

DEFAULT_BUDGET = 3000
COSTS = {
    "messages.get": 20, "messages.get.full": 60,
    "threads.get": 40, "threads.get.full": 120,
    "messages.list": 5, "threads.list": 10, "history.list": 2,
    "messages.send": 100, "getProfile": 1, "messages.modify": 5,
    "messages.attachments.get": 20, "labels.list": 1,
}
_lock = threading.RLock()
_vaults = {}
_accounts = {}


def operation_for(url):
    """Ne retourne que des noms constants ; jamais de segment issu d'un identifiant."""
    parsed = urlsplit(url)
    parts = parsed.path.split("/users/me/")[-1].split("/")
    if parts == ["profile"]:
        return "getProfile"
    if parts == ["history"]:
        return "history.list"
    if parts == ["labels"]:
        return "labels.list"
    if parts[0] in {"messages", "threads"}:
        if len(parts) == 1:
            return parts[0] + ".list"
        if parts == ["messages", "send"]:
            return "messages.send"
        if len(parts) == 3 and parts[2] == "modify":
            return "messages.modify"
        if len(parts) == 4 and parts[2] == "attachments":
            return "messages.attachments.get"
        if len(parts) == 2:
            full = parse_qs(parsed.query).get("format", ["full"])[0] in {"full", "raw", "html"}
            return parts[0] + ".get" + (".full" if full else "")
    raise ValueError("Opération Gmail non prise en charge.")


class GmailQuota:
    def __init__(self, settings=None):
        self.settings = settings
        self.tokens = 120.0
        self.send_tokens = 100.0
        self.next_send = 0.0
        self.updated = time.monotonic()
        self.lock = threading.Lock()
        self.account_key = ""
        blocks = (settings.get("gmail.daily_quota_blocks") or {}) if settings else {}
        self.block_until = blocks.get("unidentified", "")

    def wait(self, operation, cancel=lambda: False):
        from .http_client import ConnectorError, wait_until

        cost = COSTS[operation]
        while True:
            if cancel():
                raise ConnectorError("Opération annulée.")
            with self.lock:
                if self.block_until and datetime.now(UTC).isoformat() < self.block_until:
                    raise ConnectorError("Quota quotidien Gmail atteint. Réessayez demain.",
                                         reason="quota", reasons=("dailyLimitExceeded",))
                budget = self.settings.get("gmail.units_per_minute") if self.settings else DEFAULT_BUDGET
                now = time.monotonic()
                elapsed = max(0, now - self.updated)
                self.tokens = min(120.0, self.tokens + elapsed * budget / 60)
                self.send_tokens = min(100.0, self.send_tokens + elapsed * budget / 120)
                self.updated = now
                delay = (cost - self.tokens) * 60 / budget
                if operation == "messages.send":
                    delay = max(delay, (cost - self.send_tokens) * 120 / budget,
                                self.next_send - now)
                if delay <= 1e-9:
                    self.tokens = max(0, self.tokens - cost)
                    if operation == "messages.send":
                        self.send_tokens = max(0, self.send_tokens - cost)
                        self.next_send = now + 4
                    return
            wait_until(time.monotonic() + delay, cancel)

    def block_today(self):
        tomorrow = datetime.now(UTC).date() + timedelta(days=1)
        with self.lock:
            self.block_until = datetime.combine(tomorrow, datetime.min.time(), UTC).isoformat()
            if self.settings:
                blocks = self.settings.get("gmail.daily_quota_blocks") or {}
                self.settings.set("gmail.daily_quota_blocks",
                                  {**blocks, self.account_key or "unidentified": self.block_until})


def quota_for(vault, settings=None):
    # Le chemin identifie le coffre avant la première lecture du profil (aucun secret lu ici).
    key = str(vault.directory.resolve()) if hasattr(vault, "directory") else vault
    with _lock:
        if key not in _vaults:
            _vaults[key] = GmailQuota(settings)
        quota = _vaults[key]
        if settings is not None:
            quota.settings = settings
            account = settings.get("gmail.quota_account")
            if account:
                quota = bind_account(vault, account)
        return quota


def bind_account(vault, account):
    key = str(vault.directory.resolve()) if hasattr(vault, "directory") else vault
    account_key = sha256(account.strip().lower().encode()).hexdigest()
    with _lock:
        previous = _vaults.setdefault(key, GmailQuota())
        if previous.account_key and previous.account_key != account_key:
            previous = GmailQuota(previous.settings)
        quota = _accounts.setdefault(account_key, previous)
        _vaults[key] = quota
        quota.account_key = account_key
        if previous.settings:
            quota.settings = previous.settings
            blocks = quota.settings.get("gmail.daily_quota_blocks") or {}
            quota.block_until = blocks.get(account_key, "")
            if quota.settings.get("gmail.quota_account") != account:
                quota.settings.set("gmail.quota_account", account)
        return quota


def forget_account(vault):
    key = str(vault.directory.resolve()) if hasattr(vault, "directory") else vault
    with _lock:
        settings = quota_for(vault).settings
        if settings:
            settings.set("gmail.quota_account", "")
        _vaults[key] = GmailQuota(settings)
