"""Polite, cached HTTP client for Tabroom.

Tabroom put its results and postings pages behind a login in 2026, citing load
from AI crawlers, and disallows those paths in robots.txt. This client is built
so that pulling a full season costs Tabroom roughly what one human browsing the
site for an afternoon costs it, and so that re-running the pipeline costs it
nothing at all:

  * every response is written to an on-disk cache and never re-fetched
  * requests are serialised with a minimum interval between them
  * completed tournaments are immutable, so once a season is cached the rolling
    update only ever touches the current season's tournaments
  * a single retry with backoff on 5xx, and a hard stop on auth failure rather
    than hammering the login redirect

Auth is a session cookie supplied by the user, read from (in order) the
TABROOM_COOKIE environment variable or data/.tabroom_cookie. Never commit it;
.gitignore covers both.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "raw", "cache")
COOKIE_FILE = os.path.join(ROOT, "data", ".tabroom_cookie")
BASE = "https://www.tabroom.com"

USER_AGENT = (
    "NDT-CEDA-Glicko/1.0 (+https://github.com/jsong175/NDT-CEDA-Glicko) "
    "one-pass season archiver, contact via GitHub issues"
)

MIN_INTERVAL = float(os.environ.get("TABROOM_MIN_INTERVAL", "1.25"))  # seconds
MAX_RETRIES = 2

_last_request = [0.0]


class RateLimitError(RuntimeError):
    """Tabroom served its rate-limit notice. It comes back as HTTP 200 with a
    normal-looking page, so nothing but the body text gives it away -- and if it
    reaches the cache it is indistinguishable from a real page on every later
    run."""


class AuthError(RuntimeError):
    """Tabroom bounced us to the login page: the cookie is missing or stale."""


class FetchError(RuntimeError):
    pass


def load_cookie():
    """Return the raw Cookie header value, or None if we have no credentials."""
    env = os.environ.get("TABROOM_COOKIE", "").strip()
    if env:
        return _normalise_cookie(env)
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        if raw:
            return _normalise_cookie(raw)
    return None


def _normalise_cookie(raw):
    """Accept either a bare token or a full 'a=1; b=2' cookie header.

    People paste both shapes out of devtools, and a bare token is by far the
    most common, so treat anything without '=' as a TabroomToken value.
    """
    raw = raw.strip().strip('"')
    if "=" not in raw:
        return "TabroomToken=" + raw
    return raw


def cache_path(url):
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest[:2], digest + ".html.gz")


def is_cached(url):
    return os.path.exists(cache_path(url))


def _read_cache(url):
    with gzip.open(cache_path(url), "rt", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _write_cache(url, body):
    path = cache_path(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(body)
    # Keep a human-readable index so the cache can be audited or pruned by URL.
    with open(os.path.join(CACHE_DIR, "index.tsv"), "a", encoding="utf-8") as fh:
        fh.write("%s\t%s\n" % (os.path.relpath(path, CACHE_DIR), url))


def _throttle():
    elapsed = time.time() - _last_request[0]
    wait = MIN_INTERVAL - elapsed
    if wait > 0:
        time.sleep(wait)
    # A little jitter so we never look like a metronome.
    time.sleep(random.uniform(0, 0.25))
    _last_request[0] = time.time()


def fetch(path, cookie=None, force=False, allow_anonymous=False):
    """GET a Tabroom page, from cache when possible.

    `path` may be absolute or site-relative. Returns the HTML body as str.
    """
    url = path if path.startswith("http") else urllib.parse.urljoin(BASE, path)

    if not force and is_cached(url):
        return _read_cache(url)

    if cookie is None and not allow_anonymous:
        cookie = load_cookie()
        if cookie is None:
            raise AuthError(
                "No Tabroom session cookie. Set TABROOM_COOKIE or write "
                "data/.tabroom_cookie. See README section 'Getting a cookie'."
            )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        req = urllib.request.Request(url, headers=headers)
        try:
            # Do not follow redirects blindly: a 302 here means the login wall.
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=45) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                text = body.decode("utf-8", errors="replace")
                if _is_rate_limited(text):
                    # Never cache this: it is a 200, so it would masquerade as
                    # the real page forever after.
                    if attempt < MAX_RETRIES:
                        time.sleep(60 * (attempt + 1))
                        last_err = RateLimitError(url)
                        continue
                    raise RateLimitError(
                        "Tabroom rate-limited us at %s. It lifts after about an "
                        "hour; nothing was cached, so re-running picks up where "
                        "this left off." % url)
                _write_cache(url, text)
                return text
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "")
                if "login" in location:
                    raise AuthError(
                        "Tabroom redirected %s to the login page. The cookie is "
                        "missing, expired, or lacks access." % url
                    )
                return fetch(location, cookie=cookie, force=force)
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                last_err = exc
                continue
            raise FetchError("HTTP %s for %s" % (exc.code, url)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                last_err = exc
                continue
            raise FetchError("network error for %s: %s" % (url, exc)) from exc

    raise FetchError("exhausted retries for %s: %s" % (url, last_err))


RATE_LIMIT_MARKERS = (
    "hit our rate limit",
    "may not access this page for another hour",
)


def _is_rate_limited(text):
    # The notice sits near the END of the page, after all the usual chrome
    # (offset ~22k of 22.6k on the ones we caught), so a head-only scan misses
    # it. These pages are small; scan the whole body.
    low = text.lower()
    return any(m in low for m in RATE_LIMIT_MARKERS)


def purge_bad_cache():
    """Drop cached pages that are really rate-limit notices.

    Returns the number removed. Anything fetched while Tabroom was throttling us
    is a 6KB error page wearing a 200, and it will be served as truth forever
    unless it is taken out.
    """
    removed = 0
    for root, _dirs, files in os.walk(CACHE_DIR):
        for fn in files:
            if not fn.endswith(".html.gz"):
                continue
            path = os.path.join(root, fn)
            try:
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            if _is_rate_limited(body):
                os.remove(path)
                removed += 1
    return removed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def check_auth(cookie=None):
    """Verify the cookie actually opens a gated page. Returns (ok, detail)."""
    probe = "/index/tourn/fields.mhtml?tourn_id=36610"
    try:
        html = fetch(probe, cookie=cookie, force=True)
    except AuthError as exc:
        return False, str(exc)
    except FetchError as exc:
        return False, str(exc)
    if "login" in html[:2000].lower() and "password" in html.lower()[:6000]:
        return False, "fetched the page but it looks like the login form"
    return True, "cookie works (%d bytes from the gated fields page)" % len(html)


def cache_stats():
    n = size = 0
    for dirpath, _dirs, files in os.walk(CACHE_DIR):
        for name in files:
            if name.endswith(".html.gz"):
                n += 1
                size += os.path.getsize(os.path.join(dirpath, name))
    return {"pages": n, "bytes": size}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "check":
        ok, detail = check_auth()
        print(("OK: " if ok else "FAIL: ") + detail)
        sys.exit(0 if ok else 1)
    print(json.dumps(cache_stats(), indent=2))
