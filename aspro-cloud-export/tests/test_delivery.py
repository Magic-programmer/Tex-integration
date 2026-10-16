import json
import shlex
import subprocess
from pathlib import Path

import pytest

from export_aspro import ExportError, SmbDelivery, Snapshot, write_csv, smb_quote


class FakeSmb:
    def __init__(self):
        self.files = {}
        self.commands = []
        self.corrupt_upload = False
        self.fail_first_put = False
        self.fail_rename = False
        self.lose_rename_response = False

    def __call__(self, args, **kwargs):
        self.commands.append(args)
        tokens = shlex.split(args[-1])
        op, *names = tokens
        error = False
        if op == 'put':
            if self.fail_first_put:
                self.fail_first_put = False
                error = True
            else:
                self.files[names[1]] = b'corrupt' if self.corrupt_upload else Path(names[0]).read_bytes()
        elif op == 'get':
            if names[0] not in self.files:
                error = True
            else:
                Path(names[1]).write_bytes(self.files[names[0]])
        elif op == 'rename':
            if self.fail_rename:
                error = True
            else:
                self.files[names[1]] = self.files.pop(names[0])
                error = self.lose_rename_response
        elif op == 'del':
            error = self.files.pop(names[0], None) is None
        return subprocess.CompletedProcess(args, 1 if error else 0,
                                           b'NT_STATUS_UNSUCCESSFUL' if error else b'', b'')


@pytest.fixture
def setup_delivery(tmp_path, monkeypatch):
    credentials = tmp_path/'credentials'
    credentials.write_text('username = synthetic\npassword = synthetic\ndomain = TEST\n')
    credentials.chmod(0o600)
    monkeypatch.setenv('ASPRO_SMB_CREDENTIALS_FILE', str(credentials))
    reports = [{'name':'Test','filename':name,'columns':[{'header':'ID'}]} for name in ['Первый.csv','Второй.csv']]
    snapshot = Snapshot(tmp_path/'output')
    for r in reports:
        write_csv(snapshot.stage/r['filename'], ['ID'], [['123']])
    snapshot.commit([(r['filename'], ['ID'], 1) for r in reports])
    snapshot.close()
    fake = FakeSmb()
    delivery = SmbDelivery({'share':'//server.invalid/reports','directory':'Work/Тест'}, execute=fake, sleep=lambda seconds:None)
    return delivery, fake, tmp_path/'output', reports, credentials


