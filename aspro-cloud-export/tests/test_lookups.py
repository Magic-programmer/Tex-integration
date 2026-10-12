import pytest

from export_aspro import ApiApplicationError, ExportError
from export_aspro import LookupTable


def test_individual_user_get_and_cache():
    class Client:
        calls = []

        def get(self, path):
            self.calls.append(path)
            return {"id": 2, "name": "Synthetic user"}
    client = Client()
    table = LookupTable({"1": "Known user"}, client,
                        {"get_path": "core/user/get/{id}", "label": "name"})
    assert table["1"] == "Known user"
    assert table["2"] == "Synthetic user"
    assert table["2"] == "Synthetic user"
    assert client.calls == ["core/user/get/2"]


def test_unavailable_user_stops_instead_of_fabricating_name():
    class Client:
        def get(self, path):
            raise ExportError("Synthetic API application error")
    table = LookupTable({}, Client(), {"get_path": "core/user/get/{id}", "label": "name"})
    with pytest.raises(ExportError):
        table["2"]
    assert not table


def test_authorized_original_id_fallback_is_cached():
    class Client:
        calls = 0

        def get(self, path):
            self.calls += 1
            raise ApiApplicationError("Synthetic unavailable user")
    client = Client()
    table = LookupTable({}, client, {"get_path": "core/user/get/{id}", "label": "name",
                                     "unavailable_label": "id"})
    assert table["2"] == "2"
    assert table["2"] == "2"
    assert client.calls == 1


def test_id_fallback_does_not_hide_transport_failure():
    class Client:
        def get(self, path):
            raise ExportError("Synthetic transport failure")
    table = LookupTable({}, Client(), {"get_path": "core/user/get/{id}", "label": "name",
                                      "unavailable_label": "id"})
    with pytest.raises(ExportError):
        table["2"]


def test_id_fallback_for_explicit_empty_name():
    class Client:
        def get(self, path):
            return {"id": 2, "name": None}
    table = LookupTable({}, Client(), {"get_path": "core/user/get/{id}", "label": "name",
                                      "unavailable_label": "id"})
    assert table["2"] == "2"


def test_id_fallback_for_empty_name_in_list():
    table = LookupTable({"2": None}, None, {"unavailable_label": "id"})
    assert table["2"] == "2"


def test_email_list_cleanup_preserves_readable_original_values():
    from export_aspro import join_labels
    spec = {'separator': ', ', 'unique': True, 'clean_email_list': True}
    assert join_labels([' INFO@EXAMPLE.INVALID ', '', '\ufeffinfo@example.invalid',
                        'INFO@EXAMPLE.INVALID', "'quoted@example.invalid'", '<bad@value>'], spec) == (
        "INFO@EXAMPLE.INVALID, info@example.invalid, 'quoted@example.invalid', <bad@value>")
