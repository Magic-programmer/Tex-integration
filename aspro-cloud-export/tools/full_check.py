"""Download resumable private API snapshots; render and compare offline.

No customer values are printed. Cached pages include query metadata; published CSV files include checksums.
Use --download once; rerun without it to apply changed report mappings locally.
"""
import argparse
import csv
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aspro_export.api import ApiClient, ExportError
from aspro_export.lookups import join_labels
from aspro_export.output import Snapshot, write_csv
from aspro_export.transform import transform, task_depths

REPORTS = ('Контрагенты', 'Сделки', 'Задачи')


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def download(client, cache, name, path, params):
    directory = cache / name
    directory.mkdir(parents=True, exist_ok=True)
    query = {'path': path, 'params': params}
    metadata = directory / 'query.json'
    if metadata.exists() and json.loads(metadata.read_text()) != query:
        raise ExportError('Cache query differs; use a new cache directory')
    save(metadata, query)
    rows, seen, page, expected = [], set(), 1, None
    while True:
        target = directory / f'{page:06d}.json'
        if target.exists():
            result = json.loads(target.read_text())
        else:
            result = client.get(path, {**params, 'limit': 100, 'page': page})
        items, total = result.get('items'), result.get('total_result')
        if total is None and path == 'core/user/list':
            total = result.get('total')
        if (not isinstance(items, list) or type(total) is not int or total < 0 or
                result.get('page') != page or result.get('count') != len(items) or len(items) > 100):
            raise ExportError('Invalid cached/downloaded page metadata')
        if expected is None:
            expected = total
        if expected != total:
            raise ExportError('Dataset count changed; use a new cache for this dataset')
        for row in items:
            identifier = str(row.get('id'))
            if identifier == 'None' or identifier in seen:
                raise ExportError('Missing or duplicate ID in snapshot')
            seen.add(identifier)
        rows.extend(items)
        if len(rows) > total or (not items and len(rows) < total):
            raise ExportError('Incomplete page sequence')
        if not target.exists():
            save(target, result)
        if page == 1 or page % 50 == 0 or len(rows) == total:
            print(f'{name}: {len(rows)}/{total}', flush=True)
        if len(rows) == total:
            save(cache / f'{name}.json', rows)
            save(directory / 'complete.json', {'rows': len(rows), 'completed_utc': datetime.now(timezone.utc).isoformat()})
            return rows
        page += 1


