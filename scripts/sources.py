"""Publisher-specific inventories. A nonempty crawl alone is not a coverage check."""
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
import sys
from urllib.parse import unquote, urljoin, urlsplit
from urllib.error import HTTPError, URLError

CENSUS = 'https://www2.census.gov/programs-surveys/acs/'
BLS = 'https://www.bls.gov'
GSS = 'https://gss.norc.org'
# NORC's SAS page links this nonexistent wave; its URL serves HTML, not data.
# NORC's FAQ explicitly lists 1992 as a year without a GSS survey.
KNOWN_INVALID_GSS = GSS + '/content/dam/gss/get-the-data/documents/sas/1992_sas.zip'
# Checked against Census's public directory listing on 2026-10-28. This is a
# fallback only when the listing is rejected; the files remain subject to normal
# download and integrity checks. A failed listing still marks coverage incomplete.
ACS_2019_FIVE_YEAR = CENSUS + 'data/pums/2019/5-Year/'
ACS_STATES_AND_PR = ('ak al ar az ca co ct dc de fl ga hi ia id il in ks ky la ma md '
                     'me mi mn mo ms mt nc nd ne nh nj nm nv ny oh ok or pa pr ri sc '
                     'sd tn tx ut va vt wa wi wv wy').split()
SOURCES = {
    'gss': ['microdata', 'documentation'],
    'acs': ['api-tables', 'pums', 'summary', 'historical-tables', 'other-tables', 'experimental',
            'replicate-estimates', 'documentation'],
    'atus': ['microdata', 'time-series', 'tables', 'news-tables'],
}


@dataclass(frozen=True)
class Artifact:
    path: str
    url: str
    product: str
    title: str = ''
    series: str = ''
    vintage: int = 0
    alternates: tuple = ()

    def record(self):
        return {key: getattr(self, key) for key in ('url', 'product', 'title', 'series', 'vintage')}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href = dict(attrs).get('href')
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.links.append((self.href, ' '.join(''.join(self.text).split())))
            self.href = None


def links(client, page):
    parser = LinkParser()
    parser.feed(client.text(page))
    return sorted(set((urljoin(page, u).split('#')[0], t) for u, t in parser.links))


def child_links(client, directory):
    """Only immediate descendants, excluding navigation, sorting links and escapes."""
    parent = urlsplit(directory)
    found = {}
    for url, title in links(client, directory):
        part = urlsplit(url)
        if part.netloc != parent.netloc or part.query or not part.path.startswith(parent.path):
            continue
        relative = unquote(part.path[len(parent.path):])
        if (not relative or '/' in relative.rstrip('/') or '\\' in relative
                or ':' in relative or relative.rstrip('/') in ('.', '..')):
            continue
        found[url] = title
    return sorted(found.items())


def files_under(client, root, select=None):
    pending, seen, files = [root], set(), []
    while pending:
        directory = pending.pop()
        if directory in seen:
            continue
        seen.add(directory)
        try:
            entries = child_links(client, directory)
            if not entries:
                raise RuntimeError(f'Empty/unrecognized publisher directory: {directory}')
        except (HTTPError, URLError, OSError, RuntimeError) as exc:
            if not getattr(client, 'allow_partial', False) or directory == root:
                raise
            if directory == ACS_2019_FIVE_YEAR:
                client.discovery_errors.setdefault(client.current_product, []).append({
                    'url': directory,
                    'error': 'listing unavailable; using Census listing captured 2026-10-28'})
                entries = [(directory + f'csv_{kind}{state}.zip', f'csv_{kind}{state}.zip')
                           for kind in ('h', 'p') for state in ACS_STATES_AND_PR]
                print(f'Using pinned Census listing for {directory}', file=sys.stderr, flush=True)
                files.extend(entries)
                continue
            client.discovery_errors.setdefault(client.current_product, []).append(
                {'url': directory, 'error': str(exc)})
            print(f'Skipping inaccessible directory {directory}: {exc}', file=sys.stderr, flush=True)
            continue
        if select:
            entries = select(directory, entries)
        for url, title in entries:
            if url.endswith('/'):
                pending.append(url)
            else:
                files.append((url, title))
    return files


