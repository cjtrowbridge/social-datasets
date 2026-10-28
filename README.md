# Social datasets

Versioned public social survey data for reuse as a Git submodule in analysis pipelines. The initial collection covers the General Social Survey (GSS), American Community Survey (ACS), and American Time Use Survey (ATUS). More surveys can be added later.

The intended workflow is to fetch the latest available historical releases, review the changes, and commit and push the updated data. Consumers read a stable `latest` directory. Git history preserves earlier repository snapshots.

## Canonical latest data

Each survey has one canonical directory:

```text
data/
  gss/latest/
    catalog.json
    microdata/cross-sectional/cumulative.zip
    microdata/cross-sectional/annual/1972.zip
    microdata/panel/...
    documentation/...
  acs/latest/
    catalog.json
    pums/<year>/<period>/...
    summary/<year>/...
    api-tables/<family>/<year>/...
    historical-tables/...
  atus/latest/
    catalog.json
    microdata/cumulative/...
    microdata/data/...
    time-series/tu.data.1.AllData
    time-series/tu.series
    tables/...
    news-tables/...
```

`latest` means **the latest successfully downloaded version in this repository checkout**, including the historical years. It does not mean only the newest survey year, and it does not change until someone refreshes and publishes the repository. Updating a downstream submodule to a newer commit gives it that snapshot.

The catalog records each file's source URL, product, release title, vintage, size and SHA-256. Its `current` section identifies the newest downloaded vintage separately for each series (for example, ACS one-year and five-year PUMS). Paths in the catalog are relative to the survey's `latest/` directory:

```python
import json
from pathlib import Path

root = Path('vendor/social-datasets/data/acs/latest')
catalog = json.loads((root / 'catalog.json').read_text(encoding='utf-8'))
newest_one_year_pums = [
    root / path for path in catalog['current']['pums/1-year']['paths']
]
all_pums_files = [
    root / path for path, record in catalog['files'].items()
    if record['product'] == 'pums'
]
```

The catalog's `products` field lists what has actually been downloaded. A selectively bootstrapped checkout is not a complete collection. Cumulative and annual data overlap; multi-year ACS samples also overlap. Keep the release families separate in analyses.

## Refreshing the collection

Requires Python 3.10+ (standard library only). Data is tracked through **Git LFS**; install it before adding data to Git. The `.gitattributes` file covers data payloads while leaving catalogs as ordinary JSON.

```sh
git lfs install --local
python scripts/bootstrap.py --products
python scripts/bootstrap.py --source gss --plan
python scripts/bootstrap.py
```

