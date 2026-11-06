# Lifecycle

A published test case is never deleted or silently rewritten.
When a case is superseded or turns out to be wrong, an event is appended to a lifecycle log, and the case's status follows from its definition and those events.
This page specifies schema version 1 of the log, defined in [`schemas/lifecycle-1.schema.json`](../schemas/lifecycle-1.schema.json).

## Three kinds of data

| Data | Where | Changes |
| --- | --- | --- |
| Normative definition | `schema`, `id` and `normative` of the case file | Never after the first stable release; during 0.x only through a `revise` event |
| Informative metadata | `informative` of the case file | Freely, in reviewed pull requests |
| Lifecycle events | The lifecycle log | Appended only |

The [definition digest](digest.md) identifies the normative definition.
Each event names the digest it applies to, so the log and the definitions can always be checked against each other.

## Logs

| Log | Covers |
| --- | --- |
| [`registry/lifecycle.json`](../registry/lifecycle.json) | Conformance cases under `cases/` |
| [`examples/lifecycle.json`](../examples/lifecycle.json) | The example cases under [`examples/valid/`](../examples/valid) |

An event can only refer to a case covered by its own log, so an example can never deprecate or replace a releasable case.

```json
{
  "schema": "urn:opentlv:corpus:schema:lifecycle:1",
  "phase": "0.x",
  "events": [
    {
      "seq": 1,
      "case": "example.der.000007",
      "digest": "sha256:ebe77355bbe8461b4383d66fb4a842495fd56a747bd0fa143ce72dc6dc0f3e9d",
      "event": "invalidate",
      "reason": {"code": "expectation-wrong", "detail": "..."},
      "replacement": "example.der.000008",
      "actor": "Marek Cingel",
      "review": {"status": "self-reviewed", "reviewers": ["Marek Cingel"], "date": "2026-11-06"},
      "reference": "https://github.com/OpenTLV/opentlv-corpus/pull/..."
    }
  ]
}
```

- `seq` is the event's position in the log, starting at 1. Reports refer to events by it.
- `digest` is the digest of the definition the event applies to.
- `reason` has a `code` from the table below and a `detail` that explains the specific case.
- `actor` decided the event. `review` follows the same rules as an oracle review: a `self-reviewed` event is reviewed by its actor only, and a `peer-reviewed` event has at least one reviewer who is not the actor.
- `reference` optionally links the issue or pull request with the discussion.
- `replacement` and `revised_digest` depend on the event.

## Statuses and events

A case with no events is `active`.

| Event | From | To | Reason codes | Replacement |
| --- | --- | --- | --- | --- |
| `deprecate` | `active` | `deprecated` | `superseded`, `duplicate`, `out-of-scope` | Required for `superseded` and `duplicate`, otherwise optional |
| `invalidate` | `active`, `deprecated` | `invalid` | `expectation-wrong`, `definition-wrong` | Optional |
| `reinstate` | `deprecated` | `active` | `deprecation-withdrawn` | None; clears the previous replacement |
| `revise` | `active`, `deprecated` | unchanged | `expectation-wrong`, `definition-wrong`, `representation` | None |

- `deprecated`: the case is correct, but should no longer be selected by default, for example because another case covers the same assertion better.
- `invalid`: the case does not state what the standard requires. `invalid` is terminal; the definition stays in the corpus so recorded results can still be traced to it.
- `expectation-wrong`: an expectation, clause or reason does not follow from the standard.
- `definition-wrong`: the input, type or request does not express what the case is meant to test.

### Revise

A `revise` event records that a definition was changed in place.
`digest` names the definition before the change and `revised_digest` the definition after it.

- `expectation-wrong` and `definition-wrong` revisions correct the meaning and are allowed only while the log's `phase` is `0.x`, under the [0.x change policy](identity.md#during-0x).
- `representation` revisions record a schema migration that preserves the meaning, as [schema compatibility](compatibility.md) requires. They are allowed in every phase.

A case that is invalid is never revised; its replacement is a new case.

## Replacements

A replacement names the case to use instead.
A case's replacement is the one named by its latest `deprecate` or `invalidate` event; `reinstate` clears it.
When the event is recorded, the replacement must exist in the same log's cases, be `active`, and not lead back to the replaced case through existing replacements.
If the replacement is later deprecated or invalidated, its own event names the next replacement, and consumers follow the chain.
Because every replacement was active when it was recorded and statuses only leave `active` through events, the chains never form a cycle.

## Phase

`phase` is `0.x` until the first stable corpus release and `stable` from then on.
The pull request that prepares the first stable release changes it, and a log never returns from `stable` to `0.x`.
From then on the normative definition of an ID is permanent: a correction is a new case, and the old one is invalidated with the new one as its replacement.

## Projection

Replaying a log over the current definitions gives its projection:

```json
{
  "phase": "0.x",
  "cases": [
    {"id": "example.der.000007", "status": "invalid", "digest": "sha256:...", "replacement": "example.der.000008", "events": [2]},
    {"id": "example.der.000008", "status": "active", "digest": "sha256:..."}
  ],
  "tainted": [{"case": "example.der.000007", "digest": "sha256:...", "event": 2}]
}
```

- `cases` lists every case of the log's set, ordered by ID, with its status, current digest, replacement and the events that changed it.
- `tainted` lists the definitions whose expectations were found wrong: every `invalidate` event and every `expectation-wrong` or `definition-wrong` revision, in log order.

The projection is a pure function of the definitions and the events, so anyone holding both reproduces it exactly.
A [data release](release.md) ships the projection of the conformance log as `lifecycle-projection.json`.
[`examples/lifecycle-projection.json`](../examples/lifecycle-projection.json) is the projection of the example log, and `python scripts/check_lifecycle.py --projection examples` prints it.

## Historical results

Reports are never rewritten.
A recorded result names the case ID and the definition digest it was computed against.
When `tainted` lists that pair, the result is marked as affected: its verdict came from a wrong oracle and does not count as evidence for or against the implementation.
Because raw observations are kept separately from verdicts, an affected result can be re-evaluated against the replacement or the revised definition without running the implementation again.

## Checks

`python scripts/check_lifecycle.py` checks both logs against the current definitions:

- the log matches the schema, which also fixes the reason codes, replacements and revised digests each event allows;
- `seq` equals the position, every event refers to a case of its own set, and every digest names the definition the event applies to, tracking revisions back from the current definitions;
- each transition starts from a permitted status;
- replacements exist, are active and form no cycle;
- revisions change the digest, and only `representation` revisions occur in the `stable` phase;
- the review is by the actor only when self-reviewed, and includes someone else when peer-reviewed;
- the example projection and the [digest vectors](digest.md#vectors) match their committed files;
- each mutation in [`examples/lifecycle-invalid.json`](../examples/lifecycle-invalid.json) breaks exactly the rule it declares.

`python scripts/check_history.py --base <revision>` checks a change against a base revision, which CI sets to the base of a pull request or the previous head of a push:

- every event at the base is still present and unchanged, and the log was not removed;
- the phase did not return from `stable` to `0.x`;
- no case file at the base was deleted;
- a case whose definition changed has a `revise` event appended in the same change, and the first appended event of an existing case names its digest at the base;
- the registry transition rules of [Identity](identity.md#serial-allocation) hold;
- no [published release record](release.md#published-releases) was changed or removed.
