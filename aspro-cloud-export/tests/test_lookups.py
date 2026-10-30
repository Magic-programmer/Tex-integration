import pytest

from export_aspro import ApiApplicationError, ExportError
from export_aspro import LookupTable


def test_live_select_labels_override_fallback_and_accept_new_options():
    from export_aspro import transform
    column={'field':'cf_17','header':'Производитель','transform':'enum',
            'values':{'1':'Old name'},'live_options_field_id':17}
    tables={'field_options':{'17':{'1':'Current name','2':'New option'}}}
    assert transform({'cf_17':1},column,tables)=='Current name'
    assert transform({'cf_17':2},column,tables)=='New option'
    assert column['values']=={'1':'Old name'}


def test_live_options_access_denied_uses_explicit_fallback():
    from export_aspro import ApiModuleAccessError, load_live_options
    class Client:
        def records(self,*args):raise ApiModuleAccessError('denied')
    assert load_live_options({'live_options':{'path':'customfields/fieldoptions/list'}},
                            [{'columns':[{'live_options_field_id':17}]}],Client())=={}


def test_live_options_transport_errors_are_not_hidden():
    from export_aspro import load_live_options
    class Client:
        def records(self,*args):raise ExportError('transport failure')
    with pytest.raises(ExportError,match='transport failure'):
        load_live_options({'live_options':{'path':'customfields/fieldoptions/list'}},
                          [{'columns':[{'live_options_field_id':17}]}],Client())


def test_live_options_keep_field_namespaces_separate():
    from export_aspro import load_live_options
    class Client:
        def records(self,path,params):
            assert params['filter[field_id]']=='17,23'
            return iter([{'id':1,'field_id':17,'value':'Manufacturer'},
                         {'id':2,'field_id':23,'value':'Engine'}])
    result=load_live_options({'live_options':{'path':'customfields/fieldoptions/list'}},
                            [{'columns':[{'live_options_field_id':17},{'live_options_field_id':23}]}],Client())
    assert result=={'17':{'1':'Manufacturer'},'23':{'2':'Engine'}}


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


def test_live_get_has_priority_over_historical_label():
    class Client:
        def get(self,path):return {'id':2,'name':'Current system name'}
    table=LookupTable({},Client(),{'get_path':'core/user/get/{id}','label':'name'})
    table.fallback_labels={'2':'Historical name'}
    assert table['2']=='Current system name'


def test_historical_label_is_used_only_after_live_get_is_unavailable():
    class Client:
        def get(self,path):raise ApiApplicationError('unavailable')
    table=LookupTable({},Client(),{'get_path':'crm/account/get/{id}','label':'name'})
    table.fallback_labels={'2':'Historical name'}
    assert table['2']=='Historical name'


def test_historical_label_does_not_hide_transport_error():
    class Client:
        def get(self,path):raise ExportError('network failure')
    table=LookupTable({},Client(),{'get_path':'crm/account/get/{id}','label':'name'})
    table.fallback_labels={'2':'Historical name'}
    with pytest.raises(ExportError,match='network failure'):
        table['2']


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
