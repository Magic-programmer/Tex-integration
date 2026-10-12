import csv

import pytest

from export_aspro import ExportError
from export_aspro import Snapshot, validate_csv, write_csv
from export_aspro import run, validate_config


def test_csv_bom_quoting_and_newlines(tmp_path):
    path = tmp_path / "report.csv"
    rows = [["00123", 'Synthetic "value", line\nnext'], ["0", ""]]
    headers = ["ID", "Название "]
    assert write_csv(path, headers, rows) == 2
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [headers, *rows]
    validate_csv(path, headers, 2)


def publish(root, value):
    snapshot = Snapshot(root)
    write_csv(snapshot.stage / "report.csv", ["ID"], [[value]])
    snapshot.commit([("report.csv", ["ID"], 1)])
    snapshot.close()


def test_atomic_generation_and_retention(tmp_path):
    for n in range(3):
        publish(tmp_path, str(n))
    assert (tmp_path / "current/report.csv").read_text(encoding="utf-8-sig") == "ID\n2\n"
    assert len(list((tmp_path / "generations").iterdir())) == 2


def test_invalid_report_preserves_previous_snapshot(tmp_path):
    publish(tmp_path, "previous")
    snapshot = Snapshot(tmp_path)
    write_csv(snapshot.stage / "report.csv", ["Wrong"], [["new"]])
    with pytest.raises(ExportError):
        snapshot.commit([("report.csv", ["ID"], 1)])
    snapshot.close()
    assert "previous" in (tmp_path / "current/report.csv").read_text(encoding="utf-8-sig")


def test_midstream_failure_does_not_publish(tmp_path):
    publish(tmp_path, "previous")
    class BrokenClient:
        def records(self, *args):
            yield {"id": 1}
            raise ExportError("Synthetic page failure")
    report = {"name": "Test", "filename": "report.csv", "path": "crm/account/list",
              "columns": [{"header": "ID", "field": "id"}]}
    with pytest.raises(ExportError):
        run({"lookups": {}}, [report], tmp_path, BrokenClient())
    assert "previous" in (tmp_path / "current/report.csv").read_text(encoding="utf-8-sig")
    assert not list((tmp_path / "generations").glob(".pending-*"))


def test_second_report_failure_does_not_publish_first_report(tmp_path):
    publish(tmp_path, "previous")
    class Client:
        def records(self, path, params=None):
            if path == "crm/account/list":
                yield {"id": 1}
            else:
                raise ExportError("Synthetic failure in second report")
    reports = [{"name": name, "filename": filename, "path": path,
                "columns": [{"header": "ID", "field": "id"}]}
               for name, filename, path in [("First", "report.csv", "crm/account/list"),
                                            ("Second", "second.csv", "crm/lead/list")]]
    with pytest.raises(ExportError):
        run({"lookups": {}}, reports, tmp_path, Client())
    assert "previous" in (tmp_path / "current/report.csv").read_text(encoding="utf-8-sig")
    assert not (tmp_path / "current/second.csv").exists()
    assert not list((tmp_path / "generations").glob(".pending-*"))


def test_unresolved_mapping_and_traversal_blocked():
    report = {"name": "Test", "filename": "report.csv", "path": "crm/account/list",
              "columns": [{"header": "Unknown", "unresolved": "No documented field"}]}
    with pytest.raises(ExportError):
        validate_config({"reports": [report]}, None)
    report["filename"] = "../report.csv"
    with pytest.raises(ExportError):
        validate_config({"reports": [report]}, None)


def test_multiple_emails_preserve_order_and_empty_accounts(tmp_path):
    class Client:
        def records(self, path, params=None):
            if path == "crm/emails/list":
                yield {"id": 1, "account_id": 7, "email": "first@example.invalid"}
                yield {"id": 2, "account_id": 7, "email": "second@example.invalid"}
            else:
                yield {"id": 7}
                yield {"id": 8}
    config = {"lookups": {"emails": {"path": "crm/emails/list", "key": "account_id",
              "label": "email", "many": True, "separator": ", "}}}
    report = {"name": "Test", "filename": "report.csv", "path": "crm/account/list",
              "columns": [{"header": "E-mail", "field": "id", "transform": "lookup",
                           "lookup": "emails", "empty_if_missing": True}]}
    run(config, [report], tmp_path, Client())
    with (tmp_path / "current/report.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.reader(handle)) == [["E-mail"],
                ["first@example.invalid, second@example.invalid"], [""]]
