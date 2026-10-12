import argparse
import fcntl
import json
import logging
import os
from pathlib import Path

from .api import ApiClient, ExportError
from .lookups import LookupTable, join_labels
from .output import Snapshot, write_csv
from .transform import transform, task_depths

log = logging.getLogger("aspro_export")


def validate_config(config, selected):
    reports = [r for r in config["reports"] if not selected or r["name"] in selected]
    if not reports or (selected and set(selected) != {r["name"] for r in reports}):
        raise ExportError("Unknown or empty report selection")
    filenames = set()
    for report in reports:
        if report.get("unresolved"):
            raise ExportError("Report API source unresolved: " + report["name"])
        filename = report["filename"]
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
        if name == "users" and os.environ.get("ASPRO_USER_LABELS_FILE"):
            supplemental = json.loads(Path(os.environ["ASPRO_USER_LABELS_FILE"]).read_text())
            if not isinstance(supplemental, dict) or any(
                    not str(k).isdigit() or not isinstance(v, str) or not v
                    for k, v in supplemental.items()):
                raise ExportError("Invalid supplemental user labels")
            for identifier, label in supplemental.items():
                if not table.get(identifier):
                    lookups[name][identifier] = label
        if name == "accounts" and os.environ.get("ASPRO_ACCOUNT_LABELS_FILE"):
            supplemental = json.loads(Path(os.environ["ASPRO_ACCOUNT_LABELS_FILE"]).read_text())
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
                              [c["header"] for c in columns], rows)
            if count == 0 and not report.get("allow_empty", False):
                raise ExportError("Empty report requires explicit allow_empty: " + report["name"])
            completed.append((report["filename"], [c["header"] for c in columns], count))
            log.info("Report %s validated: %d records", report["name"], count)
        snapshot.commit(completed)
        log.info("Snapshot published locally; Windows delivery is not configured")
    finally:
        snapshot.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only Aspro.Cloud CSV export")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config/reports.json"))
    parser.add_argument("--output", required=True)
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
            client = ApiClient(os.environ.get("ASPRO_BASE_URL", ""),
                               os.environ.get("ASPRO_API_KEY", ""),
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
