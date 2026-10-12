import logging
import math
import re
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


class ExportError(Exception):
    """Only sanitized diagnostics may be included in this exception."""


class ApiApplicationError(ExportError):
    """An explicit API error envelope, distinct from transport/schema failures."""


def transient_tls_eof(error):
    """Inspect exception types only; never stringify URLs or credentials."""
    pending, seen, eof = [error], set(), False
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, ssl.SSLCertVerificationError):
            return False
        eof = eof or isinstance(item, ssl.SSLEOFError)
        pending.extend(arg for arg in getattr(item, "args", ()) if isinstance(arg, BaseException))
        for attribute in ("reason", "__cause__", "__context__"):
            nested = getattr(item, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return eof


class ApiClient:
    def __init__(self, base_url, api_key, *, interval=1.1, attempts=4,
                 session=None, sleep=time.sleep, clock=time.monotonic):
        parsed = urlparse(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in ("", "/")):
            raise ExportError("ASPRO_BASE_URL must be an HTTPS origin without credentials")
        if not api_key or not api_key.strip():
            raise ExportError("ASPRO_API_KEY is missing")
        if not math.isfinite(interval) or interval <= 0 or attempts < 1:
            raise ExportError("Invalid request interval or attempts")
        self.base_url = base_url.rstrip("/") + "/api/v1/module/"
        self.api_key = api_key
        self.interval, self.attempts = interval, attempts
        self.session = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self.last_request = None

    def _wait(self):
        if self.last_request is not None:
            self.sleep(max(0, self.interval - (self.clock() - self.last_request)))
        self.last_request = self.clock()

    def _retry_delay(self, response, attempt):
        delay = min(60, 2 ** attempt)
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    delay = float(header)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        pass
        if not math.isfinite(delay) or delay > 300:
            raise ExportError("Retry-After exceeds allowed wait")
        return max(0, delay)

    def get(self, path, params=None):
        # Do not accept arbitrary URLs, traversal, or write methods (some use GET).
        standard_read = re.fullmatch(r"[a-z_]+/[a-z_]+/(?:list|get/[0-9]+)", path)
        customlist_read = re.fullmatch(r"customlists/lists/[0-9]+/items", path)
        if not (standard_read or customlist_read):
            raise ExportError("Only allowlisted read-only paths are allowed")
        query = dict(params or {})
        query["api_key"] = self.api_key
        for attempt in range(self.attempts):
            self._wait()
            response = None
            transient = False
            try:
                response = self.session.get(self.base_url + path, params=query,
                                            timeout=(10, 45), allow_redirects=False, verify=True)
            except requests.exceptions.SSLError as error:
                if not transient_tls_eof(error):
                    raise ExportError("TLS connection failed; certificate verification remains enabled") from None
                transient = True
            except (requests.Timeout, requests.ConnectionError):
                transient = True
            except requests.RequestException:
                raise ExportError("HTTP request failed") from None
            if response is not None:
                transient = response.status_code in (429, 500, 502, 503, 504)
            if transient:
                if attempt + 1 == self.attempts:
                    raise ExportError("Temporary API failure; retry limit reached")
                log.warning("Temporary API failure; retry %d/%d", attempt + 1, self.attempts)
                self.sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code != 200:
                raise ExportError(f"API HTTP status {response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise ExportError("API returned invalid JSON") from None
            if not isinstance(data, dict):
                raise ExportError("API returned an invalid envelope")
            if "error" in data:
                error = data["error"]
                if (isinstance(error, dict) and error.get("error_code") == 22 and
                        str(error.get("error_msg", "")).strip().lower().rstrip(".") ==
                        "apikey has not been granted to the module"):
                    raise ApiApplicationError("API key is not granted access to this module (code 22)")
                raise ApiApplicationError("API returned an application error")
            result = data.get("response")
            if not isinstance(result, dict):
                raise ExportError("API response object is missing")
            return result

    def records(self, path, params=None):
        if re.fullmatch(r"customlists/lists/[0-9]+/items", path):
            raise ExportError("Customlist items response and pagination schema are not yet confirmed")
        params = dict(params or {})
        if "page" in params or "limit" in params or "api_key" in params:
            raise ExportError("Pagination and authentication are managed by the client")
        seen, expected, page = set(), None, 1
        while True:
            result = self.get(path, {**params, "page": page, "limit": 100})
            items = result.get("items")
            total = result.get("total_result")
            # The live core/user/list response omits total_result. Only use total
            # for an unfiltered query; filtered totals cannot be inferred safely.
            if (total is None and path == "core/user/list" and
                    not any(k == "search" or k.startswith("filter") for k in params)):
                total = result.get("total")
            if (not isinstance(items, list) or type(total) is not int or total < 0 or
                    type(result.get("page")) is not int or type(result.get("count")) is not int or
                    result.get("page") != page or result.get("count") != len(items)):
                raise ExportError("Invalid pagination metadata")
            if expected is None:
                expected = total
            if total != expected:
                raise ExportError("Record count changed during export; rerun required")
            if len(items) > 100:
                raise ExportError("API exceeded requested page size")
            for item in items:
                if not isinstance(item, dict) or item.get("id") is None:
                    raise ExportError("Record identifier is missing")
                identifier = str(item["id"])
                if identifier in seen:
                    raise ExportError("Duplicate record across API pages")
                seen.add(identifier)
                yield item
            if page == 1 or page % 25 == 0 or len(seen) == expected:
                log.info("API %s: %d/%d records", path, len(seen), expected)
            if len(seen) == expected:
                return
            if len(seen) > expected or not items:
                raise ExportError("Incomplete or inconsistent API pagination")
            page += 1
