# Test case and oracle contract

A test case states what a standard requires for one input, independently of any implementation.
This page specifies schema version 1, defined in [`schemas/case-1.schema.json`](../schemas/case-1.schema.json).
[`examples/valid`](../examples/valid) holds a complete example of every construct, and [`examples/invalid.json`](../examples/invalid.json) shows the rules each part enforces.

## Shape

```json
{
  "schema": "urn:opentlv:corpus:schema:case:1",
  "id": "asn1.der.000001",
  "normative": {
    "standard": {"ref": "itu-t-x690-2021-02", "rules": "der"},
    "input": {"octets": "020105"},
    "assertions": [
      {
        "key": "decode",
        "request": {"operation": "decode", "strictness": "strict", "view": "tree"},
        "clauses": ["8.3.1", "10.1"],
        "expect": {"result": {"outcome": "accept", "tree": {"class": "universal", "constructed": false, "number": "2", "contents": "05"}}}
      }
    ]
  },
  "informative": {
    "title": "...", "description": "...", "tags": ["primitive", "integer"], "related": ["asn1.ber.000001"],
    "oracle": {"source": "specification", "rationale": "...", "review": {"status": "self-reviewed", "reviewers": ["..."], "date": "2026-11-06"}},
    "provenance": {"origin": "authored", "authors": ["..."], "license": "MIT"}
  }
}
```

`type`, `requires`, `description` and `related` are optional. [Identity](identity.md) specifies the `id`.

## Normative and informative fields

| Member | Kind | Contents |
| --- | --- | --- |
| `schema` | Format | Schema version of the document |
| `id` | Identity | Stable case ID |
| `normative` | Normative | `standard`, `type`, `input`, `assertions` with their requests, clauses, required capabilities and expectations |
| `informative` | Informative | `title`, `description`, `tags`, `related`, `oracle`, `provenance` |

