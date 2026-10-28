"""Discover ACS aggregate tables outside the bulk detailed-table releases.

Use published geography hierarchies, expanding required parents rather than
assuming that one national query includes every geography or population group.
"""
import hashlib
import json
import os
import sys
from urllib.parse import urlencode
from sources import Artifact

CATALOG = 'https://api.census.gov/data.json'


def metadata(client, url):
    return json.loads(client.text(url))


def query_url(endpoint, params):
    return endpoint + '?' + urlencode(params)


class Geographies:
    def __init__(self, client, endpoint, definitions, fallback=None):
        self.client = client
        self.endpoint = endpoint
        self.definitions = definitions
        self.fallback = fallback
        self.cache = {}

    def scopes(self, geo):
        required = geo.get('requires', [])
        fixed = [name for name in required if name not in geo.get('wildcard', [])]
        if not fixed:
            return [{name: '*' for name in required}]
        parent_name = fixed[-1]
        parent_chain = required[:required.index(parent_name)]
        rows = self.parent_rows(parent_name, parent_chain)
        return sorted({tuple((name, row[name] if name in fixed else '*') for name in required)
                       for row in rows})

    def parent_rows(self, name, requires):
        key = (name, tuple(requires))
        if key in self.cache:
            return self.cache[key]
        candidates = [g for g in self.definitions if g['name'] == name and g.get('requires', []) == requires]
        if not candidates:
            if self.fallback:
                return self.fallback.parent_rows(name, requires)
            raise RuntimeError(f'Cannot enumerate required parent hierarchy {requires + [name]} at {self.endpoint}')
        result = []
        for scope in self.scopes(candidates[0]):
            params = [('get', 'NAME'), ('for', name + ':*')]
            params.extend(('in', k + ':' + v) for k, v in dict(scope).items())
            text = self.client.text(query_url(self.endpoint, params))
            data = json.loads(text) if text else []
            if not data:
                continue
            if not isinstance(data, list) or not isinstance(data[0], list):
                raise RuntimeError('Invalid Census geography response')
            result.extend(dict(zip(data[0], row)) for row in data[1:])
        self.cache[key] = result
        return result


def api_tables(client):
    if not os.environ.get('CENSUS_API_KEY'):
        raise RuntimeError('ACS API tables require CENSUS_API_KEY. Bulk products can be '
                           'selected separately with --product; they are not full API coverage.')
    datasets = metadata(client, CATALOG)['dataset']
    found = []
    for dataset in datasets:
        family = dataset.get('c_dataset', [])
        # Root acs1/acs3/acs5 detailed tables are already represented by the
        # Summary File; microdata is represented by PUMS. Include all other ACS
        # aggregate families, including supplemental and selected populations.
        if not family or family[0] != 'acs' or any(p.startswith('pums') for p in family):
            continue
        if len(family) == 2 and family[1] in ('acs1', 'acs3', 'acs5'):
            continue
        year = dataset.get('c_vintage')
        if not year:
            continue
        endpoint = 'https://api.census.gov/data/' + str(year) + '/' + '/'.join(family)
        relative = '/'.join(family[1:])
        print(f'  ACS API {year}/{relative}', flush=True, file=sys.stderr)
        variables = metadata(client, endpoint + '/variables.json')['variables']
        geos = metadata(client, endpoint + '/geography.json')['fips']
        groups = metadata(client, endpoint + '/groups.json')['groups']
        if not geos or not groups:
            raise RuntimeError(f'Missing ACS API groups/geographies: {endpoint}')
        fallback = None
        if len(family) > 2:
            base = 'https://api.census.gov/data/' + str(year) + '/' + '/'.join(family[:2])
            fallback = Geographies(client, base, metadata(client, base + '/geography.json')['fips'])
        geography = Geographies(client, endpoint, geos, fallback)
        prefix = f'api-tables/{relative}/{year}'
        for name in ('variables', 'geography', 'groups'):
            found.append(Artifact(f'{prefix}/{name}.json', f'{endpoint}/{name}.json',
                'api-tables', dataset['title'], f'api-tables/{relative}', int(year)))
        for geo in geos:
            for scope in geography.scopes(geo):
                scope = dict(scope)
                for group in groups:
                    params = [('get', 'group(' + group['name'] + ')'), ('for', geo['name'] + ':*')]
                    params.extend(('in', key + ':' + value) for key, value in scope.items())
                    # Defaults would otherwise restrict selected-population and
                    # geographic-component products to one population/component.
                    for dimension in ('POPGROUP', 'GEOCOMP'):
                        if dimension in variables:
                            params.append((dimension, '*'))
                    url = query_url(endpoint, params)
                    partition = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]
                    found.append(Artifact(f'{prefix}/{geo["geoLevelDisplay"]}/{partition}/{group["name"]}.json',
                        url, 'api-tables', group.get('description', group['name']), f'api-tables/{relative}', int(year)))
    if not found:
        raise RuntimeError('No ACS API aggregate datasets found')
    return found
