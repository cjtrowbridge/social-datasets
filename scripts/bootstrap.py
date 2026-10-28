#!/usr/bin/env python3
"""Refresh versioned canonical survey snapshots (Python 3.10+)."""
from __future__ import annotations
import argparse
from dataclasses import replace
from contextlib import contextmanager
import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlencode
import zipfile
from sources import SOURCES, discover

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


def safe_path(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or any(p in ('.', '..', '/') or '\\' in p or ':' in p for p in parts):
        raise ValueError(f'Unsafe relative path: {relative}')
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path leaves data directory: {relative}')
    return path


def current_releases(records):
    result = {}
    for path, record in sorted(records.items()):
        series = record['series']
        if not series:
            continue
        vintage = record['vintage']
        entry = result.setdefault(series, {'vintage': vintage, 'paths': []})
        if vintage > entry['vintage']:
            entry.update(vintage=vintage, paths=[])
        if vintage == entry['vintage']:
            entry['paths'].append(path)
    return result


class Client:
    def __init__(self, timeout=120, retries=3):
        self.timeout = timeout
        self.retries = retries
        self.pages = {}
        self.allow_partial = False
        self.discovery_errors = {}
        self.min_free_bytes = 10 * 1024 ** 3

    def open(self, url, headers=None):
        actual_url = url
        if urlsplit(url).hostname == 'api.census.gov' and urlsplit(url).query:
            key = os.environ.get('CENSUS_API_KEY')
            if not key:
                raise RuntimeError('CENSUS_API_KEY is required for Census data queries')
            actual_url += '&' + urlencode({'key': key})
        for attempt in range(self.retries + 1):
            try:
                return urlopen(Request(actual_url, headers={
                    'User-Agent': 'social-datasets/2.0 (public research archive)',
                    'Accept-Encoding': 'identity', **(headers or {}),
                }), timeout=self.timeout)
            except HTTPError as exc:
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == self.retries:
                    # Do not expose the key-bearing request URL in error objects.
                    if actual_url != url:
                        raise HTTPError(url, exc.code, exc.reason, exc.headers, None) from None
                    raise
            except (URLError, TimeoutError):
                if attempt == self.retries:
                    raise
            time.sleep(min(2 ** attempt, 8))

    def text(self, url):
        if url not in self.pages:
            with self.open(url) as response:
                self.pages[url] = response.read().decode('utf-8-sig', errors='replace')
            if '<title>Request Rejected</title>' in self.pages[url]:
                del self.pages[url]
                raise RuntimeError(f'Publisher rejected this request (returned HTML with HTTP 200): {url}')
        return self.pages[url]

    def fetch(self, item, target, old_path, old_record, recheck=False):
        target.parent.mkdir(parents=True, exist_ok=True)
        old_valid = (old_record and old_path.is_file()
                     and old_path.stat().st_size == old_record['bytes']
                     and sha256(old_path) == old_record['sha256'])
        headers = {}
        if old_valid and old_record['url'] == item.url and not recheck:
            if old_record.get('etag'):
                headers['If-None-Match'] = old_record['etag']
            elif old_record.get('last_modified'):
                headers['If-Modified-Since'] = old_record['last_modified']
        try:
            response = self.open(item.url, headers)
        except HTTPError as exc:
            if exc.code == 304 and old_valid and headers:
                link_or_copy(old_path, target)
                return {**old_record, **item.record()}
            raise
        with response, target.open('wb') as output:
            compress = item.product == 'summary' and item.path.lower().endswith('.dat.gz')
            stream = gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) if compress else output
            size = 0
            try:
                while chunk := response.read(CHUNK):
                    if shutil.disk_usage(target.parent).free < self.min_free_bytes:
                        raise OSError('Stopped download to preserve the minimum free disk space')
                    stream.write(chunk)
                    size += len(chunk)
            finally:
                if compress:
                    stream.close()
            expected = response.headers.get('Content-Length')
            if expected is not None and size != int(expected):
                raise IOError(f'Truncated response for {item.url}')
            validators = {'etag': response.headers.get('ETag'),
                          'last_modified': response.headers.get('Last-Modified')}
        if item.product == 'api-tables':
            raw = target.read_bytes()
            if not raw and response.status == 204:
                data = []
            else:
                data = json.loads(raw)
            if urlsplit(item.url).query:
                if not isinstance(data, list) or (data and not all(isinstance(row, list) for row in data)):
                    raise IOError('Invalid Census table response')
                if data:
                    if any(len(row) != len(data[0]) for row in data):
                        raise IOError('Inconsistent Census table columns')
                    data = [data[0]] + sorted(data[1:], key=lambda row: json.dumps(row))
            target.write_bytes(json_bytes(data))
        validate(target)
        digest = sha256(target)
        # Changed HTTP timestamps alone must not produce new repository snapshots.
        if old_valid and digest == old_record['sha256'] and all(
                old_record.get(k) == v for k, v in item.record().items()):
            target.unlink()
            link_or_copy(old_path, target)
            return old_record
        return {**item.record(), **validators, 'bytes': target.stat().st_size, 'sha256': digest}


