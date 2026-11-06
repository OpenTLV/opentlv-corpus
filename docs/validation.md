# Corpus validation

[`scripts/validate.py`](../scripts/validate.py) checks a corpus tree in one pass: the source repository, or a [data release](release.md) extracted from its archive.
It runs every rule that one revision can answer.
The rules that compare a change with its base revision run in [`scripts/check_history.py`](../scripts/check_history.py).

```sh
python scripts/validate.py          # the repository
python scripts/validate.py <root>   # another corpus tree, such as an extracted release
```

## Diagnostics

Each problem is reported as one line:

```text
<path>: <code>: <detail>
```

- `path` is the file relative to the corpus root, with `/` separators.
- `code` is a stable identifier from the tables below. Tools and tests match the code, never the detail.
- `detail` names the offending value, location or rule.
  A schema error gives the JSON Pointer of the failing member and the schema keyword, such as `/normative/input/octets (pattern)`.

Diagnostics are sorted by path, code and detail, and an identical line is reported once.
Two runs over the same files therefore print the same bytes, whatever the platform, directory order or file times.
The validator exits with status 1 when it reports anything.

A document that cannot be read or does not match its schema is not checked further, and is left out of the cross-file checks and the lifecycle replay.

## Case sets

| Set | Cases | Lifecycle log | Namespace purpose | Checked |
| --- | --- | --- | --- | --- |
| Conformance | `cases/<namespace>/<id>.json` | `registry/lifecycle.json` | `conformance` | Always |
| Examples | `examples/valid/*.json` | `examples/lifecycle.json` | `example` | When present |

A data release holds only the conformance set.

## Files and encoding

Every JSON document under `schemas/`, `registry/`, `cases/`, `examples/` and `releases/` is checked byte by byte before it is parsed, so a case file, and a release archive containing it, has the same bytes on every platform.

| Code | Rule |
| --- | --- |
| `encoding-invalid` | The document is UTF-8. |
| `encoding-bom` | No byte order mark precedes the document. |
| `encoding-line-ending` | Lines end with LF; there is no carriage return. |
| `encoding-final-newline` | The document ends with a line feed. |
| `json-invalid` | The document is JSON, with no repeated member names and no `NaN` or `Infinity`. |
| `file-missing` | A required file exists: `LICENSE`, the registry, the conformance lifecycle log, a schema file this tooling supports, or a file of a release record. |
| `unexpected-file` | Nothing else is stored where corpus documents are expected, such as a non-JSON file under `cases/` or `schemas/`. |

## Schemas

Documents are validated against the schema files of the tree being checked, so a release is validated against the schemas it ships.

| Code | Rule |
| --- | --- |
| `schema-id-mismatch` | `schemas/<kind>-<version>.schema.json` has the `$id` `urn:opentlv:corpus:schema:<kind>:<version>`. |
| `schema-invalid` | The schema file is a valid JSON Schema draft 2020-12 schema. |
| `schema-unsupported` | The document declares the schema version this tooling checks for its kind: case, registry, lifecycle log or manifest version 1. The plan and resolved plan schema files are required and checked too; plans are checked by [`scripts/plan.py`](../scripts/plan.py). |
| `schema` | The document matches its schema. |

## Registry

The registry rules of [Identity](identity.md#namespaces): `registry-duplicate`, `namespace-purpose`, `namespace-standard-unknown`, `namespace-rules-unsupported` and `reason-parent-missing`.

The [suites](execution-plan.md#suites) are checked against the registry and the conformance set:

| Code | Rule |
| --- | --- |
| `suite-standard-unknown`, `suite-rules-unknown` | A suite selects only registered standard editions and rule sets. |
| `suite-case-unknown` | Each case or check a suite names is in the conformance set. |
| `suite-case-excluded` | Each case or check a suite names is selected by the suite's other conditions, such as its lifecycle statuses. |

## Cases

Every case is checked against the [test case contract](test-case-contract.md#checks), which reports:

| Area | Codes |
| --- | --- |
| IDs and allocation | `id-invalid`, `namespace-unallocated`, `serial-unallocated` |
| Standard editions and rules | `standard-unknown`, `standard-not-permitted`, `rules-unknown`, `rules-mismatch` |
| Assertions | `duplicate-assertion-key`, `input-operation-mismatch`, `result-view-mismatch`, `reason-unknown`, `capability-unknown` |
| Types and values | `type-unused`, `type-duplicate-name`, `value-type-mismatch`, `value-not-canonical` |
| Oracle review | `review-status-mismatch`, `review-not-independent` |
| References | `related-self`, `related-example` |
| Licenses | `license-expression-invalid`, `license-unregistered` |

The validator adds the rules that depend on where a case is stored and on the other cases of its set:

| Code | Rule |
| --- | --- |
| `case-path` | A conformance case is stored at `cases/<namespace>/<id>.json`. |
| `case-set-mismatch` | Conformance cases use conformance namespaces, and examples use example namespaces. |
| `duplicate-id` | An ID occurs once in its set. Every file with a repeated ID is reported. |
| `related-unknown` | Each `related` ID is a case of the same set. |
| `not-canonicalizable` | The definition is in the [canonical JSON subset](digest.md#canonical-json-subset), so it has a digest. For example, the schema accepts `5.0` as an integer, but the digest does not. |

## Lifecycle

Each lifecycle log is replayed over the cases of its set, with the rules of [Lifecycle](lifecycle.md#checks): `event-sequence`, `event-case-unknown`, `event-digest-mismatch`, `transition-forbidden`, `replacement-unknown`, `replacement-cycle`, `replacement-inactive`, `revision-after-stable`, `revision-unchanged`, `review-status-mismatch` and `review-not-independent`.
A case definition changed without a `revise` event shows up as `event-digest-mismatch` on every event that names it.

## Release records

Each [published release](release.md#published-releases) under `releases/<version>/` is checked for consistency.

| Code | Rule |
| --- | --- |
| `release-version-invalid` | The directory name is a Semantic Versioning 2.0.0 version without build metadata. |
| `release-record-mismatch` | The manifest names the release of its directory, and `SHA256SUMS` has the SHA-256 of `manifest.json`. |
| `release-record-incomplete` | `SHA256SUMS` lists exactly the archive and the manifest of the release. |
| `checksums-invalid` | `SHA256SUMS` is ASCII lines of `<sha256>  <name>`. |
