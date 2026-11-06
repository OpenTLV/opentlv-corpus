# Execution plans

An execution plan says which checks to run against one adapter.
Resolving it against one data release and one adapter's capability descriptor gives a resolved execution plan: the exact checks, the definitions they were resolved against, which of them the adapter supports, and everything else a run depends on.
This page specifies schema version 1 of both documents:

| Schema | URN | File |
| --- | --- | --- |
| Execution plan | `urn:opentlv:corpus:schema:plan:1` | [`schemas/plan-1.schema.json`](../schemas/plan-1.schema.json) |
| Resolved execution plan | `urn:opentlv:corpus:schema:resolution:1` | [`schemas/resolution-1.schema.json`](../schemas/resolution-1.schema.json) |

[`scripts/plan.py`](../scripts/plan.py) holds the typed, immutable Python model of both documents, the resolver and the check a runner makes before it runs a resolved plan.
[`examples/plans/`](../examples/plans) holds example plans.

## Principles

- **Declarative.** A plan is data: conditions on the cases, not code or a query language.
- **Never by results.** Selection reads the case definitions, their tags and their lifecycle status, and nothing else.
  No plan can select or drop a check because it passed, failed or crashed before, so a run never hides what an earlier run found.
- **One oracle.** The verdict of every check comes from the corpus expectation.
  A plan has no comparison mode; differential analysis between implementations is built from the observations, outside the corpus.
- **Nothing disappears silently.** Checks the adapter does not support stay in the resolved plan, marked unsupported.
  A case or check a plan names that is then not selected, a misspelt standard edition or rule set and a plan that selects nothing are errors.
- **Pinned.** A resolved plan names one release by its archive SHA-256, one adapter by the digest of its descriptor, and every check by its definition digest.
  A later release, a new case or a changed adapter never changes it; running it against anything else fails before the first request.

## Plan

```json
{
  "schema": "urn:opentlv:corpus:schema:plan:1",
  "name": "core-der",
  "description": "Strict DER checks of OpenTLV Core.",
  "corpus": {"release": "0.1.0", "sha256": "<SHA-256 of opentlv-corpus-data-0.1.0.tar>"},
  "select": {"rules": ["der"], "strictness": ["strict"]},
  "limits": {"execute_timeout_ms": 30000}
}
```

