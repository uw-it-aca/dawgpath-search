# DawgPath search

Shared, Django-independent Whoosh index builder for the data pipeline and
offline Pathways development. No database, GCS client, credentials, or network
access is needed at build time.

Requires Python 3.12+ and pins Whoosh 2.7.4, matching the existing Pathways
schema/analyzers. The pipeline and Pathways share index building, bundle
creation, and verified offline preparation through this package.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
ruff check .
```

Tests are self-contained and need no Django installation or application checkout.
They compare contract v1 with a frozen reference of the Pathways builder,
including stored documents, query ranking, stemming, campus/type/flag filters,
and integer COI ranges. They also cover input validation, offline operation,
partial builds, metadata/file tampering, and the module CLI.

## CI and releases

The GitHub Actions workflow runs on pull requests and pushes to `main`, `master`,
`qa`, and `develop`, and on published GitHub releases. It tests with Python 3.12,
runs Ruff lint/format checks, builds a wheel and source distribution, validates
their metadata, and smoke-tests the installed wheel outside the checkout.
Tested distributions are retained as workflow artifacts for seven days.

This follows the organization's `actions/examples/python-test-publish` pattern,
but uses `python -m build` for this `pyproject.toml` package instead of the
`setup.py`-based `uw-it-aca/actions/publish-pypi` action. The package version is
maintained in `pyproject.toml`; release tags must match it, with an optional `v`
prefix (for example, `v0.1.0`).

PyPI publishing is disabled by default. When ready:

1. Grant this repository access to the `PYPI_API_TOKEN` Actions secret, with
   permission to publish the `dawgpath-search` project on PyPI.
2. Set the Actions repository variable `PYPI_PUBLISH_ENABLED` to `true`.
3. Update the package version and publish a matching GitHub release.

The publish job runs only for a published release after all checks pass, and
uploads the exact distributions built by the test job. Neither a normal push
nor a tag push publishes to PyPI. Enabling the variable does not retroactively
publish earlier releases. Until publishing is enabled, applications should
continue to pin an exact Git commit.

## Build an index

Supply one release's local files:

- `course_data.json`
- `major_data.json`
- `coi_scores.csv`
- `gateway_courses.csv`
- `bottleneck_courses.csv`

Course JSON is a nonempty array with `course_id`, `course_title`,
`course_description` (string or null), and `course_campus`. Major JSON is a
nonempty object whose values contain `credential_code`, `major_code`,
`credential_title` and `credential_description` (string or null), and
`major_campus`. Other catalog fields are ignored.

COI CSV uses `course_id,score`; scores must be finite and between -5 and 5,
with -1 treated as missing. Flag CSVs use `course,dept_abbr,course_no`; the
legacy `dept_abbv` spelling is also accepted for bottleneck files. Flags are
matched by department and number, not by the first column. CSVs may contain
only their header when no scores or flags are available.

```bash
dawgpath-search --input-root /path/to/snapshot \
  --output-dir /path/to/new-index --release-id local-snapshot-v1
```

Or use the Python API:

```python
from dawgpath_search import build_index, open_index

metadata = build_index("snapshot", "new-index", release_id="local-snapshot-v1")
index = open_index("new-index", release_id="local-snapshot-v1")
with index.searcher() as searcher:
    ...
index.close()
```

The output directory must not already exist. Input errors fail before the
directory is created; an indexing failure may leave a partial directory.
`index-metadata.json` is written last and records source hashes, release ID,
builder/Whoosh versions, contract version, counts, and index-file checksums.
Discard failed output and retry with a fresh directory. The caller handles
storage, full release manifests, finalization, and activation; this package does not
overwrite an active index.

`open_index` verifies metadata, the exact index file set, file hashes, schema,
and document counts. Missing, unlisted, or symlinked index files are rejected;
Whoosh's `MAIN_WRITELOCK` is not part of the immutable file set.
Call it during preparation, not on every search request. Only open trusted
indexes: Whoosh index schemas use Python serialization, and checksums in an
untrusted bundle do not make its contents safe. The release consumer must
authenticate the source and verify its release manifest before using it.

Invalid input contents or index validation failures raise the public
`IndexValidationError` (a `ValueError` subclass). Build-time filesystem failures,
including missing source files and an existing output directory, raise their
normal `OSError` subclasses. The CLI reports these failures on stderr and exits
nonzero without emitting success metadata.

## Create and prepare a bundle

```python
from dawgpath_search import create_bundle, prepare_bundle

bundle = create_bundle("new-index", release_id="local-snapshot-v1")
# Persist/publish bundle and metadata through the application's release manifest.
prepare_bundle(
    bundle, "prepared-index", metadata=metadata, release_id="local-snapshot-v1"
)
```

`create_bundle` verifies an inactive index and returns gzip-compressed tar bytes,
with sorted root-level files and fixed archive timestamps. The index must not
change while bundling. Repeated bundling of identical files is deterministic;
independent Whoosh builds need not produce identical bytes. The format remains
compatible with the pipeline's original `search-index.tar.gz` releases.

`prepare_bundle` takes trusted expected index metadata, compares the bundled
metadata exactly, rejects unknown/duplicate/non-regular/path-traversing members,
and checks sizes and hashes before opening Whoosh. It verifies compatibility,
schema, and document counts in a sibling staging directory before moving to a
fresh output directory. Failure removes staging; existing outputs, including
symlinks, are never replaced. Metadata is limited to 1 MiB.

The caller must authenticate the release origin and check the bundle's outer
checksum, full application manifest, and source-data consistency first.
Neither API downloads files, imports data, or activates an index.

## Compatibility

Contract version 1 preserves the existing Pathways schema and search-document
construction, including stemming, field storage, campus casing, nullable
descriptions, COI rounding and `-1`/missing scores, and gateway/bottleneck flags.
The existing integer `NUMERIC()` COI field is deliberately unchanged; switching
to floating-point indexing requires a separately tested contract change.

COI IDs absent from the catalog and flags for absent courses have no indexed
document, matching Pathways' importer. Duplicate course, credential, and COI
identities are rejected rather than generating ambiguous search results.
This is intentionally stricter than the legacy importer's first-non-missing COI
match: fix conflicting source data before building rather than silently choosing
a score. Some older Pathways snapshots contain such duplicates and will need
reference-data cleanup before use.
Curriculum and career data do not contribute to search documents.

Readers require the exact Whoosh version and supported index contract.
Increment the contract version for incompatible schema, analyzer, or document
semantics changes, and test reader compatibility before deploying new bundles.
Pin the same published package version in pipeline and Pathways. Dependency
installation happens during normal builds; offline local startup then builds
from checked-in representative data without GCS access.

## Integration sequence

1. Publish this repository and establish an immutable package release or commit.
2. Pin it in the pipeline; build from verified run-specific files and publish
   a bundle before finalizing a complete immutable release.
3. Pin it in Pathways; use it for local snapshot builds and verification of
   downloaded live indexes. Preserve the existing query code and result shapes.

No changes to either application's deployment requirements are included here.
