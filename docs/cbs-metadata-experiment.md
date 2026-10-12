# Supervised CBS metadata experiment

This opt-in repository experiment evaluates the existing monitoring comparison
contract. It is not a CBS collector, persistent source registry, scheduler, or
ingestion path. The package release version is unchanged.

## Source and endpoint

The fixed experimental source ID is `cbs:83583NED`. This table is already used in
the repository's CBS transport, classification, and authorization tests (for
example, `tests/unit/test_task2_transport_routes.py`). It was selected to reuse
an established reference rather than discover or sample a different dataset.

The endpoint is:

`https://opendata.cbs.nl/ODataApi/odata/83583NED/TableInfos`

The request selects OData JSON representation. The endpoint shape comes from
the metadata stage of `CBSTool.fetch()` in `tools/cbs_tool.py`; the experiment
does **not** call `fetch()`, which also requests `TypedDataSet` sample rows.
Each successful response must have media type `application/json` (case-insensitive,
with optional parameters such as `charset=utf-8`), contain exactly one TableInfos object,
and identify `83583NED`. No pagination is followed. This verifies the returned
metadata identity, not the provider's semantic promises about individual fields.

## Limits verified before live execution

- Two independent metadata GET operations, separated by one second.
- Four total connection attempts at most, counting redirect requests. Each
  operation permits at most one redirect, only to the same HTTPS host and
  TableInfos path (including a root-equivalent trailing slash).
- No application retry, SDK retry, or fallback to a second DNS address. The
  experiment uses `SafeHttpClient`'s existing connector seam and delegates to
  its pinned connector with one of the already validated addresses. All DNS
  answers are still validated; TLS verification and redirect guards remain.
- Each operation, including its redirect, has at most 15 seconds, reduced by
  the remaining 60-second monotonic overall deadline. Independent POSIX alarm
  timers interrupt blocking operations, including stalled response-body reads.
  The runner requires a POSIX main thread; it refuses unsupported environments.
- At most 1 MiB of response body may be read per request. The accepted body cap
  is one byte smaller to reserve the existing guard's overflow-detection byte.
  Declared oversize is rejected before reading; undeclared oversize is rejected
  by the bounded read. Redirect bodies are not read.
- After an unsuccessful observation, the remaining observation is recorded as
  blocked and no more requests are made. Limits are never enlarged.

Connection attempts are conservatively counted before dispatch. A failure
before an HTTP response may have sent no request or a request with no response;
the runner cannot distinguish those outcomes. Successful response and redirect
counts are recorded separately. Artifact writing occurs locally after the
bounded collection phase; it does not start another request.

## Evidence and interpretation

The saved report includes UTC start/completion times, request status, elapsed
times, bytes read, allowlisted response headers, selected sanitized metadata,
immutable observations, and the existing module's comparison results.

- `Modified` and `MetaDataModified` are retained as complete provider strings;
  no precision, timezone, or date-only conversion is imposed. Their provider
  meanings require separate certification before an ongoing collector uses them.
- `Title` is descriptive evidence only.
- HTTP `ETag`, when present, is evidence for this **metadata resource**, never
  proof about dataset records. `Last-Modified` is saved as an operational header
  but is not substituted for a provider field.
- Revision and schema slots are `NOT_CHECKED`: no revision interpretation is
  assumed and no schema request is made. Missing fields/headers are
  `NOT_PROVIDED`; malformed or unsafe strings are `INVALID`; a failed check
  yields `CHECK_FAILED` for attempted evidence. Deliberately skipped observations
  remain `BLOCKED`, with their reason preserved and all evidence `NOT_CHECKED`.
  Both observations use the same
  explicit slots and normalization versions.
- No raw error response, cookies, credentials, URL query values, or fragments
  are saved. Only selected metadata and header fields are retained. Unsafe
  evidence is invalidated rather than comparing redacted values as originals.

`POSSIBLY_UPDATED` would mean comparable metadata changed. `NO_CHANGE_DETECTED`
would describe the checked metadata only: a short interval can show no change,
and neither equality nor an HTTP validator proves content freshness or excludes
an intervening change that was reverted. Comparison never authorizes ingestion.

## Reproduce

From the repository root, using the existing project environment:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 python -m pytest -q -p no:cacheprovider \
  tests/unit/test_cbs_metadata_experiment.py tests/unit/test_monitoring.py \
  tests/unit/test_task2_url_safety.py
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=src \
  python experiments/cbs_metadata.py --live
```

The second command makes real public CBS metadata requests. Run it only with
explicit approval and permitted network access. Each invocation creates a new
`output/cbs-metadata-experiment/<UTC-time>-<random-suffix>/report.json`; `output/`
is already Git-ignored. It never overwrites a previous run. Exit code 1 means
collection failed or was blocked; inspect the saved observations and outcomes.
With persistence requested, storage errors take precedence and return code 2.
Excessively nested JSON is recorded as a failed observation with a `RecursionError`
diagnostic; the report is still saved without retaining the raw error body.

The JSON is a local experiment artifact, not a promised monitoring serialization
contract. A future collector still needs a stable source registry and verified
provider semantics. Observation IDs now include UUID4 values and remain distinct
from ingestion identity.

### Optional history across runs

Pass an explicit database filename to reuse earlier observations:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=src \
  python experiments/cbs_metadata.py --live --database /path/to/monitoring.duckdb
```

This command makes the same bounded live metadata requests. The database's parent
must already exist; no directory is created for it. A new database is initialized
automatically, and later invocations reopen it. Without `--database`, no monitoring
database is opened. The normal report directory is still created automatically.

The experiment validates stored history before collection and compares against
that history plus earlier attempts in the current run. Both observations are
saved after collection, including failed or blocked checks. `storage.saves` records
each ID's acknowledged save or storage error independently of the monitoring
outcome. Storage errors do not prove absence; reconcile the original observation
and ID after reopening before retrying a save. Running the experiment again makes
new observations, not a retry of the previous saves. There is no automatic retry.
Comparisons may include earlier attempts from this run even if their saves failed.

Open/read errors return code 2 without a completed report. Append errors are
included in the report and also return code 2. A close failure after collection
and saving preserves the completed report and is recorded separately in
`storage.close_error`; the CLI attempts to write the report and returns code 2.
The original observation payloads and IDs, comparisons, and individual save
outcomes remain unchanged for reconciliation. A close error cannot turn an
uncertain save into an acknowledged save. If report writing also fails with an
I/O error, code 2 is retained but a report file cannot be guaranteed. The network
time budget does not bound database operations. Crash and power-loss recovery
remain unverified.
See the [monitoring contract](v0.2-monitoring-contract.md#optional-cbs-experiment-persistence)
for the storage representation, reconciliation, and exit-code details.
