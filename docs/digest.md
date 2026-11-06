# Canonical definition digest

Every test case has a definition digest that identifies its normative content exactly.
Execution plans, reports and the [lifecycle log](lifecycle.md) record the digest next to the case ID, so a result always names the definition it was computed against.
[`scripts/canonical.py`](../scripts/canonical.py) implements the digest, and [`examples/digests.json`](../examples/digests.json) holds vectors for other implementations.

## Definition

```text
digest = "sha256:" + lowercase-hex(SHA-256(UTF-8(JCS(definition))))
definition = {"schema": case.schema, "id": case.id, "normative": case.normative}
```

- `JCS` is the JSON Canonicalization Scheme, [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785).
- The octets hashed are the UTF-8 encoding of the canonical text, with no byte order mark and no trailing newline.
- The digest is written as `sha256:` followed by 64 lowercase hexadecimal digits.
  The prefix names the algorithm, so a later algorithm can be added without ambiguity.

## Scope

The digest covers `schema`, `id` and `normative`, and nothing else.

- `normative` is the meaning of the case: standard, type, input and assertions with their requests, clauses, required capabilities and expectations.
- `id` binds the digest to one identity, so two cases with the same content under different IDs have different digests, and a digest alone names one definition.
- `schema` binds the digest to the representation. Moving a case to another schema version changes its digest even when the meaning is the same; the [lifecycle log](lifecycle.md#revise) records that as a representation revision.
- `informative` is excluded. Titles, tags, related cases, oracle rationale, review status and provenance can be edited without changing the digest.

The scope is an allow list: the definition is built from the three members, not by removing `informative`.
A member added to the case schema later stays outside the digest until the contract admits it.

## Canonical JSON subset

Case content uses only a subset of JSON, so RFC 8785 needs no floating-point formatting:

| Value | Rule |
| --- | --- |
| Number | Integers from -(2^53 - 1) to 2^53 - 1, written in decimal without exponent or fraction. Other numbers are rejected. |
| String | Unicode scalar values. A lone surrogate is rejected. No Unicode normalization is applied: the digest covers the characters exactly as stored. |
| Escapes | `"` and `\` are escaped; U+0008, U+0009, U+000A, U+000C and U+000D use `\b`, `\t`, `\n`, `\f` and `\r`; other characters below U+0020 use `\u00xx` with lowercase hexadecimal. Every other character, including U+007F and non-ASCII, is written as itself. |
| Object | Members sorted by the UTF-16 code units of their names, with no whitespace. |
| Array | Order preserved. |

Byte strings and integers of unbounded size are already strings in the [case representation](test-case-contract.md#canonical-representations), so no case content depends on number precision.
In schema version 1 the only JSON number in normative content is `unused_bits`, which is 0 to 7.

A case file is parsed before it is canonicalized, so its formatting, indentation and member order never affect the digest.
A file with a repeated member name, `NaN` or `Infinity` is rejected, because parsers disagree about what it means.

## Vectors

[`examples/digests.json`](../examples/digests.json) has three parts:

- `canonicalization`: inputs with their canonical text and its SHA-256, including the RFC 8785 member-order and string-escape examples, and inputs that must be rejected.
- `cases`: the digest of every example case.
- `edits`: mutations of an example case and the resulting digest.
  Informative edits and member reordering keep the digest; normative edits, assertion reordering and a different ID or schema change it.

`python scripts/check_lifecycle.py` checks the vectors.
`python scripts/canonical.py <case files>` prints the digest of each case.