def years(text):
    return [int(y) for y in re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', text)]


def from_tree(client, root, product, select=None):
    result = []
    for url, title in files_under(client, root, select):
        relative = unquote(urlsplit(url).path[len(urlsplit(root).path):])
        dates = years(relative)
        vintage = dates[0] if dates else 0
        # Distinct ACS period lengths must not compete for the same 'current' alias.
        period = re.search(r'(?:^|/)([135])[-_ ]?(?:year|yr)', relative, re.I)
        series = product + (f'/{period[1]}-year' if period else '')
        # Modern table-based ACS files are published as large uncompressed DAT
        # files; compress each table independently for tractable Git LFS storage.
        stored = relative + '.gz' if product == 'summary' and relative.lower().endswith('.dat') else relative
        result.append(Artifact(f'{product}/{stored}', url, product, title, series, vintage))
    return result


def gss_microdata(client):
    found = {}
    for fmt in ('stata', 'sas', 'spss'):
        page = f'{GSS}/get-the-data/{fmt}.html'
        count = 0
        for url, title in links(client, page):
            title = title.replace('\u200b', '').strip()
            suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
            if urlsplit(url).netloc != 'gss.norc.org' or suffix not in ('.zip', '.dta', '.sav', '.sas7bdat'):
                continue
            if not title:
                continue
            if url == KNOWN_INVALID_GSS:
                print('Ignoring documented invalid NORC link for the nonexistent 1992 wave', file=sys.stderr)
                continue
            count += 1
            label = re.split(r'\(release', title, flags=re.I)[0]
            dates = years(label)
            lower = label.lower()
            if 'cumulative' in lower:
                identity, series = 'cross-sectional/cumulative', 'cross-sectional'
            elif re.fullmatch(r'\d{4}', label.strip()):
                identity, series = f'cross-sectional/annual/{label.strip()}', 'annual'
            elif 'paradata' in lower and dates:
                identity, series = f'paradata/{dates[0]}', 'paradata'
            elif 'merged' in lower and dates:
                identity, series = f'merged/{dates[0]}', 'merged'
            elif 'panel' in lower and dates:
                identity, series = 'panel/' + '-'.join(map(str, dates)), 'panel'
            else:
                raise RuntimeError(f'Unclassified GSS dataset: {title} ({url})')
            # Prefer Stata when a logical product is offered in multiple formats;
            # SAS supplies public products not offered in Stata (e.g. paradata).
            candidate = Artifact(f'microdata/{identity}{suffix}', url,
                'microdata', title, series, max(dates, default=0))
            if identity not in found:
                found[identity] = candidate
            elif found[identity].path == candidate.path and url != found[identity].url:
                found[identity] = replace(found[identity], alternates=found[identity].alternates + (url,))
        if not count:
            raise RuntimeError(f'No downloads found on {page}')
    if 'cross-sectional/cumulative' not in found or not any(k.startswith('panel/') for k in found):
        raise RuntimeError('GSS cumulative or panel coverage is missing')
    return list(found.values())


def document_links(client, pages, product, host, prefix):
    found = {}
    for page in pages:
        for url, title in links(client, page):
            if urlsplit(url).netloc == host and PurePosixPath(urlsplit(url).path).suffix.lower() in ('.pdf', '.xlsx', '.xls', '.zip', '.txt', '.do'):
                path = unquote(urlsplit(url).path).lstrip('/')
                found[url] = Artifact(f'{prefix}/{path}', url, product, title)
    if not found:
        raise RuntimeError(f'No documents found on {pages}')
    return list(found.values())


def pums_select(directory, entries):
    names = {PurePosixPath(urlsplit(u).path).name.lower() for u, _ in entries}
    national_csv = {'csv_hus.zip', 'csv_pus.zip'}.issubset(names)
    any_csv = any(re.match(r'csv_[hp].*\.zip$', n) for n in names)
    states_h = {n[5:7] for n in names if re.fullmatch(r'csv_h[a-z]{2}\.zip', n)} - {'us', 'pr'}
    states_p = {n[5:7] for n in names if re.fullmatch(r'csv_p[a-z]{2}\.zip', n)} - {'us', 'pr'}
    five_year_states = bool(re.search(r'(?:^|/)5[-_ ]?year/', directory, re.I)
                            and states_h and states_h == states_p)
    chosen = []
    for url, title in entries:
        name = PurePosixPath(urlsplit(url).path).name.lower()
        if any_csv and re.match(r'(?:sas|unix|pc)_[hp].*\.zip$', name):
            continue
        if five_year_states and name in {'csv_hus.zip', 'csv_pus.zip'}:
            continue
        if national_csv and not five_year_states and re.match(r'csv_[hp].*\.zip$', name) and name not in {
                'csv_hus.zip', 'csv_pus.zip', 'csv_hpr.zip', 'csv_ppr.zip'}:
            continue
        chosen.append((url, title))
    return chosen


def summary_select(directory, entries):
    names = {url.rstrip('/').rsplit('/', 1)[-1].lower() for url, _ in entries}
    result = []
    for url, title in entries:
        name = url.rstrip('/').rsplit('/', 1)[-1].lower()
        # Per-state bundles avoid multi-gigabyte entire-US ZIP files and duplicate
        # by-sequence bundles. Preserve templates and separate aggregate products.
        match = re.match(r'([135])_year_(?:entire_sf|seq_by_state)$', name)
        if match and f'{match[1]}_year_by_state' in names:
            continue
        if name == 'prototype':
            continue  # experimental duplicate layout of the same detailed tables
        if name in ('1yrdata.zip', '5yrdata.zip') and any(n.endswith('.dat') for n in names):
            continue  # keep the individual tables, not a second combined copy
        result.append((url, title))
    return result


def acs_other_tables(client):
    root = CENSUS + 'data/'
    result = []
    for url, _ in child_links(client, root):
        if re.fullmatch(r'\d{4}', url.rstrip('/').rsplit('/', 1)[-1]):
            try:
                items = from_tree(client, url, 'other-tables')
            except (HTTPError, URLError, OSError, RuntimeError) as exc:
                if not getattr(client, 'allow_partial', False):
                    raise
                client.discovery_errors.setdefault('other-tables', []).append({'url': url, 'error': str(exc)})
                print(f'Skipping inaccessible ACS year {url}: {exc}', file=sys.stderr, flush=True)
                continue
            for item in items:
                year = int(url.rstrip('/').rsplit('/', 1)[-1])
                result.append(Artifact(f'other-tables/{year}/' + item.path.removeprefix('other-tables/'),
                    item.url, item.product, item.title, item.series, year))
    return result


def atus_microdata(client):
    index = BLS + '/tus/data.htm'
    pages = [(u, title) for u, title in links(client, index)
             if 'microdata files' in title.lower() and urlsplit(u).netloc == 'www.bls.gov']
    annual = {max(years(title)): u for u, title in pages if re.fullmatch(
        r'American Time Use Survey\s*[—–-]\s*\d{4}\s+Microdata files', title, re.I)}
    if not annual or min(annual) != 2003 or set(annual) != set(range(2003, max(annual) + 1)):
        raise RuntimeError('ATUS annual coverage is incomplete (expected every year since 2003)')
    # The latest cumulative bundle supersedes older cumulative bundles, but annual
    # files remain necessary for allocation flags, trip and call-history records.
    cumulative = [(u, title) for u, title in pages if len(years(title)) == 2]
    if not cumulative:
        raise RuntimeError('No ATUS cumulative release found')
    newest = max(cumulative, key=lambda p: max(years(p[1])))
    pages = [(u, t) for u, t in pages if (u, t) not in cumulative] + [newest]
    pending, seen, result = pages[:], set(), {}
    while pending:
        page, label = pending.pop()
        if page in seen:
            continue
        seen.add(page)
        is_cumulative = page == newest[0]
        count = 0
        for url, title in links(client, page):
            p = urlsplit(url)
            if p.netloc != 'www.bls.gov':
                continue
            suffix = PurePosixPath(p.path).suffix.lower()
            if suffix in ('.zip', '.pdf') and p.path.startswith('/tus/'):
                count += suffix == '.zip'
                if is_cumulative:
                    # e.g. atusresp_0325.zip -> cumulative/atusresp.zip
                    end = str(max(years(label)))[-2:]
                    name = re.sub(r'[_-]?\d{2}' + end + r'(?=\.(?:zip|pdf)$)', '', PurePosixPath(p.path).name)
                    path = f'microdata/cumulative/{name}'
                    series = 'cumulative'
                else:
                    path = 'microdata/' + unquote(p.path.removeprefix('/tus/'))
                    series = 'annual' if page in annual.values() else 'modules'
                dates = years(label)
                item = Artifact(path, url, 'microdata', title, series, max(dates, default=0))
                if path in result and result[path].url != url:
                    raise RuntimeError(f'Distinct ATUS files map to the same canonical path: {path}')
                result.setdefault(path, item)
            elif 'microdata files' in title.lower() and suffix in ('.htm', '.html'):
                pending.append((url, title))
        if page in annual.values() and not count:
            raise RuntimeError(f'No ATUS annual ZIP files found: {page}')
    return list(result.values())


def atus_series(client):
    root = 'https://download.bls.gov/pub/time.series/tu/'
    files = from_tree(client, root, 'time-series')
    required = {'time-series/tu.data.1.AllData', 'time-series/tu.series', 'time-series/tu.txt'}
    if not required.issubset({f.path for f in files}):
        raise RuntimeError('ATUS LABSTAT data or dictionaries are missing')
    return [f for f in files if not f.path.endswith('tu.data.0.Current')]


def atus_tables(client):
    page = BLS + '/tus/tables.htm'
    result = {}
    for url, title in links(client, page):
        p = urlsplit(url)
        if p.netloc == 'www.bls.gov' and p.path.startswith('/tus/') and p.path.lower().endswith(('.pdf', '.xls', '.xlsx')):
            path = 'tables/' + unquote(p.path.removeprefix('/tus/'))
            result[url] = Artifact(path, url, 'tables', title, 'tables', max(years(p.path), default=0))
    if not result:
        raise RuntimeError('No ATUS published tables found')
    return list(result.values())


def atus_news_tables(client):
    page = BLS + '/tus/news.htm'
    result, archives = {}, []
    for url, title in links(client, page):
        p = urlsplit(url)
        if p.netloc != 'www.bls.gov':
            continue
        if p.path.startswith('/bls/news-release/') and p.path.endswith('.htm'):
            archives.append(url)
        elif p.path.startswith('/news.release/') and p.path.endswith(('.htm', '.pdf')):
            result[url] = title
    for archive in archives:
        for url, title in links(client, archive):
            if url.startswith(BLS + '/news.release/archives/') and url.endswith(('.htm', '.pdf')):
                result[url] = title
    if not archives or not result:
        raise RuntimeError('ATUS news-release archive not found')
    return [Artifact('news-tables/' + unquote(urlsplit(url).path.removeprefix('/news.release/')),
                     url, 'news-tables', title) for url, title in result.items()]


def discover(source, products, client):
    from census_api import api_tables
    adapters = {
        ('acs', 'api-tables'): api_tables,
        ('gss', 'microdata'): gss_microdata,
        ('gss', 'documentation'): lambda c: document_links(c, [GSS + '/get-documentation.html'],
            'documentation', 'gss.norc.org', 'documentation'),
        ('acs', 'pums'): lambda c: from_tree(c, CENSUS + 'data/pums/', 'pums', pums_select),
        ('acs', 'summary'): lambda c: from_tree(c, CENSUS + 'summary_file/', 'summary', summary_select),
        ('acs', 'historical-tables'): lambda c: from_tree(c, CENSUS + 'data/archive/', 'historical-tables'),
        ('acs', 'other-tables'): acs_other_tables,
        ('acs', 'experimental'): lambda c: from_tree(c, CENSUS + 'experimental/', 'experimental', pums_select),
        ('acs', 'replicate-estimates'): lambda c: from_tree(c, CENSUS + 'replicate_estimates/', 'replicate-estimates'),
        ('acs', 'documentation'): lambda c: from_tree(c, CENSUS + 'tech_docs/', 'documentation'),
        ('atus', 'microdata'): atus_microdata,
        ('atus', 'time-series'): atus_series,
        ('atus', 'tables'): atus_tables,
        ('atus', 'news-tables'): atus_news_tables,
    }
    result = {}
    for product in products:
        client.current_product = product
        print(f'Discovering {source}/{product} ...', flush=True, file=sys.stderr)
        items = adapters[source, product](client)
        if not items:
            raise RuntimeError(f'No files discovered for required product {source}/{product}')
        for item in items:
            if item.path in result and result[item.path].url != item.url:
                raise RuntimeError(f'Two upstream files map to {item.path}')
            result[item.path] = item
    return sorted(result.values(), key=lambda item: item.path)