def validate(path):
    if path.stat().st_size == 0:
        raise IOError(f'Empty response: {path.name}')
    with path.open('rb') as stream:
        prefix = stream.read(1024).lstrip().lower()
    if path.name.lower().endswith('.dat.gz'):
        with gzip.open(path, 'rb') as stream:
            prefix = stream.read(1024).lstrip().lower()
    if path.suffix.lower() not in ('.html', '.htm') and (
            prefix.startswith(b'<!doctype html') or prefix.startswith(b'<html')):
        raise IOError(f'HTML error page returned instead of data: {path.name}')
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            if not archive.namelist() or archive.testzip() is not None:
                raise IOError(f'Invalid ZIP archive: {path.name}')
    elif path.suffix.lower() == '.pdf' and not prefix.startswith(b'%pdf-'):
        raise IOError(f'Invalid PDF: {path.name}')


def link_or_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def rename_with_retry(source, target):
    # Windows indexing and antivirus scanners can briefly hold files open.
    for attempt in range(20):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)


def cached_fetch(client, item, target, old_path, old_record, cache, recheck):
    """Keep completed downloads across failed runs without publishing partial data."""
    entry = cache / hashlib.sha256(item.url.encode()).hexdigest()
    payload = entry / ('payload' + Path(item.path).suffix)
    metadata = entry / 'record.json'
    if payload.exists() and metadata.exists():
        cached = json.loads(metadata.read_text('utf-8'))
        if cached.get('url') == item.url and sha256(payload) == cached.get('sha256'):
            old_path, old_record = payload, cached
    record = client.fetch(item, target, old_path, old_record, recheck)
    entry.mkdir(parents=True, exist_ok=True)
    # Replacing the cache link must not mutate an inode shared with latest.
    if not payload.exists() or sha256(payload) != record['sha256']:
        pending = entry / 'payload.pending'
        pending.unlink(missing_ok=True)
        link_or_copy(target, pending)
        pending.replace(payload)
    if not metadata.exists() or metadata.read_bytes() != json_bytes(record):
        pending = entry / 'record.pending'
        pending.write_bytes(json_bytes(record))
        pending.replace(metadata)
    return record


