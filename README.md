# opentlv-corpus

OpenTLV Corpus — An open, reusable collection of TLV test vectors, conformance cases, and reference data for validating, benchmarking, and comparing TLV implementations across languages and libraries.

Every case states what a standard requires for one input, with an independent oracle: cited clauses, a published test vector or the corpus operation contract.
Agreement between implementations is never treated as proof.
The cases are language-neutral data, usable by OpenTLV Core, its bindings, OpenTLV TestLab and unrelated TLV implementations without Python and without any third-party reference library.

## Status

The corpus is in its foundation phase.
The test case, registry and lifecycle schemas, the ID and namespace rules, the oracle contract and the canonical definition digest are defined; conformance cases, the validator, release artifacts, the adapter protocol and the runner follow.
Schema version 1 is provisional until the first stable corpus release.

## Layout

| Path | Contents |
| --- | --- |
| [`schemas/`](schemas) | JSON Schemas (draft 2020-12) of the test case, registry and lifecycle log documents |
| [`registry/registry.json`](registry/registry.json) | Standard editions, ID namespaces and their allocations, rejection reasons and capabilities |
| [`registry/lifecycle.json`](registry/lifecycle.json) | Append-only lifecycle log of the conformance cases |
| [`examples/valid/`](examples/valid) | Example cases covering every construct of the schema |
| [`examples/invalid.json`](examples/invalid.json) | Mutations of the valid examples, each breaking exactly one rule |
| [`examples/lifecycle.json`](examples/lifecycle.json) | Lifecycle log of the examples; `lifecycle-projection.json` and `lifecycle-invalid.json` hold its projection and invalid mutations |
| [`examples/digests.json`](examples/digests.json) | Canonicalization and definition digest vectors |
| [`scripts/check_contract.py`](scripts/check_contract.py) | Checks the rules the schemas cannot express and builds oracle-blind adapter views |
| [`scripts/canonical.py`](scripts/canonical.py) | RFC 8785 canonical JSON and definition digests |
| [`scripts/check_lifecycle.py`](scripts/check_lifecycle.py) | Replays and checks the lifecycle logs and checks the digest vectors |
| [`scripts/check_history.py`](scripts/check_history.py) | Checks a change against its base revision for rewritten history |
| [`docs/`](docs) | Specifications |

## Specifications

- [Test case and oracle contract](docs/test-case-contract.md): normative and informative fields, inputs, operations, strictness, expectations, canonical representations, rejection reasons, oracle evidence and oracle-blind requests.
- [Identity](docs/identity.md): ID grammar, namespace and serial allocation, no reuse, and the change policy before and after the first stable release.
- [Lifecycle](docs/lifecycle.md): append-only lifecycle events, statuses, replacements, the 0.x and stable phases, the projection and tainted results.
- [Canonical definition digest](docs/digest.md): RFC 8785 canonicalization, SHA-256, digest scope, byte encoding and limits.
- [Schema compatibility](docs/compatibility.md): schema versions and how they relate to data, tooling and protocol versions.

## Checks

Python 3.11 or later:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check_contract.py
python scripts/check_lifecycle.py
python scripts/check_history.py --base origin/main
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