def test_verified_files_replace_old_reports_and_manifest_last(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    for r in reports:
        fake.files['\\Work\\Тест\\'+r['filename']] = b'old report'
    delivery.deliver(output, reports)
    replacements = [shlex.split(args[-1]) for args in fake.commands if args[-1].startswith('rename ')]
    assert replacements[-1][2].endswith('manifest.json')
    assert all(tokens[-1] == '-f' for tokens in replacements)
    assert not any('.tmp' in name for name in fake.files)
    for r in reports:
        assert fake.files['\\Work\\Тест\\'+r['filename']] == (output/'current'/r['filename']).read_bytes()
    for args in fake.commands:
        assert '--client-protection=encrypt' in args
        assert '--option=client min protocol=SMB3' in args
        assert 'synthetic' not in ' '.join(args)


def test_report_subdirectories_and_remote_manifest_paths(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    delivery.subdirectories = {r['filename']:Path(r['filename']).stem for r in reports}
    delivery.deliver(output, reports)
    for report in reports:
        name = report['filename']
        assert fake.files['\\Work\\Тест\\'+Path(name).stem+'\\'+name] == (output/'current'/name).read_bytes()
        assert '\\Work\\Тест\\'+name not in fake.files
    remote = json.loads(fake.files['\\Work\\Тест\\manifest.json'])
    assert [e['file'] for e in remote['reports']] == ['Первый/Первый.csv','Второй/Второй.csv']
    assert [e['file'] for e in json.loads((output/'current/manifest.json').read_text())['reports']] == ['Первый.csv','Второй.csv']


@pytest.mark.parametrize('folder', ['..', '../other', 'x\\y', 'x;del *', 'x:y'])
def test_invalid_subdirectory_is_rejected(folder):
    with pytest.raises(ExportError):
        SmbDelivery({'share':'//server.invalid/share','directory':'Work','report_subdirectories':{'report.csv':folder}})


def test_corrupt_upload_does_not_replace_any_report(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    fake.corrupt_upload = True
    with pytest.raises(ExportError, match='checksum mismatch'):
        delivery.deliver(output, reports)
    assert not any(args[-1].startswith('rename ') for args in fake.commands)
    assert not fake.files


def test_local_corruption_fails_before_network(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    (output/'current'/reports[0]['filename']).write_text('ID\n456\n')
    with pytest.raises(ExportError, match='checksum mismatch'):
        delivery.deliver(output, reports)
    assert not fake.commands


def test_incomplete_snapshot_cannot_be_delivered(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    with pytest.raises(ExportError, match='complete configured'):
        delivery.deliver(output, reports[:1])
    assert not fake.commands


def test_insecure_credentials_rejected(setup_delivery):
    delivery, fake, output, reports, credentials = setup_delivery
    credentials.chmod(0o644)
    with pytest.raises(ExportError, match='permissions 600'):
        delivery.deliver(output, reports)
    assert not fake.commands


def test_temporary_upload_failure_is_retried(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    fake.fail_first_put = True
    delivery.deliver(output, reports)
    assert len([a for a in fake.commands if a[-1].startswith('put ')]) == 4


def test_publication_failure_does_not_publish_success_manifest(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    fake.fail_rename = True
    with pytest.raises(ExportError):
        delivery.deliver(output, reports)
    assert '\\Work\\Тест\\manifest.json' not in fake.files
    assert not any('.tmp' in name for name in fake.files)


def test_lost_successful_rename_response_is_verified(setup_delivery):
    delivery, fake, output, reports, _ = setup_delivery
    fake.lose_rename_response = True
    delivery.deliver(output, reports)
    assert '\\Work\\Тест\\manifest.json' in fake.files


def test_delivery_only_does_not_create_api_client(setup_delivery, monkeypatch, tmp_path):
    import export_aspro
    delivery, fake, output, reports, _ = setup_delivery
    for report in reports:
        report.update({'path':'crm/account/list'})
        report['columns'][0]['field'] = 'id'
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'reports':reports,'delivery':{'share':'//server.invalid/reports','directory':'Work/Тест'}}))
    monkeypatch.setattr(export_aspro, 'SmbDelivery', lambda settings:delivery)
    monkeypatch.setattr(export_aspro, 'ApiClient', lambda *a, **k:pytest.fail('API must not be called'))
    monkeypatch.setattr('sys.argv', ['export_aspro.py','--config',str(config),'--output',str(output),'--deliver-only'])
    assert export_aspro.main() == 0


def test_default_run_reports_delivery_failure(setup_delivery, monkeypatch, tmp_path):
    import export_aspro
    from types import SimpleNamespace
    delivery, fake, output, reports, _ = setup_delivery
    fake.fail_rename = True
    for report in reports:
        report.update({'path':'crm/account/list'})
        report['columns'][0]['field'] = 'id'
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'reports':reports,'delivery':{'share':'//server.invalid/reports','directory':'Work/Тест'}}))
    monkeypatch.setattr(export_aspro, 'SmbDelivery', lambda settings:delivery)
    monkeypatch.setattr(export_aspro, 'api_key', lambda:'synthetic-test-key')
    monkeypatch.setattr(export_aspro, 'ApiClient', lambda *a, **k:SimpleNamespace(session=SimpleNamespace(close=lambda:None)))
    monkeypatch.setattr(export_aspro, 'run', lambda *a:None)
    monkeypatch.setattr('sys.argv', ['export_aspro.py','--config',str(config),'--output',str(output)])
    assert export_aspro.main() == 1
    assert '\\Work\\Тест\\manifest.json' not in fake.files


def test_pending_delivery_retries_without_api_and_skips_after_success(setup_delivery, monkeypatch, tmp_path):
    import export_aspro
    delivery, fake, output, reports, _ = setup_delivery
    for report in reports:
        report.update({'path':'crm/account/list'})
        report['columns'][0]['field'] = 'id'
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'reports':reports,'delivery':{'share':'//server.invalid/reports','directory':'Work/Тест'}}))
    monkeypatch.setattr(export_aspro, 'SmbDelivery', lambda settings:delivery)
    monkeypatch.setattr(export_aspro, 'ApiClient', lambda *a, **k:pytest.fail('Retry must not access API'))
    monkeypatch.setattr('sys.argv', ['export_aspro.py','--config',str(config),'--output',str(output),'--deliver-pending'])
    fake.fail_rename = True
    assert export_aspro.main() == 1
    assert not (output/'.delivery-completed.json').exists()
    fake.fail_rename = False
    assert export_aspro.main() == 0
    assert (output/'.delivery-completed.json').stat().st_mode & 0o777 == 0o600
    previous_calls = len(fake.commands)
    assert export_aspro.main() == 0
    assert len(fake.commands) == previous_calls


def test_delivery_marker_is_invalidated_by_destination_or_manifest(setup_delivery):
    from export_aspro import delivery_completed, record_delivery
    _, _, output, _, _ = setup_delivery
    config = {'delivery':{'share':'//server.invalid/reports','directory':'Work'}}
    record_delivery(config, output)
    assert delivery_completed(config, output)
    assert not delivery_completed({'delivery':{'share':'//server.invalid/other','directory':'Work'}}, output)
    manifest = output/'current/manifest.json'
    manifest.write_text(manifest.read_text()+'\n')
    assert not delivery_completed(config, output)


def test_smb_failure_exposes_status_but_never_raw_response(setup_delivery):
    delivery, _, _, _, _ = setup_delivery
    delivery.execute = lambda args, **kwargs:subprocess.CompletedProcess(args,1,b'NT_STATUS_ACCESS_DENIED private-response-must-not-leak',b'')
    with pytest.raises(ExportError, match='NT_STATUS_ACCESS_DENIED') as error:
        delivery.command('put "local" "remote"', attempts=1)
    assert 'private-response' not in str(error.value)


@pytest.mark.parametrize('value', ['x;del *', 'x"y', 'x\ny'])
def test_smb_command_injection_rejected(value):
    with pytest.raises(ExportError):
        smb_quote(value)
