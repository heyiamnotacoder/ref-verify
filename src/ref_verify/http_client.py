"""The only network boundary: pooled Keep-Alive GETs, retries, NCBI pacing, and request de-duplication."""

from __future__ import annotations

import gzip
import http.client
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

from ref_verify import __version__
from ref_verify.models import HttpResponse

_MAILTO = os.environ.get("REF_VERIFY_MAILTO", "").strip() or "ref-verify@users.noreply.github.com"


USER_AGENT = (
    f"ref-verify/{__version__} "
    f"(scientific-citation-verifier; https://github.com/heyiamnotacoder/ref-verify; mailto:{_MAILTO})"
)


DEFAULT_TIMEOUT = 15.0


def _decompress_body(raw: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if "deflate" in enc:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def is_transient_http(resp: HttpResponse | None) -> bool:
    return resp is not None and (resp.status == 0 or resp.status in (429, 500, 502, 503, 504))


class HttpClient:
    FALLBACK_TIMEOUT = 4.0

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries
        self.ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
        self._ncbi_lock = threading.Lock()
        self._next_ncbi_slot = 0.0
        self._pool_lock = threading.Lock()
        self._pools: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._ssl_ctx: ssl.SSLContext | None = None
        self._cache_lock = threading.Lock()
        self._cache: dict[tuple[str, str], HttpResponse] = {}
        self._inflight: dict[tuple[str, str], threading.Event] = {}
        self._has_proxies = bool(urllib.request.getproxies())

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._pool_lock:
            for conns in self._pools.values():
                for conn in conns:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._pools.clear()

    def _get_ssl_context(self) -> ssl.SSLContext:
        with self._pool_lock:
            if self._ssl_ctx is None:
                self._ssl_ctx = ssl.create_default_context()
            return self._ssl_ctx

    def _borrow_conn(
        self, scheme: str, host: str, port: int, timeout: float, force_fresh: bool = False
    ) -> tuple[http.client.HTTPConnection, bool]:
        key = (scheme, host, port)
        if not force_fresh:
            with self._pool_lock:
                bucket = self._pools.get(key)
                if bucket:
                    conn = bucket.pop()
                    conn.timeout = timeout
                    if conn.sock is not None:
                        conn.sock.settimeout(timeout)
                    return conn, True
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._get_ssl_context()), False
        return http.client.HTTPConnection(host, port, timeout=timeout), False

    def _return_conn(self, scheme: str, host: str, port: int, conn: http.client.HTTPConnection) -> None:
        key = (scheme, host, port)
        with self._pool_lock:
            bucket = self._pools.setdefault(key, [])
            if len(bucket) < 16:
                bucket.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def _pace_ncbi(self) -> None:
        min_gap = 0.11 if self.ncbi_api_key else 0.36  # NCBI: 10/s with key, 3/s without (+margin)
        with self._ncbi_lock:
            now = time.monotonic()
            slot = max(now, self._next_ncbi_slot)
            self._next_ncbi_slot = slot + min_gap
            wait_s = slot - now
        if wait_s > 0:
            time.sleep(wait_s)

    def _single_request_pooled(
        self, url: str, req_headers: dict[str, str], timeout: float
    ) -> tuple[int, str, str, str | None, str | None]:
        """Perform one GET with Keep-Alive connection reuse & redirect handling.

        Returns (status, body, final_url, reason, retry_after).
        """
        cur_url = url
        for _ in range(6):
            parsed = urllib.parse.urlsplit(cur_url)
            scheme = (parsed.scheme or "https").lower()
            host = parsed.hostname or ""
            port = parsed.port or (443 if scheme == "https" else 80)
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"

            stale_retried = False
            while True:
                conn, reused = self._borrow_conn(scheme, host, port, timeout, force_fresh=stale_retried)
                try:
                    conn.request("GET", target, headers=req_headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                    status = resp.status
                    reason = resp.reason
                    headers_msg = resp.headers
                    should_close = resp.will_close or (headers_msg.get("Connection") or "").lower() == "close"
                except (
                    http.client.RemoteDisconnected,
                    http.client.BadStatusLine,
                    BrokenPipeError,
                    ConnectionResetError,
                    ssl.SSLError,
                ):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if reused and not stale_retried:
                        stale_retried = True
                        continue
                    raise
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise

                if should_close:
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    self._return_conn(scheme, host, port, conn)
                break

            if status in (301, 302, 303, 307, 308):
                loc = headers_msg.get("Location")
                if loc:
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    continue

            decompressed = _decompress_body(raw, headers_msg.get("Content-Encoding") or "")
            charset = headers_msg.get_content_charset() or "utf-8"
            body = decompressed.decode(charset, errors="replace")
            return status, body, cur_url, reason, headers_msg.get("Retry-After")

        return 0, "", cur_url, "Too many redirects", None

    def _single_request_urllib(
        self, url: str, req_headers: dict[str, str], timeout: float
    ) -> tuple[int, str, str, str | None, str | None]:
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                decompressed = _decompress_body(raw, resp.headers.get("Content-Encoding") or "")
                charset = resp.headers.get_content_charset() or "utf-8"
                return getattr(resp, "status", 200), decompressed.decode(charset, errors="replace"), url, None, None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                decompressed = _decompress_body(raw, exc.headers.get("Content-Encoding") if exc.headers else "")
                body = decompressed.decode("utf-8", errors="replace")
            except Exception:
                body = ""
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return exc.code, body, url, str(exc.reason), retry_after

    def get_fallback(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        primary_failed_transient: bool = False,
    ) -> HttpResponse:
        """GET for secondary fallback sources with policy confined inside HttpClient.

        Uses full timeout/retries when the primary source suffered a transient outage (status 0/429/5xx),
        and bounded timeout with zero retries for best-effort enrichment/fallback otherwise.
        """
        if primary_failed_transient:
            return self.get(url, headers)
        return self.get(url, headers, timeout=min(self.timeout, self.FALLBACK_TIMEOUT), retries=0)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> HttpResponse:
        """The single overridable request seam; ``None`` means the client's defaults."""
        return self._get_impl(
            url,
            headers=headers,
            timeout=self.timeout if timeout is None else timeout,
            retries=self.retries if retries is None else retries,
        )

    def _get_impl(
        self,
        url: str,
        headers: dict[str, str] | None,
        timeout: float,
        retries: int,
    ) -> HttpResponse:
        is_ncbi = "eutils.ncbi.nlm.nih.gov" in url
        if is_ncbi and self.ncbi_api_key and "api_key=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api_key={urllib.parse.quote(self.ncbi_api_key)}"

        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(headers)

        cache_key = (url, req_headers.get("Accept", "*/*"))
        while True:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    return cached
                inflight_evt = self._inflight.get(cache_key)
                if inflight_evt is None:
                    inflight_evt = threading.Event()
                    self._inflight[cache_key] = inflight_evt
                    break
            inflight_evt.wait()

        result: HttpResponse | None = None
        try:
            if is_ncbi:
                self._pace_ncbi()

            attempt = 0
            last_err = ""
            while attempt <= retries:
                try:
                    if self._has_proxies:
                        status, body, final_url, reason, retry_after = self._single_request_urllib(
                            url, req_headers, timeout
                        )
                    else:
                        status, body, final_url, reason, retry_after = self._single_request_pooled(
                            url, req_headers, timeout
                        )
                except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                    last_err = str(exc)
                    if attempt < retries:
                        time.sleep(0.5 * (2**attempt))
                        attempt += 1
                        if is_ncbi:
                            self._pace_ncbi()
                        continue
                    result = HttpResponse(status=0, body="", url=url, error=last_err)
                    break

                if status in (429, 500, 502, 503, 504) and attempt < retries:
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (0.7 * (2**attempt))
                    time.sleep(min(delay, 5.0))
                    attempt += 1
                    last_err = f"HTTP {status}"
                    if is_ncbi:
                        self._pace_ncbi()
                    continue

                if 200 <= status < 300:
                    result = HttpResponse(status=status, body=body, url=final_url)
                elif status == 0:
                    result = HttpResponse(
                        status=0, body="", url=final_url, error=reason or last_err or "request failed"
                    )
                else:
                    result = HttpResponse(
                        status=status,
                        body=body,
                        url=final_url,
                        error=f"HTTP {status} {reason or ''}".strip(),
                    )
                break

            if result is None:
                result = HttpResponse(status=0, body="", url=url, error=last_err or "request failed")
            return result
        finally:
            with self._cache_lock:
                if result is not None and result.status in (200, 404):
                    self._cache[cache_key] = result
                evt = self._inflight.pop(cache_key, None)
                if evt is not None:
                    evt.set()
