# Test case identity

Every test case has a stable ID. The ID names the case; it does not describe it.
This page specifies the ID grammar, how namespaces and serials are allocated, and when a case may change.
[`registry/registry.json`](../registry/registry.json) records the allocations and [`schemas/registry-1.schema.json`](../schemas/registry-1.schema.json) defines its structure.

## Grammar

```text
case-id   = family "." rules "." serial      ; asn1.der.000001
family    = lower *15(lower / digit)          ; asn1
rules     = lower *15(lower / digit)          ; der
serial    = 6digit, not "000000"              ; 000001 to 999999
```

- An ID has exactly three parts. `family.rules` is the namespace, for example `asn1.ber`, `asn1.cer` or `asn1.der`.
- The serial is zero-padded to six digits. `000000` is never allocated.
- The namespace is coarse on purpose. Finer taxonomy, such as length, boundary, indefinite-length or malformed, goes in `informative.tags`, which can change.
  `asn1.der.length.000001` is not a valid ID.
- Selection never relies on the ID. A case's encoding rules come from `normative.standard`, and its topic comes from tags.
  The checks require the standard to match the namespace, so the prefix is reliable, but it is still not a selection key.
- The check identity of one assertion is `<case-id>/<assertion-key>`, for example `asn1.der.000001/decode`.
  Reports count checks, not cases.

## Namespaces

A namespace is registered once, in a reviewed pull request that adds it to the registry with:

| Field | Meaning |
| --- | --- |
| `id` | `family.rules`, matching the grammar above |
| `purpose` | `conformance` for releasable cases, `example` for documentation and test examples |
| `title` | Human-readable name |
| `rules` | The encoding rule set every case in the namespace uses |
| `standards` | Registered standard editions the cases may cite |
| `allocated` | Highest serial ever allocated; `0` for a new namespace |
| `closed` | Optional `true` once no more serials may be allocated |

Rules:

- A namespace is never removed, renamed or given another `purpose` or `rules`.
  Its title may be corrected, and it may permit additional standard editions.
- The `example` family is reserved for namespaces whose purpose is `example`, and only they may use it.
  Example cases live in [`examples/`](../examples) and never appear in a release.
- Closing a namespace is permanent. A closed namespace keeps its cases but receives no new serials.
- Registered standard editions are never removed, and their document and edition never change.
  A new edition is a new registry entry, so an existing case keeps citing the edition it was written against.

## Serial allocation

1. Take the namespace's `allocated` value, add one, and use the result as the new serial.
2. Store the new value in `allocated` in the same pull request that adds the case.

Two pull requests that allocate in the same namespace change the same registry line, so Git reports a conflict and the second one must take the next serial.
`allocated` never decreases.
Because allocation is monotonic, deleting or invalidating a case never makes its serial available again, and an ID is never reused for different content.

A serial counts as allocated once it reaches the default branch.
A serial that only existed on an unmerged branch was never allocated.

`scripts/check_contract.py` implements the rules that do not depend on history: grammar, registered namespace, `serial <= allocated`, matching rules and standard, and unique IDs.
`check_registry_transition(old, new)` checks a registry change against the previous revision or the last release: no namespace, standard edition, reason code, capability, license or suite removed, no repurposed namespace, no lower allocation and no reopened or growing closed namespace.
`scripts/check_history.py` runs it in CI against the base of each change.

## Immutability and change policy

Each case separates `normative` content, which defines what the case means, from `informative` metadata, which may be edited at any time.
[The test case contract](test-case-contract.md) lists both sets of fields.

### From the first stable corpus release

The normative content of an ID is permanent.

- Any change to the input, standard, type, an assertion, its clauses or an expectation requires a new ID.
- The old case is not edited. It is deprecated, or invalidated when its expectation turns out to be wrong, by an event in the [lifecycle log](lifecycle.md) with its reason, review and optional replacement ID.
- Results recorded against an invalidated case are marked as affected; historical reports are never rewritten.
- Informative metadata, such as titles, tags, oracle rationale, review status and provenance details, may still change.
- The only in-place change is a schema migration that preserves the meaning, recorded as a `representation` revision.

### During 0.x

The corpus is still settling, so a normative change in place is allowed only under these conditions:

1. It is made in a reviewed pull request whose description lists each changed ID and the reason.
2. The same pull request appends a `revise` event for each changed case to the [lifecycle log](lifecycle.md#revise), naming the digests before and after the change.
   Results recorded against the previous definition are then marked as affected.
3. The CHANGELOG entry names the changed IDs.
4. The case keeps its subject. Correcting a wrong expectation, clause, type or mistyped input octet is allowed; repurposing the ID to test another rule or another input is not, and takes a new ID even during 0.x.
5. Serials are never reused; the allocation rules above apply from the first merge.

A case file is never deleted, in 0.x or later.
`scripts/check_history.py` enforces the revise events and the deletion rule against the base revision.

Published release snapshots are never modified.
A consumer pinned to a 0.x release digest therefore always sees the same definitions, even after an in-place change on the default branch.

## Reasons, capabilities, licenses and suites

The registry also holds four vocabularies that cases and plans reference by key:

- `reasons`: the expected-rejection taxonomy, such as `length.non-minimal`.
  A dotted code refines its parent, which must exist.
  Codes are never removed or renamed; a code can be marked `deprecated`.
- `capabilities`: conditions under which an assertion applies, such as `tag-number.over-31-bits`.
  They are never removed.
- `licenses`: SPDX license identifiers, with name and URL, whose terms permit redistributing case data in a [data release](release.md).
  Every license a case names must be registered, and licenses are never removed.
- `suites`: named selections that [execution plans](execution-plan.md#suites) share, such as `smoke`.
  A suite's selection may change; suites are never removed.

Adding a reason, capability, license or suite is a data change, not a schema change.
