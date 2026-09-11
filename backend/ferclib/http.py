"""
One shared HTTP client and content-addressed source cache for every adapter.

Requirements this implements (task section 3):
  * bounded parallelism, retry with backoff, Retry-After honoured;
  * content-type and body checks -- an HTML error page served with HTTP 200 must
    never be stored as if it were a filing;
  * explicit pagination completion, checked by the caller;
  * resumable checkpoints -- a cached object is never refetched;
  * credentials never printed, never written to any export, never logged. Query
    strings are redacted before anything is recorded.

The cache is content-addressed: the key is the sha256 of the bytes. Identical
bytes retrieved from two URLs are stored once and both URLs are recorded, which
is exactly the behaviour needed to recognise a byte-identical resubmission.

OFFLINE (cache-only) MODE
-------------------------
`set_offline(True)`, or `FERC_OFFLINE=1` in the environment, puts the whole
process into cache-only replay. Two things follow, and both are enforced here
rather than left to a caller's good manners (audit A01/A20):

  1. `_request()` refuses to open a socket at all. A cache miss raises
     `OfflineCacheMiss`, a distinct catchable subclass of `FetchError`, so a
     replay harness can tell "this evidence was never captured" apart from
     "the network failed".
  2. `api_key()` returns a non-credential sentinel instead of raising. The
     cache key is `sha256(redact(url))` and `redact()` replaces every secret
     query value with the literal `<REDACTED>`, so the key value has never been
     part of a cache key. Requiring a real -- or a dummy -- `FERC_API_KEY` to
     read bytes already on disk was therefore an artificial precondition on
     replay. The sentinel can never be transmitted, because in offline mode no
     request is issued.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "ferc-intel/0.30 (+local research tool)"

#: query parameters whose values are secrets and must never be recorded
SECRET_PARAMS = {"api_key", "apikey", "key", "token", "access_token", "subscription-key"}

_HTML_MARKERS = (b"<!doctype html", b"<html", b"request rejected",
                 b"<head><title>4", b"<head><title>5")

#: What `api_key()` returns in offline mode. Deliberately not credential-shaped:
#: it is never transmitted (offline refuses to open a socket) and it redacts to
#: the same `<REDACTED>` placeholder every other key does, so the cache key of a
#: previously captured URL is unchanged.
OFFLINE_KEY_SENTINEL = "OFFLINE-REPLAY-NO-CREDENTIAL"

_OFFLINE = os.environ.get("FERC_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def set_offline(flag: bool) -> None:
    """Put the process into cache-only replay (or take it out again).

    Process-global on purpose: adapters build their own URLs and call
    `api_key()` directly, so an offline switch that lived only on the run
    context would be exactly the ignored flag the audit found."""
    global _OFFLINE
    _OFFLINE = bool(flag)


def is_offline() -> bool:
    return _OFFLINE


class FetchError(RuntimeError):
    """A retrieval failure with the exact blocker preserved for the ledger."""

    def __init__(self, url: str, detail: str, status: int | None = None, attempts: int = 0):
        self.url = redact(url)
        self.detail = detail
        self.status = status
        self.attempts = attempts
        super().__init__(f"{self.url}: {detail}")


class BudgetExhausted(FetchError):
    """The per-run request budget ran out. Distinct so the runner can report a
    budget-limited run as bounded-and-incomplete rather than as complete."""


class OfflineCacheMiss(FetchError):
    """Offline replay needed an object that is not in the source cache.

    Distinct and catchable so a replay harness can report "this evidence was
    never captured" rather than silently treating it as a FERC data gap. Rule 3
    clause 7: a retrieval failure is not legitimate non-applicability."""

    def __init__(self, url: str, detail: str = "not in the source cache and "
                                               "offline replay may not open a socket"):
        super().__init__(url, detail, status=None, attempts=0)


def redact(url: str) -> str:
    """Strip secret query parameters. Every recorded URL passes through here."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.query:
        return url
    kept = []
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        kept.append((k, "<REDACTED>" if k.lower() in SECRET_PARAMS else v))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment))


def looks_like_html_error(body: bytes) -> bool:
    head = body[:600].lstrip().lower()
    return any(m in head for m in _HTML_MARKERS)


