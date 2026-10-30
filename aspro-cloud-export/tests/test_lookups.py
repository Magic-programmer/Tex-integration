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
def test_gpu_orders_use_live_projects_and_fetch_new_project_once():
    import json
    from pathlib import Path
    from export_aspro import LookupTable, transform
    config=json.loads((Path(__file__).resolve().parents[1]/'reports.json').read_text())
    column=next(c for r in config['reports'] for c in r['columns'] if c['header']=='Заказ ТЕХ')
    assert column['transform']=='lookup' and column['lookup']=='projects'
    assert 'values' not in column
    class Client:
        calls=[]
        def get(self,path):
            self.calls.append(path)
            return {'id':178,'name':'Synthetic new order'}
    client=Client()
    projects=LookupTable({},client,config['lookups']['projects'])
    for _ in range(2):
        assert transform({'cf_14':'178'},column,{'projects':projects})=='Synthetic new order'
    assert client.calls==['st/projects/get/178']
