"""Standalone read-only Aspro.Cloud CSV export. See README.md for setup."""
import argparse
import csv
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import ssl
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("aspro_export")


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



def join_labels(values, spec):
    if any(not isinstance(value, str) for value in values):
        raise ExportError("Invalid related string value")
    if spec.get("clean_email_list"):
        values = [value.replace("\ufeff", "").strip() for value in values]
        values = [value for value in values if value]
    if spec.get("unique"):
        values = list(dict.fromkeys(values))
    return spec["separator"].join(values)


class LookupTable(dict):
    """Resolve a missing ID with a documented get method and cache the result."""
    def __init__(self, values, client, spec):
        super().__init__(values)
        self.client, self.spec = client, spec

    def __getitem__(self, identifier):
        value = super().__getitem__(identifier)
        if (value is None or value == "") and self.spec.get("unavailable_label") == "id":
            return self._original_id(identifier)
        return value

    def __missing__(self, identifier):
        path = self.spec.get("get_path")
        if not path:
            raise KeyError(identifier)
        try:
            record = self.client.get(path.format(id=identifier))
        except ApiApplicationError:
            if self.spec.get("unavailable_label") != "id":
                raise
            return self._original_id(identifier)
        label = self.spec["label"]
        if str(record.get("id")) != identifier or label not in record:
            raise ExportError("Individual lookup returned an invalid related record")
        value = record[label]
        if value is None or value == "":
            if self.spec.get("unavailable_label") == "id":
                return self._original_id(identifier)
            raise ExportError("Individual lookup label is unavailable")
        self[identifier] = value
        return value

    def _original_id(self, identifier):
        self[identifier] = identifier
        log.warning("Related user name unavailable; original ID retained (value omitted)")
        return identifier

def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path, headers, rows, delimiter=","):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        writer.writerow(headers)
        count = 0
        for row in rows:
            if len(row) != len(headers):
                raise ExportError("CSV row width differs from header")
            writer.writerow(row)
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def validate_csv(path, headers, count, delimiter=","):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        if next(reader, None) != headers:
            raise ExportError("CSV header validation failed")
        actual = 0
        for row in reader:
            if len(row) != len(headers):
                raise ExportError("CSV row validation failed")
            actual += 1
        if actual != count:
            raise ExportError("CSV record count validation failed")


