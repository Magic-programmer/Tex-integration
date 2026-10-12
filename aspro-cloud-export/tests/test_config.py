import json
from pathlib import Path

import pytest

from export_aspro import ExportError
from export_aspro import validate_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_configuration_has_three_ready_reports():
    config = json.loads((ROOT/'reports.json').read_text())
    reports = validate_config(config, None)
    assert [r['name'] for r in reports] == ['Контрагенты', 'Сделки', 'Задачи']
    assert [len(r['columns']) for r in reports] == [10, 18, 12]
    for report in reports:
        assert len({c['header'] for c in report['columns']}) == len(report['columns'])
        for col in report['columns']:
            if 'lookup' in col:
                assert col['lookup'] in config['lookups']
            assert col.get('field') or col.get('transform') == 'constant'


def test_pending_gpu_cannot_be_started_without_schema():
    config = json.loads((ROOT/'tests/fixtures/pending-gpu.json').read_text())
    assert len(config['reports'][0]['columns']) == 44
    with pytest.raises(ExportError, match='source unresolved'):
        validate_config(config, None)


def test_key_file_loads_into_environment(tmp_path, monkeypatch):
    import os
    from export_aspro import api_key
    monkeypatch.delenv('ASPRO_API_KEY', raising=False)
    path = tmp_path/'key'
    path.write_text('synthetic-test-key\n')
    path.chmod(0o600)
    monkeypatch.setenv('ASPRO_KEY_FILE', str(path))
    assert api_key() == 'synthetic-test-key'
    assert os.environ['ASPRO_API_KEY'] == 'synthetic-test-key'


def test_insecure_key_file_is_rejected(tmp_path, monkeypatch):
    from export_aspro import api_key
    monkeypatch.delenv('ASPRO_API_KEY', raising=False)
    path = tmp_path/'key'
    path.write_text('synthetic-test-key')
    path.chmod(0o644)
    monkeypatch.setenv('ASPRO_KEY_FILE', str(path))
    with pytest.raises(ExportError, match='permissions 600'):
        api_key()


def test_environment_key_has_priority(monkeypatch):
    from export_aspro import api_key
    monkeypatch.setenv('ASPRO_API_KEY', 'synthetic-env-key')
    monkeypatch.setenv('ASPRO_KEY_FILE', '/nonexistent/private-key')
    assert api_key() == 'synthetic-env-key'
