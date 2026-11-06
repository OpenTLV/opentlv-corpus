# opentlv-corpus

OpenTLV Corpus — An open, reusable collection of TLV test vectors, conformance cases, and reference data for validating, benchmarking, and comparing TLV implementations across languages and libraries.

Every case states what a standard requires for one input, with an independent oracle: cited clauses, a published test vector or the corpus operation contract.
Agreement between implementations is never treated as proof.
The cases are language-neutral data, usable by OpenTLV Core, its bindings, OpenTLV TestLab and unrelated TLV implementations without Python and without any third-party reference library.

## Status

The corpus is in its foundation phase.
The test case and registry schemas, the ID and namespace rules and the oracle contract are defined; conformance cases, the validator, release artifacts, the adapter protocol and the runner follow.
Schema version 1 is provisional until the first stable corpus release.

## Layout

| Path | Contents |
| --- | --- |
| [`schemas/`](schemas) | JSON Schemas (draft 2020-12) of the test case and registry documents |
| [`registry/registry.json`](registry/registry.json) | Standard editions, ID namespaces and their allocations, rejection reasons and capabilities |
| [`examples/valid/`](examples/valid) | Example cases covering every construct of the schema |
| [`examples/invalid.json`](examples/invalid.json) | Mutations of the valid examples, each breaking exactly one rule |
| [`scripts/check_contract.py`](scripts/check_contract.py) | Checks the rules the schemas cannot express and builds oracle-blind adapter views |
| [`docs/`](docs) | Specifications |

## Specifications

- [Test case and oracle contract](docs/test-case-contract.md): normative and informative fields, inputs, operations, strictness, expectations, canonical representations, rejection reasons, oracle evidence and oracle-blind requests.
- [Identity](docs/identity.md): ID grammar, namespace and serial allocation, no reuse, and the change policy before and after the first stable release.
- [Schema compatibility](docs/compatibility.md): schema versions and how they relate to data, tooling and protocol versions.

## Checks

Python 3.11 or later:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check_contract.py
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