class Snapshot:
    """Publish the entire validated generation via a single current symlink."""
    def __init__(self, output_dir):
        self.root = Path(output_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.generations = self.root / "generations"
        self.generations.mkdir(exist_ok=True)
        self.stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.generations))
        self.published = False

    def commit(self, reports):
        manifest = {"reports": []}
        for report in reports:
            name, headers, count = report[:3]
            delimiter = report[3] if len(report) > 3 else ","
            path = self.stage / name
            validate_csv(path, headers, count, delimiter)
            manifest["reports"].append({"file": name, "rows": count,
                "sha256": sha256_file(path)})
        if not reports:
            raise ExportError("Cannot publish an empty report set")
        with (self.stage / "manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(self.stage)
        final = self.generations / uuid.uuid4().hex
        os.replace(self.stage, final)
        fsync_directory(self.generations)
        link = self.root / (".current-" + uuid.uuid4().hex)
        try:
            link.symlink_to(final.relative_to(self.root), target_is_directory=True)
            os.replace(link, self.root / "current")
            fsync_directory(self.root)
            self.published = True
        finally:
            link.unlink(missing_ok=True)
        # Keep the previous generation for recovery. No date-based archive.
        others = sorted((p for p in self.generations.iterdir() if p.is_dir() and p != final
                         and not p.name.startswith(".pending-")), key=lambda p: p.stat().st_mtime,
                        reverse=True)
        for old in others[1:]:
            shutil.rmtree(old)

    def close(self):
        if self.stage.exists():
            shutil.rmtree(self.stage)

def task_depths(records):
    parents = {str(r["id"]): str(r.get("parent_id") or "0") for r in records}
    depths = {}
    def resolve(identifier, pending):
        if identifier in depths:
            return depths[identifier]
        if identifier in pending:
            raise ExportError("Task hierarchy contains a cycle")
        parent = parents[identifier]
        depth = 0 if parent == "0" else 1 + (resolve(parent, pending | {identifier}) if parent in parents else 0)
        depths[identifier] = depth
        return depth
    for identifier in parents:
        resolve(identifier, set())
    return depths


def transform(record, column, lookups):
    if column.get("unresolved"):
        raise ExportError("Unresolved column: " + column["header"])
    if column.get("transform") == "constant":
        return column["value"]
    if column.get("closed_labels"):
        state = str(record.get("active"))
        if state in column["closed_labels"]:
            return column["closed_labels"][state]
    field = column.get("field")
    if field not in record:
        raise ExportError("Missing API field for column: " + column["header"])
    value = record[field]
    if column.get("transform") == "task_id":
        return "— " * lookups["task_depths"][str(value)] + str(value)
    if column.get("only_active") is not None and str(record.get("active")) != str(column["only_active"]):
        return ""
    if str(value) == "0" and str(record.get("id")) in column.get("zero_record_labels", {}):
        return column["zero_record_labels"][str(record["id"])]
    if column.get("transform") == "overdue_flag":
        if not value or str(value).startswith("0000-00-00"):
            return "0"
        try:
            deadline = datetime.fromisoformat(str(value))
            closed = record["closed_date"]
            end = (datetime.fromisoformat(str(closed)) if closed and
                   not str(closed).startswith("0000-00-00") else
                   datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None))
            return str(int(end > deadline))
        except (KeyError, ValueError, TypeError):
            raise ExportError("Invalid overdue dates") from None
    if column.get("blank_if_zero") and str(value) == "0":
        return ""
    kind = column.get("transform", "text")
    if kind == "project":
        if "module" not in record or "model" not in record:
            raise ExportError("Project relation discriminator is missing")
        if record["module"] != "st" or record["model"] != "project":
            return ""
        kind = "lookup"
    if kind == "custom":
        if not isinstance(value, list):
            raise ExportError("Invalid customfields structure")
        matches = [f for f in value if str(f.get("id")) == str(column["custom_id"])]
        if len(matches) != 1:
            raise ExportError("Custom field missing or ambiguous: " + column["header"])
        value = matches[0].get("value")
        kind = column.get("value_transform", "text")
    if value is None or value == "":
        return column.get("empty_value", "")
    if kind == "lookup":
        if str(value) == "0":
            return column.get("zero_label", "")
        table = lookups[column["lookup"]]
        try:
            value = table[str(value)]
        except KeyError:
            if column.get("empty_if_missing"):
                return ""
            raise ExportError("Missing related record for column: " + column["header"]) from None
    elif kind in ("enum", "reference_labels"):
        values = column["values"]
        if str(value) not in values:
            raise ExportError("Unknown enum value or reference ID for column: " + column["header"])
        value = values[str(value)]
    elif kind in ("date", "datetime"):
        if value in ("0000-00-00", "0000-00-00 00:00:00"):
            return ""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Europe/Moscow"))
            value = parsed.strftime("%d.%m.%Y" if kind == "date" else "%d.%m.%Y %H:%M")
            if kind == "datetime" and column.get("hour_unpadded"):
                value = parsed.strftime("%d.%m.%Y ") + str(parsed.hour) + parsed.strftime(":%M")
        except (ValueError, TypeError):
            raise ExportError("Invalid date for column: " + column["header"]) from None
    elif kind in ("decimal", "money"):
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise InvalidOperation
            if kind == "money":
                if number != number.quantize(Decimal("0.01")):
                    raise InvalidOperation
                value = format(number, ",.2f").replace(",", " ") + " p."
            else:
                value = format(number, "f")
        except (ValueError, InvalidOperation):
            raise ExportError("Invalid number for column: " + column["header"]) from None
    elif kind != "text":
        raise ExportError("Unknown transformation")
    if isinstance(value, (list, dict)):
        raise ExportError("Complex value needs an explicit transformation: " + column["header"])
    if value is None:
        return ""
    value = str(value)
    if column.get("normalize_nbsp"):
        value = value.replace('\xa0', ' ')
    if column.get("blank_if_unpaired_quotes") and unescape(value).count('"') % 2:
        return ""
    if column.get("escape_apostrophe"):
        value = value.replace("'", '&#039;')
    value = value.strip() if column.get("strip") else value
    return value



def validate_config(config, selected):
    reports = [r for r in config["reports"] if not selected or r["name"] in selected]
    if not reports or (selected and set(selected) != {r["name"] for r in reports}):
        raise ExportError("Unknown or empty report selection")
    filenames = set()
    for report in reports:
        if report.get("unresolved"):
            raise ExportError("Report API source unresolved: " + report["name"])
        filename = report["filename"]
        if report.get("delimiter", ",") not in (",", ";"):
            raise ExportError("Unsupported CSV delimiter")
        if Path(filename).name != filename or filename in filenames:
            raise ExportError("Invalid or duplicate output filename")
        filenames.add(filename)
        if not report.get("path"):
            raise ExportError("Report API source unresolved: " + report["name"])
        for column in report["columns"]:
            if column.get("unresolved"):
                raise ExportError("Unresolved mapping: " + report["name"] + "/" + column["header"])
    return reports


