# Data release

A data release publishes the conformance cases as data only: the definitions, the registry, the lifecycle log and its projection, the schema files and the license.
It contains no tooling and needs none.
`tar`, `sha256sum` and any JSON parser read it, and any JSON Schema draft 2020-12 validator checks it against the schemas it ships.
[`scripts/release.py`](../scripts/release.py) builds and verifies it; the Python tooling is not part of it and has its own version.

## Artifacts

Release `<version>` consists of three files:

| File | Contents |
| --- | --- |
| `opentlv-corpus-data-<version>.tar` | The data, as an uncompressed POSIX ustar archive |
| `opentlv-corpus-data-<version>.manifest.json` | A copy of the manifest inside the archive, readable before downloading the archive |
| `SHA256SUMS` | The SHA-256 of the archive and of the manifest copy, in `sha256sum` format |

The archive is uncompressed because the output of a compressor depends on its implementation and version, while the bytes of an uncompressed archive depend only on its contents.

## Layout

```text
opentlv-corpus-data-<version>/
  LICENSE
  SHA256SUMS                    SHA-256 of every other file
  manifest.json                 identity and contents of the release
  lifecycle-projection.json     status, digest and replacement of every case
  registry/registry.json
  registry/lifecycle.json
  schemas/<kind>-<version>.schema.json
  cases/<namespace>/<id>.json
```

- Cases, registry, lifecycle log, schema files and license are byte-for-byte copies of the source files.
- `lifecycle-projection.json` is the [projection](lifecycle.md#projection) of `registry/lifecycle.json` over the released cases.
  A consumer selects active cases and recognizes tainted results from it without replaying the log.
- `schemas/` holds every schema of the source revision, including the manifest schema, so a consumer validates every document without fetching anything.
- Example cases and release records are never part of a release.

## Manifest

`manifest.json` follows [`schemas/manifest-1.schema.json`](../schemas/manifest-1.schema.json).

| Member | Contents |
| --- | --- |
| `schema` | `urn:opentlv:corpus:schema:manifest:1` |
| `corpus` | `opentlv-corpus` |
| `release` | Data release version: Semantic Versioning 2.0.0 without build metadata |
| `phase` | Phase of the conformance lifecycle log. A `0.x` log is released as `0.y.z`, a `stable` log as `1.0.0` or later |
| `source` | Repository URL and full Git commit the release was built from |
| `tools` | Name and version of the tooling that built the release |
| `schemas` | URN and path of every shipped schema |
| `licenses` | SPDX identifiers of every license the case data uses, from provenance and published vectors |
| `cases` | ID, path and [definition digest](digest.md) of every case, ordered by ID |
| `files` | Path, size and SHA-256 of every file except `manifest.json` and `SHA256SUMS`, ordered by path |

The manifest records each of the [independent versions](compatibility.md#independent-versions) a data release involves: the data release version, the schema version of each document kind and the tooling version.
The adapter protocol is not part of a data release.

## Reproducibility

The same source files, release version, source revision and tooling version give the same archive bytes on every platform:

- the members are regular files below one directory named after the release, ordered by path; directories are implied;
- every member has the modification time 0, mode 0644, user and group 0 and empty owner names;
- source files are copied without change, and the [validator](validation.md#files-and-encoding) requires UTF-8 with LF line endings, which `.gitattributes` keeps in every checkout;
- generated files are written deterministically: JSON indented by two spaces with members in a fixed order, and checksum lines ordered by path.

`python scripts/release.py build --version <version>` writes the artifacts to `build/release`.
It refuses to build when:

- the [validator](validation.md) reports any diagnostic;
- the version is not a Semantic Versioning 2.0.0 version without build metadata, or does not match the phase;
- `releases/<version>` already exists;
- the release inputs, `LICENSE`, `schemas/`, `registry/` and `cases/`, differ from the checked-out commit.
  `--revision` names the source commit explicitly instead, for example when the tree is an export of that commit.

## Verification

Without any corpus tooling:

```sh
sha256sum --check SHA256SUMS
tar -xf opentlv-corpus-data-<version>.tar
cd opentlv-corpus-data-<version>
sha256sum --check SHA256SUMS
```

The first check covers the archive and the manifest copy, the second every file in the archive, including `manifest.json`.
The case files are listed in `manifest.json` and validate against the shipped schemas.

`python scripts/release.py verify <archive> --sums SHA256SUMS` checks everything a release promises and reports [diagnostics](validation.md#diagnostics) with paths relative to the release directory:

| Code | Rule |
| --- | --- |
| `checksum-missing`, `checksum-mismatch` | The published `SHA256SUMS` lists the archive, and every listed file present beside it has the listed SHA-256. |
| `checksums-invalid` | A `SHA256SUMS` file is ASCII lines of `<sha256>  <name>`. |
| `archive-invalid` | The archive is an uncompressed tar archive with at least one file. |
| `archive-member-path`, `archive-member-type`, `archive-member-duplicate` | Members are regular files with safe relative paths below one directory, each once. |
| `archive-member-metadata`, `archive-order` | Members have the normalized metadata above and are ordered by path. |
| `manifest-release-mismatch`, `release-version-phase` | The directory is named after the release, and the version matches the phase. |
| `unexpected-file` | The release holds only the files of the layout above. |
| `manifest-file-unlisted`, `manifest-file-missing`, `manifest-file-mismatch`, `manifest-order` | `files` lists every file exactly once, in order, with its size and SHA-256. |
| `checksums-unlisted`, `checksums-missing`, `checksums-mismatch` | The inner `SHA256SUMS` lists every other file with its SHA-256. |
| `manifest-case-unlisted`, `manifest-case-missing`, `manifest-case-mismatch` | `cases` lists every case with its path and the digest recomputed from its definition. |
| `manifest-schemas-mismatch`, `manifest-licenses-mismatch`, `manifest-phase-mismatch` | `schemas`, `licenses` and `phase` match the shipped files. |
| `projection-mismatch` | `lifecycle-projection.json` equals the replay of the shipped log over the shipped cases. |

The extracted release is then checked by the [validator](validation.md), against its own schemas.

## Published releases

A published release is recorded in the repository by committing its manifest copy and `SHA256SUMS`:

```text
releases/<version>/manifest.json   the opentlv-corpus-data-<version>.manifest.json artifact
releases/<version>/SHA256SUMS      the SHA256SUMS artifact
```

- A record is immutable: [`scripts/check_history.py`](../scripts/check_history.py) reports a changed record as `release-record-changed` and a deleted one as `release-record-removed`.
- The [validator](validation.md#release-records) checks that the two files describe the same release.
- A consumer pins a release by its version and the SHA-256 of its archive from the record, and never follows a moving name such as `latest`.

A published release is reproduced from its source revision with that revision's own tooling:

```sh
git worktree add --detach build/reproduce <revision>
python build/reproduce/scripts/release.py build --version <version> --output build/reproduced \
  --check releases/<version>/SHA256SUMS
```

`--check` fails unless the rebuilt artifacts have exactly the recorded checksums.
CI reproduces every published release this way.