The full ACS API collection requires a [Census API key](https://api.census.gov/data/key_signup.html) in the `CENSUS_API_KEY` environment variable. The key is added only to outgoing requests; it is not saved in catalogs. Without a key, the ACS source reports an error rather than claiming API coverage. Other surveys still run. Bulk ACS products can be fetched independently without a key:

```sh
python scripts/bootstrap.py --source acs --product pums --product summary
python scripts/bootstrap.py --source acs --product historical-tables --product other-tables
python scripts/bootstrap.py --source atus
```

`--product` may be repeated. `--plan` (also `--list`) discovers the selected releases without downloading payloads; API geography discovery still needs the key. `--data-dir PATH` writes an independent collection elsewhere. `--recheck` downloads selected payloads regardless of HTTP cache validators to detect corrections even if a publisher fails to update those headers.

Use `--allow-partial` to publish accessible files when individual publisher directories or files fail. The catalog's `incomplete` field lists skipped URLs and errors, previously downloaded files are retained, and the command exits nonzero until the gaps are resolved. By default, downloads stop before free disk space falls below 10 GiB; adjust this with `--min-free-gib` if needed.

For a long initial ACS PUMS download, add `--checkpoint-years` to `--source acs --product pums --allow-partial` to publish each completed year separately, newest first. The catalog records pending years until the run finishes.

A complete historical collection is large. Plan for the payloads, changed files staged during an update, and Git LFS's local object cache and remote storage. Bulk releases remain in their native formats, including ZIP archives, except that individual uncompressed ACS summary `.dat` files are stored as losslessly compressed `.dat.gz` files. Extraction and statistical interpretation belong in downstream analysis pipelines. API responses are saved as JSON tables, with rows sorted deterministically; labels, annotations, estimates and margins of error are retained.

After a successful refresh:

```sh
git status --short
git diff -- data/gss/latest/catalog.json data/acs/latest/catalog.json data/atus/latest/catalog.json
git add .gitattributes data
git commit -m "Refresh social survey data"
git push
```

The bootstrap never commits or pushes. Downloaded source data retains its publisher's terms and citation requirements; the repository LICENSE does not replace those terms.

## Update guarantees

- Rerunning against unchanged data leaves payloads and catalogs byte-identical, preserving modification times. There are no changing run timestamps in tracked metadata.
- Existing files are checked against their recorded hashes before conditional requests are used. Revisions at an existing URL replace the canonical file and update its checksum.
- All selected products for a survey are staged and validated before its `latest/` directory is replaced. By default, a failed download leaves that survey's previous snapshot intact. With `--allow-partial`, accessible files are published and gaps are recorded in the catalog. Other surveys are independent; any failure produces a nonzero exit status.
- The directory replacement uses two renames, with rollback on failure. Do not run analysis readers concurrently with a refresh: there can be a brief interval when `latest/` is absent. This is a local maintenance operation, not a live database transaction.
- An exclusive per-survey lock prevents concurrent writers. After a process is killed, confirm it has stopped before removing `data/<survey>/.update.lock`; rerunning recovers an interrupted directory replacement. Temporary staging and recovery directories are Git-ignored.
- Completed downloads are retained in an ignored per-survey `.cache/` directory for reuse after a failed run. Reuse still checks the remote validators; this is not a promise that every rerun avoids network transfers.
- Previously cataloged files disappearing from discovery cause an error, not silent deletion of historical data. Publisher layout changes or intentional withdrawals need review and an adapter/catalog correction.

## Source coverage

The adapters discover published releases instead of hard-coding the latest year. Coverage is defined by these public products; restricted respondent records, bespoke extracts, and unpublished tables are outside it.

| Survey | Products |
| --- | --- |
| GSS | NORC cumulative and annual cross-sections, merged files, panels and paradata. Prefer Stata for each logical product and fall back to SAS/SPSS when a preferred link fails or the product is unavailable in Stata. The catalog records the chosen URL. Linked codebooks and supporting documentation are retained. |
| ACS microdata | The PUMS archive from its earliest available year onward, including early pilot files, available one-/three-/five-year products and Puerto Rico. Prefer CSV files over duplicate SAS formats. Use state CSV bundles for five-year releases when available, avoiding national archives that can exceed GitHub's LFS file-size limit; use national and Puerto Rico CSV bundles for other releases where available. |
| ACS aggregates | Historical bulk tables, detailed Summary Files with geography/variable documentation, other published bulk tables, experimental products and variance replicate estimates. Separate Census API discovery covers profiles, subject/comparison tables, selected populations, supplemental estimates and other ACS aggregate families listed in its catalog. Detailed tables and PUMS are obtained from bulk sources. |
| ATUS | All annual microdata releases since 2003, the newest cumulative release, linked module data and dictionaries; LABSTAT historical estimates and series dictionaries; published tables and current/historical news-release tables. Annual files are retained because cumulative files omit some variables and file types. |

Aggregate data is downloaded as published. The bootstrap does not invent GSS tabulations from microdata or treat those as official published estimates. It also does not harmonize variable meanings, survey weights or geographic boundaries across years.

One documented upstream exception: NORC's SAS page lists `1992_sas.zip`, which returns HTML instead of a data archive. The adapter excludes that exact link; [NORC's FAQ](https://gss.norc.org/faq.html) identifies 1992 as a year without a GSS survey.

When the 2019 five-year PUMS listing is rejected, partial mode uses an inventory captured from the Census directory listing on 2026-10-28 and downloads the individual state and Puerto Rico CSV archives directly. The catalog still flags the listing as incomplete until it can be checked live.

Official sources: [GSS downloads](https://gss.norc.org/get-the-data.html), [GSS documentation](https://gss.norc.org/get-documentation.html), [ACS bulk products](https://www.census.gov/programs-surveys/acs/data/data-via-ftp.html), [ACS API](https://www.census.gov/programs-surveys/acs/data/data-via-api.html), [ATUS microdata](https://www.bls.gov/tus/data.htm), [ATUS database](https://www.bls.gov/tus/database.htm), [ATUS tables](https://www.bls.gov/tus/tables.htm).

## Using as a submodule

```sh
git submodule add https://github.com/cjtrowbridge/social-datasets.git vendor/social-datasets
git submodule update --init --recursive
git -C vendor/social-datasets lfs install --local
git -C vendor/social-datasets lfs pull
```

Consumers need the committed LFS objects, not access to the publishers or an API key. Pin the submodule commit for reproducibility; deliberately update it when adopting a new data snapshot. LFS pointer files alone are not the data.

## Validation and development

```sh
python -m unittest discover -s tests -v
```

Tests cover idempotence, source revisions, corruption, download failures, rollback, concurrent writers, product selection and geography discovery. Live verification has exercised GSS discovery/downloads and Census bulk listings. BLS returned HTTP 403 from this environment, and the Census 2019 five-year PUMS directory returned an HTTP-200 rejection page. Census API data queries cannot be exercised here without a key. Those paths require live validation before relying on a complete production snapshot.

To add a survey or product, implement its inventory in `scripts/sources.py`, give artifacts stable semantic paths, and add discovery and update tests. Treat unexpected gaps or unclassified products as errors.