@contextmanager
def lock(path, wait_seconds=0):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Another update may be active: {path}. If interrupted, '
                                   'confirm it has stopped before removing this lock.') from None
            time.sleep(1)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def refresh(source, items, data_root, client, products, recheck=False, allow_partial=False, wait_seconds=0,
            keep_missing=False):
    """Stage all selected products; leave latest untouched on download failure."""
    base = data_root / source
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.resolve().is_relative_to(data_root.resolve()):
        raise RuntimeError(f'Survey directory must stay inside the data root: {base}')
    latest = base / 'latest'
    backup = base / '.previous'
    # Check resolved targets before recursive cleanup or directory moves.
    for path in (latest, backup, base / '.cache'):
        if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
            raise RuntimeError(f'Unsafe survey directory: {path}')
    with lock(base / '.update.lock', wait_seconds):
        # All deletion targets are fixed children of this survey's data directory.
        if backup.exists() and not latest.exists():
            rename_with_retry(backup, latest)
        elif backup.exists():
            shutil.rmtree(backup)
        catalog_path = latest / 'catalog.json'
        old = json.loads(catalog_path.read_text('utf-8')) if catalog_path.exists() else {
            'schema_version': 1, 'source': source, 'products': [], 'files': {}}
        if latest.exists():
            untracked = {p.relative_to(latest).as_posix() for p in latest.rglob('*') if p.is_file()}
            untracked -= set(old['files']) | {'catalog.json'}
            if untracked:
                raise RuntimeError(f'Uncataloged files in {latest}; move them before refreshing: '
                                   + ', '.join(sorted(untracked)[:5]))
        paths = {item.path for item in items}
        missing = [p for p, r in old['files'].items() if r['product'] in products and p not in paths]
        if missing and not keep_missing and (not allow_partial or any(
                old['files'][path]['product'] not in client.discovery_errors for path in missing)):
            raise RuntimeError('Discovery lost cataloged files; latest is unchanged: '
                               + ', '.join(missing[:5]))
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=base) as temporary:
            stage = Path(temporary) / 'latest'
            stage.mkdir()
            records = {}
            for path, record in old['files'].items():
                if record['product'] not in products or path in missing:
                    previous = safe_path(latest, path)
                    if not previous.is_file() or sha256(previous) != record['sha256']:
                        raise RuntimeError(f'Unselected file is corrupt or is an LFS pointer: {previous}')
                    link_or_copy(previous, safe_path(stage, path))
                    records[path] = record
            for index, item in enumerate(items, 1):
                print(f'[{source} {index}/{len(items)}] {item.path}', flush=True)
                target = safe_path(stage, item.path)
                try:
                    for candidate_url in (item.url, *item.alternates):
                        candidate = replace(item, url=candidate_url, alternates=())
                        try:
                            records[item.path] = cached_fetch(client, candidate, target,
                                safe_path(latest, item.path), old['files'].get(item.path), base / '.cache', recheck)
                            break
                        except (HTTPError, URLError, OSError, ValueError, zipfile.BadZipFile) as exc:
                            if candidate_url == (item.alternates[-1] if item.alternates else item.url):
                                raise
                            if 'minimum free disk space' in str(exc):
                                raise
                            target.unlink(missing_ok=True)
                            print(f'Trying alternative after failure: {candidate_url}', file=sys.stderr, flush=True)
                except (HTTPError, URLError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    if not allow_partial:
                        raise
                    target.unlink(missing_ok=True)
                    if 'minimum free disk space' in str(exc):
                        print('Disk reserve reached; publishing completed files and recording remaining gaps.',
                              file=sys.stderr, flush=True)
                        for skipped in items[index - 1:]:
                            client.discovery_errors.setdefault(skipped.product, []).append(
                                {'url': skipped.url, 'error': 'not downloaded: minimum free disk space'})
                            previous = safe_path(latest, skipped.path)
                            record = old['files'].get(skipped.path)
                            if record and previous.is_file() and sha256(previous) == record['sha256']:
                                link_or_copy(previous, safe_path(stage, skipped.path))
                                records[skipped.path] = record
                        break
                    client.discovery_errors.setdefault(item.product, []).append(
                        {'url': item.url, 'error': str(exc)})
                    print(f'Skipping failed download {item.url}: {exc}', file=sys.stderr, flush=True)
                    previous = safe_path(latest, item.path)
                    record = old['files'].get(item.path)
                    if record and previous.is_file() and sha256(previous) == record['sha256']:
                        link_or_copy(previous, target)
                        records[item.path] = record
            if not records:
                raise RuntimeError('No files downloaded; latest is unchanged')
            incomplete = dict(old.get('incomplete', {}))
            for product in products:
                incomplete.pop(product, None)
                if client.discovery_errors.get(product):
                    incomplete[product] = sorted(client.discovery_errors[product], key=lambda e: e['url'])
            catalog = {'schema_version': 1, 'source': source,
                       'products': sorted(set(old['products']) | set(products)),
                       'current': current_releases(records), 'incomplete': incomplete, 'files': records}
            contents = json_bytes(catalog)
            (stage / 'catalog.json').write_bytes(contents)
            if catalog_path.exists() and catalog_path.read_bytes() == contents and all(
                    safe_path(latest, p).is_file() and sha256(safe_path(latest, p)) == r['sha256']
                    for p, r in records.items()):
                print(f'{source}: unchanged', flush=True)
                return
            if latest.exists():
                rename_with_retry(latest, backup)
            try:
                rename_with_retry(stage, latest)
            except BaseException:
                if backup.exists():
                    rename_with_retry(backup, latest)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            print(f'{source}: published {len(records)} files in {latest}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=['all', *SOURCES], default='all')
    parser.add_argument('--product', action='append', help='Only this product; repeat to select several')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--list', '--plan', dest='plan', action='store_true', help='Discover without downloading')
    parser.add_argument('--products', action='store_true', help='List supported products without network access')
    parser.add_argument('--recheck', action='store_true', help='Ignore HTTP validators and compare downloaded content')
    parser.add_argument('--allow-partial', action='store_true', help='Publish accessible files and record missing files; exit nonzero')
    parser.add_argument('--min-free-gib', type=float, default=10, help='Minimum free disk space to preserve (default: 10 GiB)')
    parser.add_argument('--wait-for-lock', type=int, default=0, metavar='SECONDS', help='Wait for another refresh of the same survey')
    parser.add_argument('--checkpoint-years', action='store_true', help='Publish ACS PUMS one year at a time (requires --allow-partial)')
    args = parser.parse_args(argv)
    if args.checkpoint_years and (args.source != 'acs' or args.product != ['pums'] or not args.allow_partial):
        parser.error('--checkpoint-years requires --source acs --product pums --allow-partial')
    sources = list(SOURCES) if args.source == 'all' else [args.source]
    if args.products:
        for source in sources:
            for product in SOURCES[source]:
                print(f'{source}: {product}')
        return 0
    available = {p for s in sources for p in SOURCES[s]}
    if args.product and set(args.product) - available:
        parser.error('Unknown product(s): ' + ', '.join(sorted(set(args.product) - available)))
    client = Client()
    client.allow_partial = args.allow_partial
    if args.min_free_gib < 0:
        parser.error('--min-free-gib must be nonnegative')
    if args.wait_for_lock < 0:
        parser.error('--wait-for-lock must be nonnegative')
    client.min_free_bytes = int(args.min_free_gib * 1024 ** 3)
    failures = []
    for source in sources:
        products = [p for p in SOURCES[source] if not args.product or p in args.product]
        if not products:
            continue
        try:
            client.discovery_errors = {}
            items = discover(source, products, client)
            if args.plan:
                print(json_bytes({'source': source, 'files': [i.record() | {'path': i.path} for i in items]}).decode(), end='')
            elif args.checkpoint_years:
                discovery_errors = {k: list(v) for k, v in client.discovery_errors.items()}
                years = sorted({i.path.split('/')[1] for i in items if len(i.path.split('/')) > 2}, reverse=True)
                for index, year in enumerate(years):
                    batch = [i for i in items if i.path.startswith(f'pums/{year}/')]
                    client.discovery_errors = {k: list(v) for k, v in discovery_errors.items()}
                    if index + 1 < len(years):
                        client.discovery_errors.setdefault('pums', []).append({
                            'url': 'https://www2.census.gov/programs-surveys/acs/data/pums/',
                            'error': 'pending years: ' + ', '.join(years[index + 1:])})
                    print(f'Publishing ACS PUMS year {year} ({index + 1}/{len(years)})', flush=True)
                    refresh(source, batch, args.data_dir.resolve(), client, products,
                            args.recheck, True, args.wait_for_lock, keep_missing=True)
                    # Preserve errors encountered during a batch, excluding only the
                    # temporary pending-years marker, for the final catalog.
                    discovery_errors = {k: [v for v in values if not v['error'].startswith('pending years: ')]
                                        for k, values in client.discovery_errors.items()}
                    discovery_errors = {k: v for k, v in discovery_errors.items() if v}
                client.discovery_errors = discovery_errors
            else:
                refresh(source, items, args.data_dir.resolve(), client, products, args.recheck, args.allow_partial, args.wait_for_lock)
            if client.discovery_errors:
                failures.append(source)
                print(f'{source}: incomplete; see catalog for missing publisher files', file=sys.stderr)
        except Exception as exc:
            failures.append(source)
            print(f'{source}: FAILED: {exc}', file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