class SourceCache:
    """Content-addressed, shared, immutable. Adapters read and write it through
    the client only; nothing ever mutates a stored object in place."""

    def __init__(self, root: pathlib.Path):
        self.root = pathlib.Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._lock = threading.Lock()
        self._index: dict[str, dict] = {}
        if self.index_path.is_file():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._index = {}

    def _object_path(self, digest: str) -> pathlib.Path:
        return self.root / "objects" / digest[:2] / digest

    def url_key(self, url: str) -> str:
        return hashlib.sha256(redact(url).encode("utf-8")).hexdigest()

    def get(self, url: str) -> tuple[bytes, dict] | None:
        entry = self._index.get(self.url_key(url))
        if not entry:
            return None
        p = self._object_path(entry["content_hash"])
        if not p.is_file():
            return None
        return p.read_bytes(), entry

    def put(self, url: str, body: bytes, media_type: str, source_system: str) -> dict:
        digest = hashlib.sha256(body).hexdigest()
        p = self._object_path(digest)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file():                       # immutable: identical bytes stored once
            # Process-unique temp name: several adapters may run concurrently
            # against one shared cache, and a fixed ".tmp" would let them
            # clobber each other's half-written file.
            tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident():x}.tmp")
            tmp.write_bytes(body)
            tmp.replace(p)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            key = self.url_key(url)
            prev = self._index.get(key, {})
            entry = {
                "content_hash": digest,
                "source_url": redact(url),
                "source_system": source_system,
                "media_type": media_type,
                "byte_size": len(body),
                "first_seen_at": prev.get("first_seen_at", now),
                "last_seen_at": now,
                "fetch_count": int(prev.get("fetch_count", 0)) + 1,
                "cache_path": str(p.relative_to(self.root)),
            }
            self._index[key] = entry
            self._flush()
        return entry

    def _flush(self) -> None:
        # Re-read before writing: a concurrent process may have added entries
        # since we loaded, and a blind overwrite would silently drop them.
        if self.index_path.is_file():
            try:
                on_disk = json.loads(self.index_path.read_text(encoding="utf-8"))
                for k, v in on_disk.items():
                    self._index.setdefault(k, v)
            except (json.JSONDecodeError, OSError):
                pass
        tmp = self.index_path.with_suffix(f".{os.getpid()}.{threading.get_ident():x}.tmp")
        tmp.write_text(json.dumps(self._index, indent=0, sort_keys=True), encoding="utf-8")
        tmp.replace(self.index_path)

    def manifest_rows(self) -> list[dict]:
        rows = []
        for cache_key, e in self._index.items():
            rows.append({"cache_key": cache_key, "content_hash": e["content_hash"],
                         "source_system": e["source_system"],
                         "source_url": e["source_url"], "media_type": e["media_type"],
                         "byte_size": e["byte_size"], "first_seen_at": e["first_seen_at"],
                         "last_seen_at": e["last_seen_at"], "fetch_count": e["fetch_count"],
                         "note": ""})
        return rows


