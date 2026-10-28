import contextlib
import io
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bootstrap import Client, current_releases, refresh, safe_path, sha256
from sources import Artifact, CENSUS, child_links, gss_microdata, pums_select, summary_select, atus_microdata, BLS, ACS_2019_FIVE_YEAR, files_under
from census_api import Geographies, api_tables
import bootstrap


class Response(io.BytesIO):
    status = 200
    def __init__(self, contents, headers=None):
        super().__init__(contents)
        self.headers = headers or {}


class FakeClient(Client):
    def __init__(self, contents=b'first release', headers=None):
        super().__init__()
        self.contents = contents
        self.headers = headers or {}
        self.requests = []
        self.fail_url = None
    def open(self, url, headers=None):
        self.requests.append((url, headers or {}))
        if url == self.fail_url:
            raise OSError('network interrupted')
        return Response(self.contents, self.headers)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.item = Artifact('microdata/cumulative.txt', 'https://example.org/data',
                             'microdata', 'Historical data', 'cumulative', 2024)
        self.client = FakeClient()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.temp.cleanup()
    def run_refresh(self, items=None, products=None):
        refresh('gss', items or [self.item], self.root, self.client, products or ['microdata'])
    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file() and '.cache' not in p.parts}
    def test_repeat_is_byte_and_mtime_identical_even_when_http_headers_change(self):
        self.run_refresh()
        before = self.snapshot()
        self.client.headers = {'Last-Modified': 'a later server timestamp'}
        self.run_refresh()
        self.assertEqual(before, self.snapshot())
    def test_revision_updates_same_canonical_path(self):
        self.run_refresh()
        self.client.contents = b'corrected historical release'
        self.run_refresh()
        path = self.root / 'gss/latest/microdata/cumulative.txt'
        self.assertEqual(path.read_bytes(), self.client.contents)
        catalog = json.loads((path.parents[1] / 'catalog.json').read_text())
        self.assertEqual(catalog['files']['microdata/cumulative.txt']['sha256'], sha256(path))
    def test_failure_leaves_published_snapshot_unchanged(self):
        self.run_refresh()
        before = self.snapshot()
        other = Artifact('microdata/new.txt', 'https://example.org/broken', 'microdata')
        self.client.contents = b'changed first file'
        self.client.fail_url = other.url
        with self.assertRaises(OSError):
            self.run_refresh([self.item, other])
        self.assertEqual(before, self.snapshot())

    def test_partial_mode_publishes_successes_and_records_failures(self):
        other = Artifact('microdata/broken.txt', 'https://example.org/broken', 'microdata')
        self.client.fail_url = other.url
        refresh('gss', [self.item, other], self.root, self.client, ['microdata'], allow_partial=True)
        latest = self.root / 'gss/latest'
        catalog = json.loads((latest / 'catalog.json').read_text())
        self.assertTrue((latest / self.item.path).exists())
        self.assertFalse((latest / other.path).exists())
        self.assertEqual(catalog['incomplete']['microdata'][0]['url'], other.url)

    def test_alternate_format_is_used_when_preferred_link_fails(self):
        item = Artifact('microdata/data.txt', 'https://example.org/broken', 'microdata',
                        alternates=('https://example.org/alternate',))
        self.client.fail_url = item.url
        self.run_refresh([item])
        catalog = json.loads((self.root / 'gss/latest/catalog.json').read_text())
        self.assertEqual(catalog['files'][item.path]['url'], item.alternates[0])

    def test_partial_discovery_preserves_previously_downloaded_file(self):
        other = Artifact('microdata/other.txt', 'https://example.org/other', 'microdata')
        self.run_refresh([self.item, other])
        self.client.discovery_errors = {'microdata': [{'url':'https://example.org/missing/', 'error':'unavailable'}]}
        refresh('gss', [self.item], self.root, self.client, ['microdata'], allow_partial=True)
        self.assertTrue((self.root / 'gss/latest' / other.path).exists())
    def test_missing_history_is_not_silently_deleted(self):
        self.run_refresh()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'lost cataloged'):
            self.run_refresh([Artifact('microdata/other.txt', 'https://example.org/other', 'microdata')])
        self.assertEqual(before, self.snapshot())
    def test_partial_product_update_preserves_other_products(self):
        self.run_refresh()
        documentation = Artifact('documentation/readme.txt', 'https://example.org/docs', 'documentation')
        self.run_refresh([documentation], ['documentation'])
        self.assertTrue((self.root / 'gss/latest/microdata/cumulative.txt').exists())
    def test_corrupt_local_file_is_repaired(self):
        self.run_refresh()
        path = self.root / 'gss/latest/microdata/cumulative.txt'
        path.write_bytes(b'local corruption')
        self.run_refresh()
        self.assertEqual(path.read_bytes(), self.client.contents)
    def test_truncated_response_does_not_publish(self):
        self.run_refresh()
        before = self.snapshot()
        self.client.headers = {'Content-Length': '999999'}
        with self.assertRaisesRegex(OSError, 'Truncated'):
            self.run_refresh()
        self.assertEqual(before, self.snapshot())
    def test_304_uses_verified_existing_file(self):
        self.client.headers = {'ETag': 'v1'}
        self.run_refresh()
        before = self.snapshot()
        with patch.object(self.client, 'open', side_effect=HTTPError(self.item.url, 304, 'Unchanged', {}, None)):
            self.run_refresh()
        self.assertEqual(before, self.snapshot())
    def test_same_cumulative_path_survives_new_year_and_new_url(self):
        self.run_refresh()
        newer = Artifact(self.item.path, 'https://example.org/new-release', 'microdata',
                         'New cumulative', 'cumulative', 2025)
        self.run_refresh([newer])
        catalog = json.loads((self.root / 'gss/latest/catalog.json').read_text())
        self.assertEqual(catalog['current']['cumulative'],
                         {'vintage': 2025, 'paths': ['microdata/cumulative.txt']})
    def test_concurrent_writer_is_rejected(self):
        self.run_refresh()
        lock = self.root / 'gss/.update.lock'
        lock.write_text('another process')
        with self.assertRaisesRegex(RuntimeError, 'Another update'):
            self.run_refresh()
        self.assertTrue(lock.exists())
    def test_directory_promotion_failure_rolls_back(self):
        self.run_refresh()
        before = self.snapshot()
        self.client.contents = b'new data'
        original = Path.rename
        def rename(path, target):
            if '.staging-' in str(path):
                raise PermissionError('simulated open file on Windows')
            return original(path, target)
        with patch.object(Path, 'rename', rename):
            with self.assertRaises(PermissionError):
                self.run_refresh()
        self.assertEqual(before, self.snapshot())
    def test_zip_payload_is_validated_before_publication(self):
        self.client.contents = b'not a zip'
        item = Artifact('microdata/bad.zip', 'https://example.org/zip', 'microdata')
        with self.assertRaises(zipfile.BadZipFile):
            self.run_refresh([item])
        self.assertFalse((self.root / 'gss/latest').exists())

    def test_completed_download_survives_later_failure_for_retry(self):
        self.client.headers = {'ETag': 'v1'}
        other = Artifact('microdata/new.txt', 'https://example.org/broken', 'microdata')
        self.client.fail_url = other.url
        with self.assertRaises(OSError):
            self.run_refresh([self.item, other])
        self.assertFalse((self.root / 'gss/latest').exists())
        self.assertTrue(list((self.root / 'gss/.cache').rglob('record.json')))
        self.client.fail_url = None
        self.run_refresh([self.item, other])
        cached_request = [headers for url, headers in self.client.requests if url == self.item.url][-1]
        self.assertEqual(cached_request['If-None-Match'], 'v1')

    def test_api_row_order_does_not_change_snapshot(self):
        item = Artifact('api-tables/profile.json', 'https://api.census.gov/data/test?get=X', 'api-tables')
        self.client.contents = b'[["GEO_ID","X"],["02","2"],["01","1"]]'
        self.run_refresh([item], ['api-tables'])
        before = self.snapshot()
        self.client.contents = b'[["GEO_ID","X"],["01","1"],["02","2"]]'
        self.run_refresh([item], ['api-tables'])
        self.assertEqual(before, self.snapshot())

    def test_acs_tables_are_losslessly_and_deterministically_compressed(self):
        item = Artifact('summary/2024/table.dat.gz', 'https://example.org/table.dat', 'summary')
        self.run_refresh([item], ['summary'])
        path = self.root / 'gss/latest/summary/2024/table.dat.gz'
        self.assertEqual(gzip.decompress(path.read_bytes()), self.client.contents)
        before = self.snapshot()
        self.run_refresh([item], ['summary'])
        self.assertEqual(before, self.snapshot())


