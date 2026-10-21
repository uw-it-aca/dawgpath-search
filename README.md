# DawgPath search

Shared, Django-independent Whoosh index builder for the data pipeline and
offline Pathways development. No database, GCS client, credentials, or network
access is needed at build time.

Requires Python 3.12+ and pins Whoosh 2.7.4, matching the existing Pathways
schema/analyzers. This is the initial package; integration into the pipeline
and Pathways is a separate step after this repository is published.

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
bundling, storage, release finalization, and activation; this package does not
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