class Client:
    """Polite, resumable HTTP with a per-host request budget."""

    def __init__(self, cache: SourceCache, *, min_interval: float = 0.25,
                 max_attempts: int = 4, timeout: int = 300, budget: int | None = None,
                 logger=None, offline: bool | None = None):
        self.cache = cache
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.budget = budget
        self.requests_made = 0
        self.budget_exhausted = False
        self.cache_misses: list[str] = []
        # A bounded fallback can legitimately probe uncaptured request shapes
        # before a different cached official response proves it contains the
        # complete requested population. Keep those probes as evidence, but
        # remove them from the unresolved population only after validation.
        self.satisfied_cache_misses: list[dict] = []
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()
        self._log = logger or (lambda level, msg: None)
        if offline is not None:
            set_offline(offline)

    @property
    def offline(self) -> bool:
        return is_offline()

    def _miss(self, url: str):
        """One cache miss in offline mode: recorded, then raised. Recording it
        is what lets a replay report exactly which evidence is absent instead of
        producing a shorter, apparently clean run."""
        red = redact(url)
        self.cache_misses.append(red)
        self._log("error", f"offline cache miss: {red}")
        return OfflineCacheMiss(url)

    def satisfy_cache_misses_since(self, checkpoint: int, *, satisfied_by: dict) -> list[str]:
        """Move a caller-bounded miss suffix to the satisfied-probe ledger.

        This is not a general waiver. The caller must first validate one
        official response as a complete semantic substitute for every request
        it probed. Failed fallbacks never call this method, so their misses
        remain fatal to offline publication.
        """
        if not isinstance(checkpoint, int) or checkpoint < 0 \
                or checkpoint > len(self.cache_misses):
            raise ValueError("cache-miss checkpoint is outside the current miss ledger")
        if not isinstance(satisfied_by, dict) or not satisfied_by.get("reason") \
                or not satisfied_by.get("source_url") \
                or not re.fullmatch(r"[0-9a-f]{64}",
                                    str(satisfied_by.get("content_hash") or "")):
            raise ValueError("satisfied cache misses require a reason, source URL and hash")
        resolved = self.cache_misses[checkpoint:]
        del self.cache_misses[checkpoint:]
        for url in resolved:
            self.satisfied_cache_misses.append({
                "url": url,
                "satisfied_by": {
                    "reason": str(satisfied_by["reason"]),
                    "source_url": redact(str(satisfied_by["source_url"])),
                    "content_hash": str(satisfied_by["content_hash"]),
                },
            })
        return resolved

    # -------------------------------------------------------------- internals

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_call.get(host, 0.0)
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    def _spend(self, url: str) -> None:
        if self.budget is not None and self.requests_made >= self.budget:
            # Latched, because budget exhaustion must reach the run status: a
            # run that stopped fetching because it ran out of budget is not a
            # completed universe run (audit A01 clause 5).
            self.budget_exhausted = True
            raise BudgetExhausted(url, f"request budget of {self.budget} exhausted")
        self.requests_made += 1

    def _request(self, url: str, *, data: bytes | None, headers: dict) -> tuple[bytes, str, int]:
        if is_offline():
            # Belt and braces. get()/post_json() already refuse a miss, so this
            # only fires if some future caller reaches _request directly -- and
            # it must still never open a socket.
            raise self._miss(url)
        host = urllib.parse.urlsplit(url).netloc
        last_detail = "unknown"
        last_status = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle(host)
            self._spend(url)
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    status = r.status
                    media = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                    body = r.read()
                if status != 200:
                    raise urllib.error.HTTPError(url, status, "non-200", r.headers, None)
                if looks_like_html_error(body) and "html" not in media:
                    # HTML body on HTTP 200 -- a block page, not a filing.
                    raise FetchError(url, "HTML body served with HTTP 200 (blocked or error page)",
                                     status=200, attempts=attempt)
                if not body:
                    raise FetchError(url, "empty body on HTTP 200", status=200, attempts=attempt)
                return body, media, status
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_detail = f"HTTP {exc.code}"
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if exc.code in (400, 401, 403, 404, 410):
                    raise FetchError(url, last_detail, status=exc.code, attempts=attempt) from None
                delay = self._retry_delay(attempt, retry_after)
            except FetchError:
                raise
            except Exception as exc:                                  # noqa: BLE001
                last_detail = f"{type(exc).__name__}: {str(exc)[:120]}"
                delay = self._retry_delay(attempt, None)
            if attempt < self.max_attempts:
                self._log("warn", f"retry {attempt}/{self.max_attempts} in {delay:.0f}s "
                                  f"({last_detail}) {redact(url)}")
                time.sleep(delay)
        raise FetchError(url, last_detail, status=last_status, attempts=self.max_attempts)

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(120.0, float(retry_after))
            except ValueError:
                pass
        return min(60.0, 2.0 ** attempt)

    # -------------------------------------------------------------- public

    def get(self, url: str, *, source_system: str, use_cache: bool = True,
            accept: str = "*/*", headers: dict | None = None) -> tuple[bytes, dict]:
        """Fetch (or reuse) one object. Returns (body, cache_entry).

        `headers` merges over the defaults, for endpoints that require an Origin
        or Referer. Never put a credential in here: query strings are redacted
        before anything is recorded, but headers are not."""
        if use_cache or is_offline():
            hit = self.cache.get(url)
            if hit:
                return hit
        if is_offline():
            raise self._miss(url)
        h = {"User-Agent": UA, "Accept": accept}
        h.update(headers or {})
        body, media, _ = self._request(url, data=None, headers=h)
        return body, self.cache.put(url, body, media, source_system)

    def post_json(self, url: str, payload: dict, *, source_system: str,
                  use_cache: bool = False, headers: dict | None = None) -> tuple[bytes, dict]:
        """POST a JSON body. Cache key folds in the payload, so two different
        searches against one URL do not collide."""
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        cache_url = f"{url}#body={hashlib.sha256(blob).hexdigest()[:32]}"
        # `use_cache` defaults to False here because a live search is a fresh
        # question. Offline replay overrides that: it may only ever answer from
        # what was already captured, and a search whose payload was never
        # captured is a declared missing input, not a new live query (A20).
        if use_cache or is_offline():
            hit = self.cache.get(cache_url)
            if hit:
                return hit
        if is_offline():
            raise self._miss(cache_url)
        h = {"User-Agent": UA, "Content-Type": "application/json",
             "Accept": "application/json"}
        h.update(headers or {})
        body, media, _ = self._request(url, data=blob, headers=h)
        return body, self.cache.put(cache_url, body, media, source_system)

    def get_json(self, url: str, *, source_system: str, use_cache: bool = True):
        body, _ = self.get(url, source_system=source_system, use_cache=use_cache,
                           accept="application/json")
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FetchError(url, f"body is not JSON: {exc}") from None


# ---------------------------------------------------------------- credentials

def api_key(name: str = "FERC_API_KEY", env_path: pathlib.Path | None = None) -> str:
    """Read a key from the environment or the project .env.

    The value is returned for immediate use in a request and is never logged,
    echoed or persisted. Callers must build URLs and pass them through redact()
    before recording anything.

    In offline replay no request is issued, so no credential exists to read and
    none is needed: the cache key is computed from the REDACTED url, in which
    every key value is already the same literal placeholder. Returning the
    sentinel keeps url construction working without inventing a dummy secret.
    """
    if is_offline():
        return OFFLINE_KEY_SENTINEL
    v = os.environ.get(name)
    if v:
        return v.strip().strip("\"'")
    for parent in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
        env = env_path or (parent / ".env")
        if env.is_file():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(name):
                    return line.split("=", 1)[1].strip().strip("\"'")
        if env_path:
            break
    raise FetchError(f"<{name}>", f"{name} is not set in the environment or .env")


def assert_no_secrets(text: str) -> None:
    """Guard used by the export and manifest writers."""
    if re.search(r"(?i)(api_key|apikey|access_token|subscription-key)=(?!<REDACTED>)[A-Za-z0-9._-]{8,}",
                 text):
        raise AssertionError("refusing to write text containing an unredacted credential")
