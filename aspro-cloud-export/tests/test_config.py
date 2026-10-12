import json
from pathlib import Path

import pytest

from aspro_export.api import ExportError
from aspro_export.__main__ import validate_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_configuration_has_three_ready_reports():
    config = json.loads((ROOT/'config/reports.json').read_text())
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
    config = json.loads((ROOT/'config/pending-gpu.json').read_text())
    assert len(config['reports'][0]['columns']) == 44
    with pytest.raises(ExportError, match='source unresolved'):
        validate_config(config, None)
