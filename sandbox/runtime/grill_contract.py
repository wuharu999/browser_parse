"""Normalize historical reports and expose incomplete assessments honestly.

Shared by the runner and API. Structural completeness is not proof of feasibility.
"""
from copy import deepcopy
from typing import Any

POSITIVE_CLAIM_STATUSES = {'verified', 'feasible', 'documented'}
CLAIM_STATUSES = POSITIVE_CLAIM_STATUSES | {'gap', 'unsupported', 'unknown', 'candidate', 'inferred'}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _records(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _citations(item: dict) -> list[str]:
    value = item.get('citations') or item.get('citation') or item.get('evidence_citation') or []
    return _strings([value] if isinstance(value, str) else value)


def blocking_issues(state: Any) -> list[str]:
    state = state if isinstance(state, dict) else {}
    issues = [
        str(item.get('id') or item.get('message') or item.get('description'))
        for item in _records(state.get('issues'))
        if item.get('blocking') and _text(item.get('status')) not in {'resolved', 'closed'}
    ]
    checks = state.get('checks')
    if isinstance(checks, dict):
        issues.extend(_strings(checks.get('blocking_issue_ids')))
    return list(dict.fromkeys(issues))


def normalize_report(raw: Any, *, session_id: str = '', task_intent: str = '', state: Any = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('Report must be a JSON object')
    report = deepcopy(raw)
    problems: list[str] = []
    model_generated = report.get('generation') == 'model'
    if not model_generated:
        problems.append('Report requires an evidence-based assessment')

    summary = report.get('scenario_summary')
    if isinstance(summary, dict):
        report['target_robot'] = summary.get('target_robot')
        report['confirmed_parameters'] = summary.get('confirmed_parameters', {})
        report['remaining_open_items'] = summary.get('remaining_open_items', [])
        summary = summary.get('task')
    report['scenario_summary'] = _text(summary) or task_intent
    report['target_robot'] = _text(report.get('target_robot')) or None
    report['schema_version'] = '1.0'
    report['session_id'] = session_id or _text(report.get('session_id'))
    if not _text(summary):
        problems.append('Missing scenario summary')
    if not report['target_robot']:
        problems.append('Target robot selection is unresolved')

    capabilities = report.get('capabilities')
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    claims = []
    for index, item in enumerate(_records(capabilities.get('claims'))):
        claim = {
            'claim_id': _text(item.get('claim_id')) or f'claim_{index + 1}',
            'title': _text(item.get('title')) or _text(item.get('claim')),
            'category': _text(item.get('category')) or _text(item.get('target')),
            'statement': _text(item.get('statement')) or _text(item.get('detail')) or _text(item.get('claim')),
            'status': _text(item.get('status')) or 'unknown',
            'citations': _citations(item),
        }
        if claim['status'] not in CLAIM_STATUSES:
            claim['status'] = 'unknown'
        if not claim['title'] or not claim['statement']:
            problems.append(f"Incomplete capability: {claim['claim_id']}")
        if claim['status'] in POSITIVE_CLAIM_STATUSES:
            if not claim['citations'] or not model_generated:
                claim['status'] = 'unknown'
                problems.append(f"Missing capability evidence: {claim['claim_id']}")
        if claim['status'] in {'unknown', 'gap', 'unsupported'}:
            problems.append(f"Unresolved capability: {claim['claim_id']}")
        claims.append(claim)
    if not claims:
        problems.append('Missing capability assessment')
    report['capabilities'] = {
        **capabilities, 'summary': _text(capabilities.get('summary')), 'claims': claims,
    }

    architecture = report.pop('architecture', None) or report.get('system_architecture')
    architecture = architecture if isinstance(architecture, dict) else {}
    nodes = []
    for item in _records(architecture.get('nodes') or architecture.get('ros_nodes')):
        node = {
            **item,
            'name': _text(item.get('name')) or _text(item.get('node_name')),
            'package': _text(item.get('package')),
            'type': _text(item.get('type')) or _text(item.get('role')),
            'responsibility': _text(item.get('responsibility')),
            'interfaces': _strings(item.get('interfaces')),
            'topics_sub': _strings(item.get('topics_sub') or item.get('subscribes')),
            'topics_pub': _strings(item.get('topics_pub') or item.get('publishes')),
        }
        if not node['name'] or not node['package']:
            problems.append('Incomplete integration node')
        nodes.append(node)
    if not nodes:
        problems.append('Missing integration nodes')
    architecture.pop('ros_nodes', None)
    report['system_architecture'] = {
        **architecture,
        'summary': _text(architecture.get('summary')),
        'middleware': _text(architecture.get('middleware')),
        'recommendations': _strings(architecture.get('recommendations') or architecture.get('integration_points')),
        'nodes': nodes,
    }

    matrix = report.get('risk_matrix')
    matrix = matrix if isinstance(matrix, dict) else {}
    risks = []
    for index, item in enumerate(_records(matrix.get('risks'))):
        risk = {
            **item,
            'risk_id': _text(item.get('risk_id')) or f'risk_{index + 1}',
            'title': _text(item.get('title')) or _text(item.get('description')),
            'mitigation': _text(item.get('mitigation')),
            'citations': _citations(item),
        }
        for key, allowed in [('severity', {'low', 'medium', 'high', 'critical'}),
                             ('likelihood', {'low', 'medium', 'high'})]:
            value = _text(item.get(key))
            risk[key] = value if value in allowed else 'unknown'
            if risk[key] == 'unknown':
                problems.append(f"Missing risk rating: {risk['risk_id']}")
        if not risk['title'] or not risk['mitigation'] or not risk['citations']:
            problems.append(f"Incomplete risk or evidence: {risk['risk_id']}")
        risks.append(risk)
    if not risks:
        problems.append('Missing risk assessment')
    report['risk_matrix'] = {**matrix, 'summary': _text(matrix.get('summary')), 'risks': risks}

    tree = report.get('behavior_tree') or state
    tree = tree if isinstance(tree, dict) else {}
    report['behavior_tree'] = {
        'root_id': _text(tree.get('root_id')),
        'nodes': [
            {
                **node, 'id': _text(node.get('id')), 'type': _text(node.get('type')) or 'Action',
                'name': _text(node.get('name')), 'label': _text(node.get('label')),
                'description': _text(node.get('description')), 'children': _strings(node.get('children')),
            }
            for node in _records(tree.get('nodes'))
        ],
    }
    tree_nodes = report['behavior_tree']['nodes']
    if not tree_nodes:
        problems.append('Missing behavior tree')
    elif report['behavior_tree']['root_id'] not in {node['id'] for node in tree_nodes}:
        problems.append('Missing behavior tree root')
    problems.extend(f'Unresolved blocker: {item}' for item in blocking_issues(state))
    previous_validation = report.get('validation')
    if isinstance(previous_validation, dict):
        problems.extend(_strings(previous_validation.get('issues')))
    if report.get('assessment_status') == 'incomplete' and not problems:
        problems.append('Assessment is incomplete')
    report['assessment_status'] = 'incomplete' if problems else 'review_required'
    report['validation'] = {'issues': list(dict.fromkeys(problems))}
    return report
