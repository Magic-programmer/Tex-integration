import importlib.util
from pathlib import Path

import pytest
from export_aspro import ExportError

spec = importlib.util.spec_from_file_location('full_check', Path(__file__).resolve().parents[1]/'tools/full_check.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def get(self, path, params):
        self.calls.append(params['page'])
        return self.pages[params['page']]


def page(number, total, ids):
    return {'page': number, 'total_result': total, 'count': len(ids),
            'items': [{'id': value} for value in ids]}


def test_download_reuses_completed_pages(tmp_path):
    client = Client({1: page(1, 2, [1]), 2: page(2, 2, [2])})
    assert len(module.download(client, tmp_path, 'sample', 'crm/account/list', {})) == 2
    cached = Client({})
    assert len(module.download(cached, tmp_path, 'sample', 'crm/account/list', {})) == 2
    assert not cached.calls


def test_count_drift_does_not_mark_complete(tmp_path):
    client = Client({1: page(1, 2, [1]), 2: page(2, 3, [2])})
    with pytest.raises(ExportError):
        module.download(client, tmp_path, 'sample', 'crm/account/list', {})
    assert not (tmp_path/'sample/complete.json').exists()
    assert not (tmp_path/'sample.json').exists()


def test_duplicate_pages_do_not_mark_complete(tmp_path):
    client = Client({1: page(1, 2, [1]), 2: page(2, 2, [1])})
    with pytest.raises(ExportError):
        module.download(client, tmp_path, 'sample', 'crm/account/list', {})
    assert not (tmp_path/'sample/complete.json').exists()


def test_gpu_alignment_requires_count_and_anchor_matches():
    records = [{'id': 2, 'cf_16': '20', 'cf_21': 'Synthetic address B', 'cf_22': '200'},
               {'id': 1, 'cf_16': '10', 'cf_21': 'Synthetic address A', 'cf_22': '100'}]
    reference = [{'№ пп': '10', 'Адрес установки ': 'Synthetic address A', 'Мощность ГПУ (кВт)': '100'},
                 {'№ пп': '20', 'Адрес установки ': 'Synthetic address B', 'Мощность ГПУ (кВт)': '200'}]
    assert set(module.align_gpu_rows(records, reference)) == {'1', '2'}
    with pytest.raises(ExportError):
        module.align_gpu_rows(records, reference[:1])
    reference[1]['Адрес установки '] = 'Changed address'
    with pytest.raises(ExportError):
        module.align_gpu_rows(records, reference)