class DiscoveryTests(unittest.TestCase):
    def test_census_directory_cannot_escape_root(self):
        client = Client()
        root = CENSUS + 'data/pums/'
        client.pages[root] = '<a href="../">Parent</a><a href="?C=N">Sort</a><a href="2024/">2024</a><a href="%2e%2e/">Escape</a><a href="https://other.test/2025/">Other</a>'
        self.assertEqual(child_links(client, root), [(root + '2024/', '2024')])
    def test_pums_prefers_us_and_pr_csv_over_duplicate_state_and_sas_files(self):
        names = ['csv_hus.zip', 'csv_pus.zip', 'csv_hpr.zip', 'csv_ppr.zip', 'csv_hca.zip', 'sas_hus.zip', 'unix_hca.zip', 'pc_hca.zip', 'README.pdf']
        entries = [('https://example.org/' + n, n) for n in names]
        selected = pums_select('', entries)
        self.assertEqual({t for _, t in selected}, set(names) - {'csv_hca.zip', 'sas_hus.zip', 'unix_hca.zip', 'pc_hca.zip'})

    def test_five_year_pums_uses_state_files_to_avoid_oversized_national_archives(self):
        names = ['csv_hus.zip', 'csv_pus.zip', 'csv_hca.zip', 'csv_pca.zip', 'csv_hpr.zip', 'csv_ppr.zip']
        entries = [('https://example.org/5-Year/' + n, n) for n in names]
        selected = pums_select('https://example.org/5-Year/', entries)
        self.assertEqual({t for _, t in selected}, set(names) - {'csv_hus.zip', 'csv_pus.zip'})
    def test_legacy_pums_files_are_retained(self):
        entries = [('https://example.org/Pums96FL.zip', 'legacy')]
        self.assertEqual(pums_select('', entries), entries)

    def test_rejected_2019_listing_uses_documented_inventory_and_marks_gap(self):
        client = Client()
        client.allow_partial = True
        client.current_product = 'pums'
        parent = CENSUS + 'data/pums/2019/'
        client.pages[parent] = '<a href="5-Year/">5-Year/</a>'
        client.pages[ACS_2019_FIVE_YEAR] = ''
        files = files_under(client, parent, pums_select)
        self.assertEqual(len(files), 104)
        self.assertIn((ACS_2019_FIVE_YEAR + 'csv_hak.zip', 'csv_hak.zip'), files)
        self.assertIn('listing unavailable', client.discovery_errors['pums'][0]['error'])
    def test_summary_avoids_duplicate_entire_us_bundle(self):
        names = ['1_year_by_state/', '1_year_entire_sf/', '1_year_seq_by_state/', '1_year_comparison_profiles/']
        selected = summary_select('', [('https://example.org/' + n, n) for n in names])
        self.assertEqual({t for _, t in selected}, {'1_year_by_state/', '1_year_comparison_profiles/'})
    def test_latest_aliases_keep_period_lengths_separate(self):
        records = {f'{period}/{year}': {'series': period, 'vintage': year}
                   for period, year in [('pums/1-year', 2025), ('pums/5-year', 2024), ('pums/1-year', 2024)]}
        current = current_releases(records)
        self.assertEqual(current['pums/1-year']['vintage'], 2025)
        self.assertEqual(current['pums/5-year']['vintage'], 2024)
    def test_safe_paths_reject_traversal_and_windows_drives(self):
        for relative in ('../outside', '/absolute', 'C:/outside', 'x\\y'):
            with self.assertRaises(ValueError):
                safe_path(Path.cwd(), relative)
    def test_census_api_requires_key_instead_of_silently_skipping(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'CENSUS_API_KEY'):
                api_tables(Client())

    def test_year_checkpoints_publish_newest_first_and_finish_without_pending_marker(self):
        client = FakeClient()
        items = [Artifact(f'pums/{year}/data.zip', f'https://example.org/{year}', 'pums')
                 for year in (2023, 2024)]
        calls = []
        def publish(source, batch, root, actual_client, products, *args, **kwargs):
            calls.append((batch[0].path, json.loads(json.dumps(actual_client.discovery_errors))))
        with patch.object(bootstrap, 'Client', return_value=client), \
             patch.object(bootstrap, 'discover', return_value=items), \
             patch.object(bootstrap, 'refresh', side_effect=publish), \
             contextlib.redirect_stdout(io.StringIO()):
            result = bootstrap.main(['--source', 'acs', '--product', 'pums', '--allow-partial', '--checkpoint-years'])
        self.assertEqual(result, 0)
        self.assertEqual([c[0] for c in calls], ['pums/2024/data.zip', 'pums/2023/data.zip'])
        self.assertIn('pending years: 2023', calls[0][1]['pums'][0]['error'])
        self.assertEqual(calls[1][1], {})
    def test_census_geography_expands_states_but_wildcards_counties(self):
        client = Client()
        endpoint = 'https://api.census.gov/data/test'
        from census_api import query_url
        url = query_url(endpoint, [('get','NAME'),('for','state:*')])
        client.pages[url] = json.dumps([['NAME','state'],['Alabama','01'],['Puerto Rico','72']])
        definitions = [{'name':'state'}, {'name':'tract','requires':['state','county'],'wildcard':['county']}]
        scopes = Geographies(client, endpoint, definitions).scopes(definitions[1])
        self.assertEqual([dict(s) for s in scopes], [{'state':'01','county':'*'},{'state':'72','county':'*'}])

    def test_atus_new_cumulative_year_keeps_canonical_paths(self):
        def discover(year):
            client = Client()
            index = BLS + '/tus/data.htm'
            client.pages[index] = ''.join(
                f'<a href="/tus/data/{y}.htm">American Time Use Survey—{y} Microdata files</a>'
                for y in range(2003, year + 1))
            cumulative = f'/tus/data/datafiles-03{str(year)[2:]}.htm'
            client.pages[index] += f'<a href="{cumulative}">American Time Use Survey—2003-{year} Microdata files</a>'
            for y in range(2003, year + 1):
                client.pages[BLS + f'/tus/data/{y}.htm'] = f'<a href="/tus/special.requests/atusresp_{y}.zip">Respondents</a>'
            client.pages[BLS + cumulative] = f'<a href="/tus/special.requests/atusresp_03{str(year)[2:]}.zip">Respondents</a>'
            client.pages[BLS + cumulative] += f'<a href="/tus/special.requests/atuswgts_03{str(year)[2:]}.zip">Weights</a>'
            client.pages[BLS + cumulative] += '<a href="/tus/special.requests/atuswgts_1920.zip">Pandemic weights</a>'
            return atus_microdata(client)
        first, second = discover(2024), discover(2025)
        before = {i.path for i in first}
        after = {i.path for i in second}
        self.assertTrue(before.issubset(after))
        self.assertIn('microdata/cumulative/atusresp.zip', after)
        self.assertIn('microdata/cumulative/atuswgts.zip', after)
        self.assertIn('microdata/cumulative/atuswgts_1920.zip', after)
        self.assertEqual(next(i.vintage for i in second if i.series == 'cumulative'), 2025)

    def test_atus_missing_annual_year_is_an_error(self):
        client = Client()
        client.pages[BLS + '/tus/data.htm'] = '<a href="/tus/data/2025.htm">American Time Use Survey—2025 Microdata files</a>'
        with self.assertRaisesRegex(RuntimeError, 'annual coverage'):
            atus_microdata(client)


if __name__ == '__main__':
    unittest.main()