| Member | Contents |
| --- | --- |
| `name` | Name of the plan, reported with its results |
| `description` | Optional text for people |
| `corpus` | Optional pin: the release version and the SHA-256 of its archive, as in its `SHA256SUMS`. Resolution fails against any other release. |
| `select` | The [selection](#selection) |
| `limits` | Optional [harness limits](adapter-protocol.md#limits) in integer milliseconds and octets: `hello_timeout_ms`, `execute_timeout_ms`, `exit_timeout_ms`, `max_message_octets`, `stderr_octets`. An absent limit has the protocol default. |

A consumer's CI pins the corpus by the release version and archive SHA-256 from the [release record](release.md#published-releases), never by a moving name such as `latest`.
The pin in the plan makes the plan itself refuse any other release.

### Selection

A check is one assertion of a case, `<case-id>/<assertion-key>`.
A check is selected when it meets every condition the selection has; a list condition holds when the check matches one of its items.
An empty selection, `{}`, selects every check of every active case.

| Condition | The check is selected when |
| --- | --- |
| `cases` | Its case ID or its check ID is listed. Each listed ID must exist, and must be selected by the other conditions: a case ID by at least one of its checks. |
| `standards` | Its case cites one of these registered standard editions |
| `rules` | Its case uses one of these encoding rule sets, such as `der` |
| `operations` | Its request has one of these operations: `decode`, `encode`, `round-trip` |
| `strictness` | Its request has one of these strictness modes: `strict`, `lenient` |
| `views` | Its request has one of these views: `tree`, `value` |
| `tags` | Its case has at least one of these tags |
| `exclude_tags` | Its case has none of these tags |
| `lifecycle` | Its case has one of these [statuses](lifecycle.md#statuses-and-events): `active`, `deprecated`. Only `active` when absent. |
| `suite` | It is also selected by the selection of this [suite](#suites). Only plans have this condition. |

- Invalid cases are never selected: their expectations are wrong, so no verdict against them means anything.
  The lifecycle projection of the release decides each status.
- Selection never uses ID prefixes, as [Identity](identity.md#grammar) requires; `cases` lists exact IDs.
- `standards` and `rules` must be registered, so a typing error is reported instead of selecting nothing.
  Tags are free metadata and are not checked.

### Suites

A suite is a named selection in the `suites` section of [`registry/registry.json`](../registry/registry.json), shared by every consumer of a release:

```json
{"id": "smoke", "title": "Quick pull request check: the active cases tagged smoke", "select": {"tags": ["smoke"]}}
```

| Suite | Selection | Used for |
| --- | --- | --- |
| `smoke` | The active cases tagged `smoke` | Pull request and fast CI runs |

A case joins the smoke suite by its `smoke` tag.
A plan uses a suite with `"suite": "smoke"` and may narrow it with further conditions; a full run selects every active check with `{}`.
Suites are registry data: adding one is a data change, a suite is never removed, and its selection may change in a reviewed pull request.
The [validator](validation.md#registry) checks that every case a suite names exists and is selected by the suite, so a suite in a release always resolves.

## Resolution

```sh
python scripts/plan.py resolve --corpus opentlv-corpus-data-0.1.0.tar --sums SHA256SUMS \
  --plan plan.json --output resolved.json -- <adapter command>
```

Resolution reads three inputs and nothing else:

1. **The release.** The archive is [verified](release.md#verification), with its `SHA256SUMS` when given, and a release with any diagnostic is refused.
2. **The plan.** It must match its schema, its pin must name the release, its suite must be registered, and its standard editions, rule sets and named IDs must exist.
3. **The capability descriptor.** It comes from the [handshake](adapter-protocol.md#hello) of the adapter command, or from a file with `--descriptor`.
   Its protocol version must be one the tooling speaks, and it must match the capability descriptor schema.

The resolver then walks the conformance cases of the release in ID order, and the assertions of each case in their order, and keeps every selected check.
For each one it decides applicability as the [harness](adapter-protocol.md#classification) does before sending a request:

| Applicability | Problem | Meaning |
| --- | --- | --- |
| `applicable` | | The descriptor covers the request |
| `unsupported` | `undeclared` | No `supports` entry covers the standard, rules, operation, strictness and view |
| `unsupported` | `capability` | The assertion requires a capability the descriptor does not provide |
| `unsupported` | `input-too-large` | The octets input is longer than `max_input_octets` |
| `unsupported` | `too-deep` | The tree or value input is nested deeper than `max_depth` |

An unsupported check is recorded, not dropped, so a report counts it in its denominator.
A check can still turn out unsupported or not comparable during the run, when the adapter answers so or the request line exceeds the adapter's message limit; the observations record that.

Resolution fails, without writing anything, with one `<code>: <detail>` line per problem:

| Code | Problem |
| --- | --- |
| `release-invalid` | The release archive or its checksums do not verify |
| `json-invalid`, `schema-unsupported`, `schema`, `not-canonicalizable` | The plan is not JSON, not plan schema version 1, does not match it, or is outside the [canonical JSON subset](digest.md#canonical-json-subset), such as `5.0` as a limit |
| `corpus-mismatch` | The plan pins another release version or archive digest |
| `protocol-unsupported`, `descriptor-invalid` | The descriptor's protocol version is not supported, or the descriptor does not match its schema |
| `adapter-start-failed` | The adapter command did not start or complete the handshake |
| `suite-unknown`, `standard-unknown`, `rules-unknown` | The plan names an unregistered suite, standard edition or rule set |
| `case-unknown`, `assertion-unknown` | A case or check the plan names is not in the release |
| `explicit-excluded` | A case or check the plan names is not selected by its other conditions, for example a deprecated case without `"lifecycle": ["active", "deprecated"]` |
| `selection-empty` | The plan selects no check |

## Resolved execution plan

```json
{
  "schema": "urn:opentlv:corpus:schema:resolution:1",
  "plan": {"name": "core-der", "digest": "sha256:..."},
  "corpus": {"release": "0.1.0", "sha256": "...", "revision": "..."},
  "tools": {"name": "opentlv-corpus-tools", "version": "0.1.0-dev"},
  "adapter": {"protocol": 1, "adapter": {"name": "...", "version": "..."},
              "implementation": {"name": "...", "version": "..."}, "descriptor": "sha256:..."},
  "limits": {"hello_timeout_ms": 10000, "execute_timeout_ms": 30000, "exit_timeout_ms": 5000,
             "max_message_octets": 4194304, "stderr_octets": 65536},
  "checks": [
    {"case": "asn1.der.000001", "assertion": "decode", "digest": "sha256:...", "status": "active",
     "applicability": "applicable"},
    {"case": "asn1.der.000002", "assertion": "decode", "digest": "sha256:...", "status": "active",
     "applicability": "unsupported", "problem": "undeclared", "detail": "itu-t-x690-2021-02 der decode strict value"}
  ]
}
```

| Member | Contents |
| --- | --- |
| `plan` | The plan name and the digest of the plan document |
| `corpus` | The release version, the SHA-256 of its archive and the source revision from its manifest |
| `tools` | The tooling that resolved the plan |
| `adapter` | The protocol version, adapter and implementation of the descriptor, and the digest of the complete descriptor |
| `limits` | Every limit the run enforces, defaults included |
| `checks` | Every selected check in resolution order: case ID, assertion key, [definition digest](digest.md), lifecycle status and applicability, with the problem and detail of an unsupported one |

- Digests of documents, `plan.digest` and `adapter.descriptor`, are `sha256:` and the SHA-256 of the document's RFC 8785 canonical JSON, like definition digests, so formatting and member order never change them.
- The **identity** of a resolved plan is the same digest of the resolved plan itself.
  The document contains no resolution time or other varying member, so resolving the same plan against the same release and descriptor gives the same bytes and the same identity, and serializing and loading it again keeps it.
- The checks are a snapshot.
  A later release with new cases, a changed lifecycle status or a revised definition gives a different resolution, while the existing resolved plan keeps naming exactly the checks and digests it was resolved with.
- A result names its check and the definition digest, so the [lifecycle projection](lifecycle.md#historical-results) recognizes results of definitions found wrong later.
- The resolved plan holds no case content and no expectation; the runner reads both from the pinned release.

## Checking before a run

```sh
python scripts/plan.py check --corpus opentlv-corpus-data-0.1.0.tar --resolved resolved.json -- <adapter command>
```

A runner checks a resolved plan against the release and the adapter it is given before sending the first request, and does not run it on any problem:

| Code | Problem |
| --- | --- |
| `schema-unsupported`, `schema`, `not-canonicalizable` | The document is not resolved plan schema version 1 or does not match it |
| `corpus-mismatch` | The release version, archive digest or revision differs from the resolved one |
| `protocol-unsupported`, `descriptor-invalid` | The resolved or given protocol version is not supported, or the descriptor does not match its schema |
| `adapter-mismatch` | The descriptor differs from the resolved one, even only in a version, capability or limit |
| `check-unknown` | A check is not in the release |
| `check-order` | A check is repeated or out of resolution order |
| `check-mismatch` | A check's digest, status or applicability differs from what resolution gives, for example after an edit |

## Embedded use

Plans and resolved plans use only strings, integers and lists in the canonical JSON subset, and a resolved plan is a flat list of check IDs and digests.
A future static-table export can therefore carry a resolved plan to a device that cannot parse JSON or resolve plans itself.
