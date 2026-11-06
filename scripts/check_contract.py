# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Check the test case and registry contract of schema version 1.

JSON Schema covers structure. This module adds the rules a schema cannot
express: registry consistency, ID allocation, registry transitions that would
reuse or repurpose an identity, input and view compatibility, value and type
conformity, review independence and registered licenses. It also defines the oracle-blind view,
the only part of a case an adapter may ever receive, and which checks a plan or suite selection selects.

Run it to check the committed registry, the schema examples and the example plans.
"""

import argparse
import copy
import functools
from pathlib import Path
import re
import sys

from jsonschema import Draft202012Validator, ValidationError, validators

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.canonical import load_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
REGISTRY = ROOT / "registry" / "registry.json"
EXAMPLES = ROOT / "examples"

EXAMPLE_FAMILY = "example"
# Lifecycle statuses a selection selects when it names none.
DEFAULT_LIFECYCLE = ("active",)
EXPECTATION_KEYS = frozenset({"expect", "expected", "expectation", "result", "one_of", "basis",
                              "outcome", "reason", "oracle", "verdict", "informative"})


CASE_ID = re.compile(load_json(SCHEMAS / "case-1.schema.json")["$defs"]["caseId"]["pattern"])
LICENSE_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9][A-Za-z0-9.+-]*")
LICENSE_OPERATORS = frozenset({"AND", "OR", "WITH"})


def _ecma_pattern(validator, pattern, instance, schema):
    # JSON Schema patterns use ECMA-262 semantics, where $ matches only at the
    # end of the string. Python's $ also matches before a final newline, which
    # would let "0101ff\n" pass as a byte string.
    if not validator.is_type(instance, "string"):
        return
    if re.search(pattern, instance) is None or (pattern.endswith("$") and instance.endswith("\n")):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


Validator = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern})


@functools.lru_cache(maxsize=None)
def validator(kind):
    schema = load_json(SCHEMAS / f"{kind}-1.schema.json")
    Draft202012Validator.check_schema(schema)
    return Validator(schema)


def schema_errors(kind, document):
    """Return the schema errors of a document, sorted by location."""
    return sorted(validator(kind).iter_errors(document), key=lambda error: list(error.absolute_path))


def parse_id(case_id):
    """Split a case ID into its namespace and integer serial, or return None."""
    if not isinstance(case_id, str) or CASE_ID.fullmatch(case_id) is None:
        return None
    namespace, serial = case_id.rsplit(".", 1)
    return namespace, int(serial)


def format_id(namespace, serial):
    return f"{namespace}.{serial:06d}"


def next_id(registry, namespace):
    """Return the next ID to allocate in a namespace and the updated registry."""
    updated = copy.deepcopy(registry)
    entry = _index(updated["namespaces"], "id").get(namespace)
    if entry is None:
        raise ValueError(f"namespace {namespace} is not registered")
    if entry.get("closed"):
        raise ValueError(f"namespace {namespace} is closed")
    if entry["allocated"] >= 999999:
        raise ValueError(f"namespace {namespace} is exhausted")
    entry["allocated"] += 1
    return format_id(namespace, entry["allocated"]), updated


def license_ids(expression):
    """Return the license identifiers of an SPDX expression, or None if it is malformed.

    The corpus accepts identifiers combined with AND, OR and parentheses.
    """
    tokens = LICENSE_TOKEN.findall(expression)
    if "".join(tokens) != "".join(expression.split()) or expression != expression.strip():
        return None
    ids, operand, depth = [], True, 0
    for token in tokens:
        if operand and token == "(":
            depth += 1
        elif operand and token not in LICENSE_OPERATORS and token != ")":
            ids.append(token)
            operand = False
        elif not operand and token == ")" and depth:
            depth -= 1
        elif not operand and token in ("AND", "OR"):
            operand = True
        else:
            return None
    return ids if not operand and not depth else None


def _index(items, key):
    return {item[key]: item for item in items}


def _duplicates(values):
    seen, repeated = set(), []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def check_registry(registry):
    """Return (code, detail) problems inside one registry document."""
    problems = []
    for section, key in (("standards", "id"), ("namespaces", "id"), ("reasons", "code"), ("capabilities", "id"),
                         ("licenses", "id"), ("suites", "id")):
        for value in _duplicates(item[key] for item in registry[section]):
            problems.append(("registry-duplicate", f"{section}: {value}"))
    standards = _index(registry["standards"], "id")
    for namespace in registry["namespaces"]:
        family = namespace["id"].split(".")[0]
        if (family == EXAMPLE_FAMILY) != (namespace["purpose"] == "example"):
            problems.append(("namespace-purpose", namespace["id"]))
        for ref in namespace["standards"]:
            if ref not in standards:
                problems.append(("namespace-standard-unknown", f"{namespace['id']}: {ref}"))
            elif namespace["rules"] not in standards[ref]["rules"]:
                problems.append(("namespace-rules-unsupported", f"{namespace['id']}: {ref}"))
    codes = {reason["code"] for reason in registry["reasons"]}
    for code in sorted(codes):
        if "." in code and code.rsplit(".", 1)[0] not in codes:
            problems.append(("reason-parent-missing", code))
    for suite in registry["suites"]:
        problems += [(f"suite-{code}", f"{suite['id']}: {detail}")
                     for code, detail in check_selection(suite["select"], registry)]
    return problems


def check_selection(selection, registry):
    """Return (code, detail) problems where a selection names unregistered standard editions or rules.

    A misspelt edition or rule set would silently select nothing.
    """
    standards = _index(registry["standards"], "id")
    rules = {rule for standard in registry["standards"] for rule in standard["rules"]}
    problems = [("standard-unknown", ref) for ref in selection.get("standards", ()) if ref not in standards]
    return problems + [("rules-unknown", name) for name in selection.get("rules", ()) if name not in rules]


def selects(selection, case, key, status):
    """Return whether a plan or suite selection selects the check <id>/<key> of a case with a lifecycle status.

    Selection reads only the definition, the tags and the lifecycle status,
    never a result. Invalid cases are never selected.
    """
    normative = case["normative"]
    request = next(assertion["request"] for assertion in normative["assertions"] if assertion["key"] == key)
    named = selection.get("cases")
    if named is not None and case["id"] not in named and f"{case['id']}/{key}" not in named:
        return False
    for name, value in (("standards", normative["standard"]["ref"]), ("rules", normative["standard"]["rules"]),
                        ("operations", request["operation"]), ("strictness", request["strictness"]),
                        ("views", request["view"])):
        if name in selection and value not in selection[name]:
            return False
    if status not in selection.get("lifecycle", DEFAULT_LIFECYCLE):
        return False
    tags = set(case["informative"]["tags"])
    if "tags" in selection and not tags & set(selection["tags"]):
        return False
    return not tags & set(selection.get("exclude_tags", ()))


def check_registry_transition(old, new):
    """Return problems that make a registry change reuse or repurpose an identity.

    old is the registry on the base revision or in the last published release.
    """
    problems = []
    new_standards = _index(new["standards"], "id")
    for ref, standard in _index(old["standards"], "id").items():
        current = new_standards.get(ref)
        if current is None:
            problems.append(("standard-removed", ref))
            continue
        if (current["document"], current["edition"]) != (standard["document"], standard["edition"]):
            problems.append(("standard-changed", ref))
        if not set(standard["rules"]) <= set(current["rules"]):
            problems.append(("standard-rules-removed", ref))
    new_namespaces = _index(new["namespaces"], "id")
    for name, namespace in _index(old["namespaces"], "id").items():
        current = new_namespaces.get(name)
        if current is None:
            problems.append(("namespace-removed", name))
            continue
        if (current["purpose"], current["rules"]) != (namespace["purpose"], namespace["rules"]):
            problems.append(("namespace-changed", name))
        if not set(namespace["standards"]) <= set(current["standards"]):
            problems.append(("namespace-standard-removed", name))
        if current["allocated"] < namespace["allocated"]:
            problems.append(("allocation-decreased", name))
        if namespace.get("closed") and not current.get("closed"):
            problems.append(("namespace-reopened", name))
        if namespace.get("closed") and current["allocated"] != namespace["allocated"]:
            problems.append(("namespace-closed", name))
    for section, key, code in (("reasons", "code", "reason-removed"), ("capabilities", "id", "capability-removed"),
                               ("licenses", "id", "license-removed"), ("suites", "id", "suite-removed")):
        # A section added to the registry schema is absent from older registries.
        current = {item[key] for item in new.get(section, ())}
        for value in sorted({item[key] for item in old.get(section, ())} - current):
            problems.append((code, value))
    return problems


def check_value(value, type_, path="value"):
    """Return (code, detail) problems where a value does not conform to a type."""
    if value["kind"] != type_["kind"]:
        return [("value-type-mismatch", f"{path}: {value['kind']} is not {type_['kind']}")]
    kind = type_["kind"]
    if kind in ("sequence", "set"):
        problems, fields = [], value["fields"]
        names = [field["name"] for field in fields]
        declared = [field["name"] for field in type_["fields"]]
        for name in names:
            if name not in declared:
                problems.append(("value-type-mismatch", f"{path}: unknown field {name}"))
        if problems:
            return problems
        if names != [name for name in declared if name in names]:
            return [("value-type-mismatch", f"{path}: fields out of declared order")]
        present = dict(zip(names, (field["value"] for field in fields)))
        for field in type_["fields"]:
            name = field["name"]
            if name in present:
                problems += check_value(present[name], field["type"], f"{path}.{name}")
                if present[name] == field.get("default"):
                    problems.append(("value-not-canonical", f"{path}: field {name} repeats its default"))
            elif not field.get("optional") and "default" not in field:
                problems.append(("value-type-mismatch", f"{path}: missing field {name}"))
        return problems
    if kind in ("sequence_of", "set_of"):
        problems = []
        for index, item in enumerate(value["items"]):
            problems += check_value(item, type_["item"], f"{path}[{index}]")
        return problems
    if kind == "choice":
        alternatives = _index(type_["alternatives"], "name")
        if value["alternative"] not in alternatives:
            return [("value-type-mismatch", f"{path}: unknown alternative {value['alternative']}")]
        return check_value(value["value"], alternatives[value["alternative"]]["type"], f"{path}.{value['alternative']}")
    return []


def _check_type_defaults(type_, path="type"):
    problems = []
    for field in type_.get("fields", ()):
        if "default" in field:
            problems += check_value(field["default"], field["type"], f"{path}.{field['name']}.default")
        problems += _check_type_defaults(field["type"], f"{path}.{field['name']}")
    if "item" in type_:
        problems += _check_type_defaults(type_["item"], f"{path}[]")
    for alternative in type_.get("alternatives", ()):
        problems += _check_type_defaults(alternative["type"], f"{path}.{alternative['name']}")
    for name in _duplicates(field["name"] for field in type_.get("fields", type_.get("alternatives", ()))):
        problems.append(("type-duplicate-name", f"{path}: {name}"))
    return problems


def _check_license(expression, licenses, label):
    ids = license_ids(expression)
    if ids is None:
        return [("license-expression-invalid", f"{label}: {expression!r}")]
    return [("license-unregistered", f"{label}: {license}") for license in ids if license not in licenses]


def _results(expect):
    return expect["one_of"] if "one_of" in expect else [expect["result"]]


def check_case(case, registry):
    """Return (code, detail) problems of a schema-valid case against a registry."""
    problems = []
    parsed = parse_id(case["id"])
    if parsed is None:
        return [("id-invalid", repr(case["id"]))]
    namespace_name, serial = parsed
    namespaces = _index(registry["namespaces"], "id")
    namespace = namespaces.get(namespace_name)
    normative = case["normative"]
    standard = normative["standard"]
    if namespace is None:
        problems.append(("namespace-unallocated", namespace_name))
    else:
        if serial > namespace["allocated"]:
            problems.append(("serial-unallocated", case["id"]))
        if standard["rules"] != namespace["rules"]:
            problems.append(("rules-mismatch", f"{standard['rules']} in {namespace_name}"))
        if standard["ref"] not in namespace["standards"]:
            problems.append(("standard-not-permitted", f"{standard['ref']} in {namespace_name}"))
    registered = _index(registry["standards"], "id").get(standard["ref"])
    if registered is None:
        problems.append(("standard-unknown", standard["ref"]))
    elif standard["rules"] not in registered["rules"]:
        problems.append(("rules-unknown", f"{standard['rules']} in {standard['ref']}"))

    type_ = normative.get("type")
    input_kind = next(iter(normative["input"]))
    if type_ is not None:
        problems += _check_type_defaults(type_)
        if input_kind == "value":
            problems += check_value(normative["input"]["value"], type_, "input.value")
        elif all(assertion["request"]["view"] != "value" for assertion in normative["assertions"]):
            problems.append(("type-unused", "no value input or value view uses the type"))
    reasons = {reason["code"] for reason in registry["reasons"]}
    capabilities = {capability["id"] for capability in registry["capabilities"]}
    for key in _duplicates(assertion["key"] for assertion in normative["assertions"]):
        problems.append(("duplicate-assertion-key", key))
    for assertion in normative["assertions"]:
        label = assertion["key"]
        request = assertion["request"]
        operation, view = request["operation"], request["view"]
        if operation == "decode" and input_kind != "octets":
            problems.append(("input-operation-mismatch", f"{label}: decode needs octets, not {input_kind}"))
        if operation == "encode" and input_kind != view:
            problems.append(("input-operation-mismatch", f"{label}: encode of {input_kind} with the {view} view"))
        if operation == "round-trip" and input_kind not in ("octets", view):
            problems.append(("input-operation-mismatch", f"{label}: round-trip of {input_kind} with the {view} view"))
        for capability in assertion.get("requires", ()):
            if capability not in capabilities:
                problems.append(("capability-unknown", f"{label}: {capability}"))
        for result in _results(assertion["expect"]):
            if result["outcome"] == "reject":
                if result["reason"] not in reasons:
                    problems.append(("reason-unknown", f"{label}: {result['reason']}"))
                continue
            if "tree" in result and view != "tree":
                problems.append(("result-view-mismatch", f"{label}: tree result in the {view} view"))
            if "value" in result:
                if view != "value":
                    problems.append(("result-view-mismatch", f"{label}: value result in the {view} view"))
                elif type_ is not None:
                    problems += check_value(result["value"], type_, f"{label}.value")
            preserved = result.get("preserves")
            if preserved in ("tree", "value") and preserved != view:
                problems.append(("result-view-mismatch", f"{label}: preserves {preserved} in the {view} view"))
            if preserved == "octets" and input_kind != "octets":
                problems.append(("result-view-mismatch", f"{label}: preserves octets of a {input_kind} input"))

    informative = case["informative"]
    if case["id"] in informative.get("related", ()):
        problems.append(("related-self", case["id"]))
    for related in informative.get("related", ()):
        if parse_id(related)[0].split(".")[0] == EXAMPLE_FAMILY and namespace_name.split(".")[0] != EXAMPLE_FAMILY:
            problems.append(("related-example", related))
    review = informative["oracle"]["review"]
    authors = set(informative["provenance"]["authors"])
    reviewers = set(review["reviewers"])
    if review["status"] == "self-reviewed" and not reviewers <= authors:
        problems.append(("review-status-mismatch", "self-reviewed by a non-author; use peer-reviewed"))
    if review["status"] == "peer-reviewed" and reviewers <= authors:
        problems.append(("review-not-independent", "peer review needs a reviewer who is not an author"))
    licenses = {license["id"] for license in registry["licenses"]}
    problems += _check_license(informative["provenance"]["license"], licenses, "provenance")
    for index, vector in enumerate(informative["oracle"].get("vectors", ())):
        problems += _check_license(vector["license"], licenses, f"vectors/{index}")
    return problems


def check_cases(cases, registry):
    """Return problems across a set of cases: duplicate IDs and dangling relations."""
    problems = [("duplicate-id", case_id) for case_id in _duplicates(case["id"] for case in cases)]
    known = {case["id"] for case in cases}
    for case in cases:
        for related in case["informative"].get("related", ()):
            if related not in known:
                problems.append(("related-unknown", f"{case['id']}: {related}"))
    return problems


def oracle_blind_view(case, key):
    """Return everything an adapter may receive to execute one assertion.

    The view is built from an allow list, never by removing fields, so a field
    added to the case later stays hidden until the contract admits it. It
    leaves out the case ID too, which would let an adapter look up the
    expectation; the harness relates answers to checks by request ID.
    """
    normative = case["normative"]
    assertion = next(item for item in normative["assertions"] if item["key"] == key)
    view = {"standard": copy.deepcopy(normative["standard"]),
            "request": copy.deepcopy(assertion["request"]),
            "input": copy.deepcopy(normative["input"])}
    if "type" in normative:
        view["type"] = copy.deepcopy(normative["type"])
    return view


def expectation_keys(document):
    """Return the expectation-bearing keys found anywhere in a document."""
    found = set()
    if isinstance(document, dict):
        found |= EXPECTATION_KEYS & set(document)
        for value in document.values():
            found |= expectation_keys(value)
    elif isinstance(document, list):
        for item in document:
            found |= expectation_keys(item)
    return found


def apply_mutation(document, mutation):
    """Apply one replace, add or remove operation addressed by a JSON pointer."""
    result = copy.deepcopy(document)
    parts = [part.replace("~1", "/").replace("~0", "~") for part in mutation["path"].split("/")[1:]]
    parent = result
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    last = parts[-1]
    if isinstance(parent, list):
        last = len(parent) if last == "-" else int(last)
    if mutation["op"] == "remove":
        del parent[last]
    elif mutation["op"] == "add" and isinstance(parent, list):
        parent.insert(last, copy.deepcopy(mutation["value"]))
    elif mutation["op"] in ("add", "replace"):
        if mutation["op"] == "replace" and (last not in parent if isinstance(parent, dict) else last >= len(parent)):
            raise KeyError(mutation["path"])
        parent[last] = copy.deepcopy(mutation["value"])
    else:
        raise ValueError(f"unsupported operation {mutation['op']}")
    return result


def load_examples():
    """Return the valid example cases and the invalid mutations with their documents."""
    valid = {path.name: load_json(path) for path in sorted((EXAMPLES / "valid").glob("*.json"))}
    invalid = []
    for entry in load_json(EXAMPLES / "invalid.json")["mutations"]:
        document = valid[entry["base"]]
        for mutation in entry["mutations"]:
            document = apply_mutation(document, mutation)
        invalid.append((entry, document))
    return valid, invalid


def violations(case, registry):
    """Return the schema error paths, or the semantic codes, of one case."""
    errors = schema_errors("case", case)
    if errors:
        return "schema", sorted({"/" + "/".join(str(part) for part in error.absolute_path) for error in errors})
    return "semantic", sorted({code for code, _ in check_case(case, registry)})


def run():
    failures = []
    registry = load_json(REGISTRY)
    failures += [f"registry schema: {error.message}" for error in schema_errors("registry", registry)]
    if failures:
        return failures
    failures += [f"registry: {code}: {detail}" for code, detail in check_registry(registry)]
    valid, invalid = load_examples()
    for name, case in valid.items():
        failures += [f"{name}: {error.message}" for error in schema_errors("case", case)]
        if not schema_errors("case", case):
            failures += [f"{name}: {code}: {detail}" for code, detail in check_case(case, registry)]
    failures += [f"examples: {code}: {detail}" for code, detail in check_cases(list(valid.values()), registry)]
    for entry, document in invalid:
        stage, found = violations(document, registry)
        expected = entry["violation"]
        if {stage: found} != {key: sorted(value) for key, value in expected.items()}:
            failures.append(f"invalid example {entry['name']}: expected {expected}, found {stage} {found}")
    for name, plan in load_plans().items():
        errors = schema_errors("plan", plan)
        failures += [f"plans/{name}: {error.message}" for error in errors]
        if not errors:
            problems = check_selection(plan["select"], registry)
            failures += [f"plans/{name}: {code}: {detail}" for code, detail in problems]
            suite = plan["select"].get("suite")
            if suite is not None and suite not in _index(registry["suites"], "id"):
                failures.append(f"plans/{name}: suite-unknown: {suite}")
    return failures


def load_plans():
    """Return the example execution plans by file name."""
    return {path.name: load_json(path) for path in sorted((EXAMPLES / "plans").glob("*.json"))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    failures = run()
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        valid, invalid = load_examples()
        print(f"contract ok: {len(valid)} valid and {len(invalid)} invalid examples, {len(load_plans())} example plans")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
