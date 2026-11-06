# Schema compatibility policy

Corpus documents declare their schema version in a `schema` member, for example `urn:opentlv:corpus:schema:case:1`.
The schema file `schemas/<kind>-<version>.schema.json` has the same URN as its `$id`.

| Schema | URN | File |
| --- | --- | --- |
| Test case | `urn:opentlv:corpus:schema:case:1` | [`schemas/case-1.schema.json`](../schemas/case-1.schema.json) |
| Registry | `urn:opentlv:corpus:schema:registry:1` | [`schemas/registry-1.schema.json`](../schemas/registry-1.schema.json) |
| Lifecycle log | `urn:opentlv:corpus:schema:lifecycle:1` | [`schemas/lifecycle-1.schema.json`](../schemas/lifecycle-1.schema.json) |
| Release manifest | `urn:opentlv:corpus:schema:manifest:1` | [`schemas/manifest-1.schema.json`](../schemas/manifest-1.schema.json) |

## Independent versions

Four versions move independently, and each consumer records the ones it uses:

- **Schema versions**, one per document kind, are the integers in the URNs above.
- **The data release** is the corpus version of the published cases, registry and schemas, pinned by release and SHA-256 digest.
- **Tooling** is the version of the Python validator, resolver, runner and reporter.
- **The adapter protocol** has its own version, so adapters do not change when only data or tooling does.

A [data release](release.md) ships the exact schema files its documents use, so a consumer can validate a release without the Python tooling and without fetching anything else.
Its manifest records the data release version, the source revision and the tooling version, and each document records its schema version.

## Rules

- Every schema is closed: unknown members are rejected.
  A document that validates against a schema version therefore contains nothing a consumer of that version would ignore.
- Once a schema version is part of a stable corpus release, its file never changes.
  Any change that makes a valid document invalid, makes an invalid document valid, or changes the meaning of a field creates a new version with a new URN and file.
  The previous version stays in the repository for as long as a supported release uses it.
- During 0.x, version 1 is provisional.
  It may change in place in a reviewed pull request that records the change in the CHANGELOG, and every published 0.x release keeps the schema files it was published with.
- Moving documents to a new schema version is a representation change, not a semantic one: the migration must preserve the normative meaning of every case.
  The canonical definition digest covers the normative content in its schema version, so it changes with the representation.
  The migration appends a `representation` revision for every case to the [lifecycle log](lifecycle.md#revise), which records the digests before and after it.
- Reason codes, capabilities, licenses, namespaces and standard editions are registry data, so adding one is a data change, not a schema change.
  Removing or repurposing one is forbidden at any version, as [Identity](identity.md) specifies.
