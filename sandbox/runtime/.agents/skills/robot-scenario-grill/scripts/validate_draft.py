#!/usr/bin/env python3
"""Read-only structural checks for robot-scenario-grill JSON; no capability verdict."""

import argparse
import json
import math
from pathlib import Path


def validate(data, previous=None):
    errors = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    def records(value, label):
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            errors.append(f"{label}: expected an array of objects")
            return []
        return value

    def strings(value, label, nonempty=False):
        valid = isinstance(value, list) and all(isinstance(x, str) and x for x in value)
        check(valid and (bool(value) or not nonempty), f"{label}: expected {'nonempty ' if nonempty else ''}string array")
        return value if valid else []

    def index(items, label):
        result = {}
        for item in items:
            key = item.get("id")
            if not isinstance(key, str) or not key:
                errors.append(f"{label}: missing string id")
                continue
            check(key not in result, f"{label}: duplicate id {key}")
            result[key] = item
        return result

    def refs(value, allowed, label):
        values = strings(value, label)
        for key in values:
            check(key in allowed, f"{label}: unresolved reference {key}")
        return values

    if not isinstance(data, dict):
        return ["Top level must be an object"]
    required = {"schema_version", "scenario_id", "revision", "summary", "sources", "fields", "root_id", "nodes", "alternatives", "issues", "questions", "changes", "checks"}
    check(set(data) == required, f"Top-level keys: missing {sorted(required - set(data))}, extra {sorted(set(data) - required)}")
    check(data.get("schema_version") == "1.0", "schema_version must be 1.0")
    check(type(data.get("revision")) is int and data["revision"] >= 1, "revision must be a positive integer")
    for key in ("scenario_id", "summary"):
        check(isinstance(data.get(key), str) and bool(data[key]), f"{key}: nonempty string required")

    sources = index(records(data.get("sources"), "sources"), "sources")
    fields = index(records(data.get("fields"), "fields"), "fields")
    nodes = index(records(data.get("nodes"), "nodes"), "nodes")
    alternatives = index(records(data.get("alternatives"), "alternatives"), "alternatives")
    issues = index(records(data.get("issues"), "issues"), "issues")
    questions = index(records(data.get("questions"), "questions"), "questions")
    ids = list(sources) + list(fields) + list(nodes) + list(alternatives) + list(issues) + list(questions)
    check(len(ids) == len(set(ids)), "IDs must be unique across collections")
    targets = set(fields) | set(nodes) | set(alternatives)

    for key, source in sources.items():
        check(source.get("kind") in {"user", "document", "robot_evidence"}, f"{key}: invalid source kind")
        for name in ("locator", "excerpt"):
            check(isinstance(source.get(name), str) and bool(source[name]), f"{key}: {name} required")

    blocking_targets = set()
    customer_targets = set()
    for key, issue in issues.items():
        check(issue.get("kind") in {"missing", "conflict", "choice", "evidence", "structural"}, f"{key}: invalid issue kind")
        check(issue.get("owner") in {"customer", "engineering"}, f"{key}: invalid owner")
        check(type(issue.get("blocking")) is bool, f"{key}: blocking must be boolean")
        linked = refs(issue.get("target_ids"), targets, key + ".target_ids")
        check(bool(linked), f"{key}: issue must target a field, node or alternative")
        for name in ("description", "resolve_by"):
            check(isinstance(issue.get(name), str) and bool(issue[name]), f"{key}: {name} required")
        if issue.get("blocking") is True:
            blocking_targets.update(linked)
        if issue.get("owner") == "customer":
            customer_targets.update(linked)

    domains = {"boundary", "fact_state", "requirement_graph", "constraints", "disturbances", "acceptance_value"}
    field_keys = {"id", "domain", "role", "label", "value", "unit", "status", "source_ids", "rationale", "confirmed", "blocking"}
    for key, field in fields.items():
        check(field_keys <= set(field), f"{key}: missing field keys {sorted(field_keys - set(field))}")
        check(field.get("domain") in domains, f"{key}: invalid domain")
        check(field.get("role") in {"requirement", "initial_fact", "runtime"}, f"{key}: invalid role")
        status = field.get("status")
        check(status in {"known", "unknown", "candidate", "conflict", "not_applicable"}, f"{key}: invalid field status")
        linked = refs(field.get("source_ids"), sources, key + ".source_ids")
        for name in ("confirmed", "blocking"):
            check(type(field.get(name)) is bool, f"{key}: {name} must be boolean")
        if status == "known":
            check(bool(linked) and field.get("value") is not None, f"{key}: known value requires source and non-null value")
        if status == "unknown":
            check(field.get("value") is None, f"{key}: unknown value must be null")
        if status == "candidate":
            check(bool(field.get("rationale")) and field.get("confirmed") is False, f"{key}: candidate needs rationale and cannot be confirmed")
        if status == "conflict":
            check(isinstance(field.get("value"), list) and len(field["value"]) >= 2 and bool(linked), f"{key}: conflict requires alternatives and sources")
        if field.get("role") != "runtime" and field.get("blocking") and status in {"unknown", "candidate", "conflict"}:
            check(key in blocking_targets, f"{key}: unresolved blocking field needs blocking issue")

    types = {"Sequence", "Fallback", "Condition", "Action", "SubTree", "Retry", "Timeout", "Loop"}
    node_keys = {"id", "type", "label", "children", "subtree_id", "alternative_group", "read_fields", "write_fields", "constraint_fields", "location_fields", "preconditions", "success_criteria", "failure_cases", "parameters", "basis", "required_capabilities"}
    edges = {key: [] for key in nodes}
    parents = {key: 0 for key in nodes}
    writers = set()
    for key, node in nodes.items():
        check(node_keys <= set(node), f"{key}: missing node keys {sorted(node_keys - set(node))}")
        kind = node.get("type")
        check(kind in types, f"{key}: invalid node type")
        children = refs(node.get("children"), nodes, key + ".children")
        expected = (1 if kind in {"Retry", "Timeout", "Loop"} else 0)
        check(len(children) >= 1 if kind in {"Sequence", "Fallback"} else len(children) == expected, f"{key}: invalid child count for {kind}")
        edges[key].extend(child for child in children if child in nodes)
        for name in ("read_fields", "write_fields", "constraint_fields", "location_fields"):
            linked = refs(node.get(name), fields, key + "." + name)
            if name == "write_fields":
                check(not linked or kind == "Action", f"{key}: only Action can declare writes")
                writers.update(linked)
        for name in ("preconditions", "success_criteria", "failure_cases", "required_capabilities"):
            strings(node.get(name), key + "." + name)
        if kind in {"Action", "Condition"}:
            check(bool(node.get("success_criteria")) and bool(node.get("failure_cases")), f"{key}: leaf needs success and failure semantics")
        basis = node.get("basis", {})
        if not isinstance(basis, dict):
            errors.append(f"{key}: basis must be an object")
            basis = {}
        check(basis.get("status") in {"known", "candidate"}, f"{key}: invalid basis status")
        linked = refs(basis.get("source_ids"), sources, key + ".basis.source_ids")
        check(bool(linked) if basis.get("status") == "known" else bool(basis.get("rationale")), f"{key}: basis needs source or candidate rationale")
        subtree = node.get("subtree_id")
        group = node.get("alternative_group")
        if kind == "SubTree":
            if subtree is None:
                check(group in alternatives and (key in blocking_targets or group in blocking_targets), f"{key}: unbound SubTree requires alternative group and blocking issue")
            else:
                check(subtree in nodes, f"{key}: undefined subtree {subtree}")
                if subtree in nodes:
                    edges[key].append(subtree)
        else:
            check(subtree is None and group is None, f"{key}: subtree references only allowed on SubTree")

        params = node.get("parameters", {})
        if not isinstance(params, dict):
            errors.append(f"{key}: parameters must be an object")
            params = {}
        bound_names = []
        if kind == "Timeout":
            bound_names = ["max_duration_field"]
        elif kind == "Retry":
            bound_names = [name for name in ("max_attempts_field", "max_duration_field") if name in params]
            check(bool(bound_names), f"{key}: Retry requires a bound field")
            for name in ("retryable_reasons", "reentry_checks"):
                strings(params.get(name), key + ".parameters." + name, nonempty=True)
        elif kind == "Loop":
            bound_names = [name for name in ("max_iterations_field", "max_duration_field") if name in params]
            check(bool(bound_names), f"{key}: Loop requires a bound field")
            completion = fields.get(params.get("completion_field"), {})
            check(completion.get("role") == "runtime", f"{key}: Loop completion_field must reference runtime data")
            check(completion.get("value") is None or type(completion.get("value")) is bool, f"{key}: completion value must be boolean or null")
        for name in bound_names:
            field_id = params.get(name)
            check(field_id in fields, f"{key}: missing bound field for {name}")
            field = fields.get(field_id, {})
            value = field.get("value")
            if value is not None:
                number_ok = type(value) in {int, float} if name == "max_duration_field" else type(value) is int
                check(number_ok and math.isfinite(value) and value > 0, f"{key}: {name} must be positive and finite")
            else:
                check(field_id in blocking_targets or key in blocking_targets, f"{key}: unknown bound requires blocking issue")
            check(field.get("unit") == ("s" if name == "max_duration_field" else "count"), f"{key}: invalid bound unit for {name}")

    option_roots = set()
    for key, group in alternatives.items():
        ref = nodes.get(group.get("requirement_node_id"), {})
        check(ref.get("type") == "SubTree" and ref.get("alternative_group") == key, f"{key}: must bind one SubTree placeholder")
        options = records(group.get("options"), key + ".options")
        check(len(options) >= 2, f"{key}: alternatives need at least two options")
        indexed = index(options, key + ".options")
        for option in options:
            root = option.get("root_id")
            check(root in nodes, f"{key}: undefined candidate root {root}")
            if root in nodes:
                option_roots.add(root)
            refs(option.get("precondition_fields"), fields, key + ".precondition_fields")
            strings(option.get("implementation_hints"), key + ".implementation_hints")
        selected = group.get("selected_option_id")
        check(selected is None or selected in indexed, f"{key}: invalid selected option")
        expected = indexed.get(selected, {}).get("root_id")
        check(ref.get("subtree_id") == expected, f"{key}: binding must equal selected option, or null")

    for children in edges.values():
        for child in children:
            parents[child] += 1
    for key, count in parents.items():
        check(count <= 1, f"{key}: node instance has multiple parents")
    root = data.get("root_id")
    check(root in nodes and parents.get(root) == 0, "root_id must reference an unparented node")
    visited, active = set(), set()

    def walk(key):
        if key in active:
            errors.append(f"{key}: cycle in behavior tree")
            return
        if key in visited:
            return
        active.add(key)
        for child in edges[key]:
            walk(child)
        active.remove(key)
        visited.add(key)

    if root in nodes:
        walk(root)
    main_nodes = set(visited)
    for key, group in alternatives.items():
        for option in group.get("options", []) if isinstance(group.get("options"), list) else []:
            if isinstance(option, dict) and option.get("id") != group.get("selected_option_id"):
                check(option.get("root_id") not in main_nodes, f"{key}: unselected candidate is wired into the main tree")
    for key in option_roots:
        walk(key)
    check(visited == set(nodes), "All nodes must belong to main tree or declared candidate subtrees")
    for key, node in nodes.items():
        for field_id in node.get("read_fields", []) if isinstance(node.get("read_fields"), list) else []:
            field = fields.get(field_id, {})
            if field.get("role") == "runtime":
                check(field_id in writers or field_id in blocking_targets or key in blocking_targets, f"{key}: runtime input {field_id} needs a producer or blocking issue")
        if node.get("type") == "Loop" and isinstance(node.get("parameters"), dict):
            field_id = node["parameters"].get("completion_field")
            check(field_id in writers or key in blocking_targets, f"{key}: completion condition needs an observation producer or blocking issue")

    check(len(questions) <= 3, "At most three questions per turn")
    for key, question in questions.items():
        linked = refs(question.get("target_ids"), targets, key + ".target_ids")
        check(bool(linked) and set(linked) <= customer_targets, f"{key}: targets must have customer-owned issues")
        options = records(question.get("options"), key + ".options")
        check(len(options) == 3, f"{key}: exactly three suggested options required")
        for option in options:
            check(bool(option.get("label")) and bool(option.get("interpretation")), f"{key}: option label and interpretation required")
        check(question.get("free_text") is True and question.get("allow_unknown") is True, f"{key}: free text and unknown response must be supported")
        check(bool(question.get("text")) and bool(question.get("why")), f"{key}: question and rationale required")

    changes, checks = data.get("changes", {}), data.get("checks", {})
    if not isinstance(changes, dict) or not isinstance(checks, dict):
        return errors + ["changes and checks must be objects"]
    for name in ("added_ids", "updated_ids"):
        refs(changes.get(name), targets, "changes." + name)
    removed = strings(changes.get("removed_ids"), "changes.removed_ids")
    check(not set(removed) & targets, "Removed IDs cannot still exist")
    refs(changes.get("affected_node_ids"), nodes, "changes.affected_node_ids")
    check(checks.get("validation") in {"not_run", "passed"}, "Invalid validation status")
    check(type(checks.get("ready_for_readback")) is bool, "ready_for_readback must be boolean")
    strings(checks.get("notes"), "checks.notes")
    listed = refs(checks.get("blocking_issue_ids"), issues, "checks.blocking_issue_ids")
    check(set(listed) == {key for key, issue in issues.items() if issue.get("blocking") is True}, "blocking_issue_ids must include all and only blocking issues")

    if previous is not None:
        check(data.get("scenario_id") == previous.get("scenario_id"), "Revision must preserve scenario_id")
        check(type(previous.get("revision")) is int and data.get("revision") == previous["revision"] + 1, "revision must increase by one")
        old = {item["id"]: item for name in ("fields", "nodes", "alternatives") for item in previous.get(name, [])}
        current = {**fields, **nodes, **alternatives}
        check(set(changes.get("added_ids", [])) == set(current) - set(old), "added_ids does not match previous snapshot")
        check(set(removed) == set(old) - set(current), "removed_ids does not match previous snapshot")
        updated = {key for key in set(current) & set(old) if current[key] != old[key]}
        check(set(changes.get("updated_ids", [])) == updated, "updated_ids does not match changed objects")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("draft", type=Path)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    try:
        current = json.loads(args.draft.read_text(encoding="utf-8"))
        old = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous else None
        errors = validate(current, old)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        errors = [f"Malformed input or read error: {error}"]
    print(json.dumps({"structure_valid": not errors, "errors": errors, "scope": "Structure only; not factual, customer-confirmation or robot-feasibility validation."}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if errors else 0)