def run(config, reports, output, client):
    needed = {c["lookup"] for r in reports for c in r["columns"] if "lookup" in c}
    lookups = {}
    for name in sorted(needed):
        spec = config["lookups"][name]
        table = {}
        for item in client.records(spec["path"], spec.get("params")):
            key_field = spec.get("key", "id")
            if spec["label"] not in item or key_field not in item:
                raise ExportError("Lookup label field missing: " + name)
            if spec.get("many"):
                table.setdefault(str(item[key_field]), []).append(item[spec["label"]])
            else:
                identifier = str(item[key_field])
                if identifier in table:
                    raise ExportError("Duplicate lookup key: " + name)
                table[identifier] = item[spec["label"]]
        if spec.get("many"):
            for identifier, values in table.items():
                table[identifier] = join_labels(values, spec)
        lookups[name] = LookupTable(table, client, spec)
        user_labels = Path(os.environ.get("ASPRO_USER_LABELS_FILE", str(Path.home()/".config/aspro-export/user-labels.json")))
        if name == "users" and user_labels.exists():
            supplemental = json.loads(user_labels.read_text())
            if not isinstance(supplemental, dict) or any(
                    not str(k).isdigit() or not isinstance(v, str) or not v
                    for k, v in supplemental.items()):
                raise ExportError("Invalid supplemental user labels")
            for identifier, label in supplemental.items():
                if not table.get(identifier):
                    lookups[name][identifier] = label
        account_labels = Path(os.environ.get("ASPRO_ACCOUNT_LABELS_FILE", str(Path.home()/".config/aspro-export/account-labels.json")))
        if name == "accounts" and account_labels.exists():
            supplemental = json.loads(account_labels.read_text())
            if not isinstance(supplemental, dict) or any(
                    not str(k).isdigit() or not isinstance(v, str) for k, v in supplemental.items()):
                raise ExportError("Invalid supplemental account labels")
            for identifier, label in supplemental.items():
                if identifier not in table:
                    lookups[name][identifier] = label
        log.info("Lookup %s loaded: %d records", name, len(table))
    snapshot = Snapshot(output)
    try:
        completed = []
        for report in reports:
            columns = report["columns"]
            items = client.records(report["path"], report.get("params"))
            if report.get("row_filters"):
                source = items
                filters = report["row_filters"]
                items = (r for r in source if all(str(r.get(k)) == str(v) for k, v in filters.items()))
            if any(c.get("transform") == "task_id" for c in columns):
                items = list(items)
                lookups["task_depths"] = task_depths(items)
            rows = ([transform(item, c, lookups) for c in columns]
                    for item in items)
            count = write_csv(snapshot.stage / report["filename"],
                              [c["header"] for c in columns], rows, report.get("delimiter", ","))
            if count == 0 and not report.get("allow_empty", False):
                raise ExportError("Empty report requires explicit allow_empty: " + report["name"])
            completed.append((report["filename"], [c["header"] for c in columns], count,
                              report.get("delimiter", ",")))
            log.info("Report %s validated: %d records", report["name"], count)
        snapshot.commit(completed)
        log.info("Snapshot published locally; Windows delivery is not configured")
    finally:
        snapshot.close()


def api_key():
    key = os.environ.get("ASPRO_API_KEY")
    if key:
        return key
    path = Path(os.environ.get("ASPRO_KEY_FILE", str(Path.home()/".config/aspro-export/api-key")))
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
        raise ExportError("Key file must be owned by this user with permissions 600")
    os.environ["ASPRO_API_KEY"] = path.read_text().strip()
    return os.environ["ASPRO_API_KEY"]


def main():
    parser = argparse.ArgumentParser(description="Read-only Aspro.Cloud CSV export")
    parser.add_argument("--config", default=str(Path(__file__).with_name("reports.json")))
    parser.add_argument("--output", default=str(Path.home()/".local/state/aspro-export/exported"))
    parser.add_argument("--report", action="append", help="Select a report by configuration name")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Do not allow HTTP-library debug logs to reveal query-string credentials.
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        reports = validate_config(config, args.report)
        if args.check_config:
            log.info("Configuration valid: %d reports", len(reports))
            return 0
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        with (output / ".export.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ExportError("Another export is running") from None
            client = ApiClient(os.environ.get("ASPRO_BASE_URL", config.get("base_url", "")),
                               api_key(),
                               interval=float(os.environ.get("ASPRO_REQUEST_INTERVAL", "1.1")))
            try:
                run(config, reports, output, client)
            finally:
                client.session.close()
        return 0
    except ExportError as error:
        log.error("%s", error)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        log.error("Invalid configuration or local file operation failed; sensitive details suppressed")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