Everything under `normative` defines what the case means and is the content the [canonical definition digest](digest.md) covers.
Changing it is governed by the [change policy](identity.md#immutability-and-change-policy).
Everything under `informative` may be edited without changing the meaning.
No field appears in both, and the schema rejects unknown fields anywhere, so an expectation can never hide in metadata.

The `oracle` evidence and the review status are informative because they record how far a case has been checked, which can improve while the meaning stays the same.
They are still mandatory.

## Standard

`standard.ref` names an edition registered in [`registry/registry.json`](../registry/registry.json), and `standard.rules` names an encoding rule set of that edition, such as `ber`, `cer` or `der`.
The case's namespace must permit the edition and use the same rules.
Clause references in assertions are clauses of this edition.

## Input

A case has exactly one subject, and every assertion applies to it:

| Input | Contents | Used by |
| --- | --- | --- |
| `octets` | Byte string to decode | `decode`, `round-trip` |
| `tree` | Canonical TLV tree to encode | `encode`, `round-trip` in the tree view |
| `value` | Typed value to encode; needs `type` | `encode`, `round-trip` in the value view |

The same bytes under other rules, such as BER and DER, are separate cases in their own namespaces that can reference each other in `related`.

## Assertions

Each assertion is one check, identified as `<id>/<key>`.

### Request

The request holds the execution parameters an adapter needs.

- `operation`
  - `decode`: decode the input octets as exactly one complete encoding. Octets left after it are an error (`input.trailing`).
  - `encode`: encode the input tree or value.
  - `round-trip`: decode the input octets, or encode the input tree or value, then perform the opposite operation and compare.
- `strictness`
  - `strict`: the implementation enforces every requirement of the encoding rules.
  - `lenient`: the implementation may tolerate violations it can recover from, such as BER framing in a DER decoder.
    The standards do not define lenient behavior, so a lenient expectation usually lists the permitted results with the `implementation-defined` basis.
- `view`
  - `tree`: the generic TLV tree. Every TLV implementation can provide it.
  - `value`: typed ASN.1 values; the case must declare `type`.

An implementation that does not support the operation, strictness or view of an assertion reports it as unsupported, never as passed.

### Clauses and capabilities

`clauses` lists the clauses of the referenced edition that the assertion checks, for example `8.1.3.5` or `8.1.3.2 b)`.
At least one is required.

`requires` lists registered capabilities beyond the request, for example `tag-number.over-31-bits`.
An implementation that does not declare all of them reports the assertion as not applicable.
This keeps documented implementation limits separate from standards violations.

### Expectations

An expectation is either one result or a set of correct results:

```json
{"result": {"outcome": "accept", "octets": "02020080"}}
{"one_of": [{"outcome": "reject", "reason": "length.non-minimal"}, {"outcome": "reject", "reason": "input.truncated"}],
 "basis": "permitted-alternatives"}
```

`basis` states why more than one result is correct:

- `permitted-alternatives`: the standard or the operation contract allows each listed result, such as either reason for an input that violates two rules.
- `implementation-defined`: the standard leaves the behavior to the implementation, such as lenient decoding.

Results by operation:

| Operation | Accepting result | Meaning |
| --- | --- | --- |
| `decode` | `{"outcome": "accept", "tree": node}` | The decoded tree equals `node` (tree view) |
| `decode` | `{"outcome": "accept", "value": value}` | The decoded value equals `value` (value view) |
| `encode` | `{"outcome": "accept", "octets": hex}` | The encoding is exactly these octets |
| `round-trip` | `{"outcome": "accept", "preserves": "octets"}` | Re-encoding reproduces the input octets |
| `round-trip` | `{"outcome": "accept", "preserves": "tree"}` or `"value"` | The second result equals the first in the request's view |

Every operation can also expect `{"outcome": "reject", "reason": code}`.

Where the rules allow many encodings of one input, as BER does for lengths and constructed forms, the case asserts a round-trip property instead of listing octets.
An `encode` expectation lists only octets the rules make exact, or a complete list of alternatives.

### Rejection reasons

`reason` is a code from the registry's `reasons` taxonomy, such as `length.non-minimal` or `input.truncated`.
A dotted code refines its parent: `length.non-minimal` is a kind of `length`.
Reasons never use native error codes, names or messages of an implementation.
Adapters keep their native diagnostics and map them to the taxonomy, so a report shows both.

A rejection passes on its outcome.
Whether the reported reason matches the expected code exactly, only by its parent category or not at all is reported separately, so a library with coarse errors is not failed for its error granularity.

## Canonical representations

All normative content uses one representation per value, so its canonical JSON, and the digest of it, is deterministic.
None of these forms depends on JSON, so a future static-table or binary export can carry the same data on a device without a JSON parser.

| Datum | Representation |
| --- | --- |
| Byte string | Lowercase hexadecimal, two digits per octet, no separators: `0101ff` |
| Integer of unbounded size | Decimal string without leading zeros or `+`: `"-129"`, `"2147483648"` |
| Small bounded integer | JSON integer, only `unused_bits` (0 to 7) |
| Text | JSON string of Unicode scalar values |
| Ordered collection | JSON array |
| Floating point | Not used |

Tag numbers, INTEGER and ENUMERATED values and OBJECT IDENTIFIER arcs are strings, so no value depends on the precision of a JSON number.

### TLV tree

```json
{"class": "universal", "constructed": true, "number": "16", "children": [
  {"class": "universal", "constructed": false, "number": "2", "contents": "05"}
]}
```

- `class` is `universal`, `application`, `context` or `private`.
- A primitive node has `contents`, a constructed node has `children`; never both.
- The tree records the logical structure only. Identifier and length encodings, such as the high-tag-number form or an indefinite length, follow from the input octets and are checked by accepting or rejecting them, not stored in the tree.

### Typed values

`type` describes the ASN.1 type of the value view:

```json
{"kind": "sequence", "fields": [
  {"name": "flag", "type": {"kind": "boolean"}, "default": {"kind": "boolean", "value": false}},
  {"name": "count", "type": {"kind": "integer", "tag": {"class": "context", "number": "0", "mode": "implicit"}}, "optional": true}
]}
```

| Kind | Value |
| --- | --- |
| `boolean` | `{"kind": "boolean", "value": true}` |
| `integer`, `enumerated` | `{"kind": "integer", "value": "-1"}` |
| `null` | `{"kind": "null"}` |
| `octet_string` | `{"kind": "octet_string", "hex": "aa"}` |
| `bit_string` | `{"kind": "bit_string", "hex": "a0", "unused_bits": 5}` |
| `object_identifier` | `{"kind": "object_identifier", "value": "1.2.840.113549"}` |
| `ia5_string`, `utf8_string` | `{"kind": "utf8_string", "value": "text"}` |
| `sequence`, `set` | `{"kind": "sequence", "fields": [{"name": "flag", "value": ...}]}` |
| `sequence_of`, `set_of` | `{"kind": "sequence_of", "items": [...]}` |
| `choice` | `{"kind": "choice", "alternative": "number", "value": ...}` |

- A type's universal tag follows from its kind. `tag` overrides it implicitly or explicitly; a CHOICE can only be tagged explicitly.
- Fields appear in the declared order. Absent optional fields are omitted, and so is a field whose value equals its default, so each value has one representation.
- `set_of` items are compared as a multiset, in the order they were encoded.

## Oracle contract

Every expectation comes from an independent oracle: a standard, a published test vector or the corpus operation contract.
It never comes from what an implementation does.

| Field | Contents |
| --- | --- |
| `source` | `specification`, `published-vector` or `operation-contract` |
| `rationale` | How each expectation follows from the cited clauses, vector or contract |
| `vectors` | Publisher, name, HTTPS URL, revision and license of each vector; required for `published-vector`, otherwise absent |
| `corroboration` | Optional implementations that were observed to agree or disagree |
| `review` | `status`, `reviewers` and `date` |

- `operation-contract` covers expectations that follow from this contract instead of a clause, such as rejecting trailing octets after one complete encoding.
- `corroboration` is evidence that an expectation was cross-checked, never its source.
  There is deliberately no `cross-implementation` source: agreement between libraries, however many, is not normative proof, and a shared bug produces agreement too.
- Review statuses:
  - `self-reviewed`: checked only by its authors. All reviewers must be authors.
  - `peer-reviewed`: checked against the cited clauses by at least one reviewer who is not an author.

### Provenance and licensing

`provenance` records `origin` (`authored`, `migrated` or `imported`), `authors` and the SPDX `license` of the case data.
Migrated and imported cases also record their `source` repository, revision, path and original ID.

The `license` of the provenance and of each published vector is an SPDX expression of license identifiers combined with `AND`, `OR` and parentheses, such as `MIT` or `MIT OR CC0-1.0`.
Every identifier must be registered in the registry's `licenses`, which lists the licenses that permit redistributing case data in a [data release](release.md).
A case whose data or source vectors may not be redistributed therefore cannot enter the corpus.
Untriaged or security-sensitive fuzzing material is never imported into a public release.

## Oracle-blind requests

An adapter receives only the oracle-blind view of one assertion: `standard`, `type`, `input` and that assertion's `request`.
It never receives `expect`, `clauses`, `requires`, `informative` or any other field, and not the case ID either, which would let it look up the expectation.
The harness relates each answer to its check by a request ID of its own.
`oracle_blind_view` in [`scripts/check_contract.py`](../scripts/check_contract.py) builds the view from this allow list rather than by removing fields, so a field added later stays hidden until the contract admits it.
The schema keeps the blind parts closed: `request` and `input` reject unknown fields, so no expectation can be placed inside them.
The [adapter process protocol](adapter-protocol.md) sends exactly this view as the parameters of an `execute` request.
The verdict engine is the only component that reads expectations.

## Checks

`python scripts/check_contract.py` validates the registry and the examples.
Besides the JSON Schema it checks:

- the case's namespace is registered, its serial allocated, and its standard and rules permitted there;
- assertion keys are unique, and each operation, view, input and result fit together;
- reason codes and capabilities are registered;
- typed values and defaults conform to `type`, no field repeats its default, and a declared `type` is used;
- a self-review is by authors only and a peer review includes a non-author;
- every license is a registered license in a valid expression;
- across cases, IDs are unique and `related` IDs exist.

[`scripts/validate.py`](../scripts/validate.py) applies the same rules to every case of the corpus, together with the file, encoding, path, digest and lifecycle checks of [Corpus validation](validation.md).

The schema patterns use ECMA-262 semantics, so a string with a trailing newline never matches an anchored pattern.