def references(examples):
    result = {}
    for name in REPORTS:
        with (examples / f'{name.lower()}.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            rows = {}
            for row in reader:
                identifier = re.sub(r'^(?:— )+', '', row['ID'])
                if identifier in rows:
                    raise ExportError('Duplicate reference ID')
                rows[identifier] = row
            result[name] = (reader.fieldnames, rows)
    return result


def render(config, cache, output, examples, labels_file):
    reports = [r for r in config['reports'] if r['name'] in REPORTS]
    raw_accounts = json.loads((cache / 'Контрагенты.json').read_text())
    lookups = {}
    supplemental = json.loads(labels_file.read_text()) if labels_file.exists() else {}
    for name, spec in config['lookups'].items():
        records = raw_accounts if name in ('accounts', 'companies') else json.loads((cache / f'{name}.json').read_text())
        values = {}
        for row in records:
            if name == 'companies' and str(row.get('type')) != '1':
                continue
            key, value = str(row[spec.get('key', 'id')]), row[spec['label']]
            if spec.get('many'):
                values.setdefault(key, []).append(value)
            else:
                values[key] = value
        if spec.get('many'):
            values = {k: join_labels(v, spec) for k, v in values.items()}
        if name == 'accounts' and (cache / 'targeted-accounts.json').exists():
            for row in json.loads((cache / 'targeted-accounts.json').read_text()):
                values[str(row['id'])] = row['name']
        if name == 'accounts':
            account_labels = labels_file.with_name('account-labels.json')
            if account_labels.exists():
                for key, value in json.loads(account_labels.read_text()).items():
                    values.setdefault(key, value)
        if name == 'users':
            for key, value in supplemental.items():
                if not values.get(key):
                    values[key] = value
        # Offline fallbacks: only explicitly approved user ID fallback is used.
        if name == 'users':
            class UserLabels(dict):
                def __missing__(self, key):
                    return key
                def __getitem__(self, key):
                    value = super().__getitem__(key)
                    return value if value else key
            lookups[name] = UserLabels(values)
        else:
            lookups[name] = values
    expected = references(examples)
    snapshot = Snapshot(output)
    summaries, differences, completed = [], [], []
    try:
        for report in reports:
            name, columns = report['name'], report['columns']
            headers = [c['header'] for c in columns]
            if headers != expected[name][0]:
                raise ExportError('Reference headers differ: ' + name)
            rows = json.loads((cache / f'{name}.json').read_text())
            if report.get('row_filters'):
                rows = [r for r in rows if all(str(r.get(k)) == str(v)
                        for k, v in report['row_filters'].items())]
            if name == 'Задачи':
                lookups['task_depths'] = task_depths(rows)
            rendered, seen, counts, changed = [], set(), Counter(), 0
            for record in rows:
                identifier = str(record['id'])
                seen.add(identifier)
                actual = {c['header']: transform(record, c, lookups) for c in columns}
                reference = expected[name][1].get(identifier)
                different = False
                if reference:
                    for column, value in actual.items():
                        if value != reference[column]:
                            different = True
                            counts[column] += 1
                            differences.append({'report': name, 'id': identifier, 'column': column,
                                                'actual': value, 'expected': reference[column]})
                changed += different
                rendered.append([actual[c] for c in headers])
            count = write_csv(snapshot.stage / report['filename'], headers, rendered)
            completed.append((report['filename'], headers, count))
            reference_ids = set(expected[name][1])
            summary = {'report': name, 'api_rows': count, 'reference_rows': len(reference_ids),
                       'common_ids': len(seen & reference_ids), 'api_only_ids': len(seen - reference_ids),
                       'reference_only_ids': len(reference_ids - seen), 'differing_rows': changed,
                       'differing_cells': sum(counts.values()), 'columns': dict(counts)}
            summaries.append(summary)
            save(cache / f'{name}-missing-ids.json', sorted(reference_ids - seen))
            save(cache / f'{name}-extra-ids.json', sorted(seen - reference_ids))
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        snapshot.commit(completed)
    finally:
        snapshot.close()
    save(cache / 'comparison.json', summaries)
    save(cache / 'differences.json', differences)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--examples', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=0.2)
    parser.add_argument('--labels', type=Path, default=Path.home()/'.config/aspro-export/user-labels.json')
    args = parser.parse_args()
    os.umask(0o077)
    args.cache.mkdir(parents=True, exist_ok=True)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)
    config = json.loads((Path(__file__).resolve().parents[1]/'config/reports.json').read_text())
    if args.download:
        key = os.environ.get('ASPRO_API_KEY')
        if not key:
            key = (Path.home()/'.config/aspro-export/api-key').read_text().strip()
        os.environ['ASPRO_API_KEY'] = key
        client = ApiClient(os.environ.get('ASPRO_BASE_URL', 'https://gktex.aspro.cloud'), key, interval=args.interval)
        try:
            for report in config['reports']:
                if report['name'] in REPORTS:
                    download(client, args.cache, report['name'], report['path'], report.get('params', {}))
            for name, spec in config['lookups'].items():
                if name not in ('accounts', 'companies'):
                    download(client, args.cache, name, spec['path'], spec.get('params', {}))
        finally:
            client.session.close()
    render(config, args.cache, args.output, args.examples, args.labels)


if __name__ == '__main__':
    try:
        main()
    except ExportError as error:
        print('Full check failed:', error, flush=True)
        raise SystemExit(1)
    except (OSError, ValueError, KeyError, TypeError):
        print('Full check failed; private cache retained. Inspect metadata without printing records.', flush=True)
        raise SystemExit(1)
