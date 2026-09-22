import json

import pytest

from backend.app import GrillAnswerItem
from backend.grill_store import GrillStore
from sandbox import run_grill
from sandbox.runtime.grill_contract import normalize_report


@pytest.mark.parametrize('answer,field,value', [
    ({'free_text_answer': 'Clean the inner glass'}, 'free_text', 'Clean the inner glass'),
    ({'is_unknown': True}, 'unknown', True),
])
def test_browser_answer_fields_survive_api_validation(answer, field, value):
    assert GrillAnswerItem.model_validate({'question_id': 'q1', **answer}).model_dump()[field] == value


def interview(tmp_path):
    store = GrillStore(':memory:', tmp_path / 'uploads')
    session, token = store.create_session('Inspect a cabinet')
    task = store.worker_claim_grill('worker', {'cpu_milli': 2000, 'memory_mb': 4096})
    question = {'id': 'q1', 'text': 'Which surface?', 'options': [{'label': x, 'interpretation': x} for x in ['Glass', 'Metal', 'Plastic']], 'free_text': True, 'allow_unknown': True}
    state = {'summary': 'Inspect a cabinet', 'nodes': [], 'issues': [], 'checks': {'validation': 'passed'}}
    output = {'scenario_state': state, 'questions': [question], 'ready_for_readback': False}
    store.worker_finish_grill(task['id'], 'worker', 'completed', output)
    return store, session['id'], token, task, output


@pytest.mark.parametrize('answers', [[], [{'question_id': 'wrong', 'unknown': True}], [{'question_id': 'q1'}], [{'question_id': 'q1', 'selected_option': 'Imaginary'}], [{'question_id': 'q1', 'unknown': True}] * 2])
def test_invalid_answers_cannot_advance_the_interview(tmp_path, answers):
    store, sid, token, _, _ = interview(tmp_path)
    with pytest.raises(ValueError):
        store.submit_answers(sid, token, answers)
    assert store.get_session(sid)['question_count'] == 0


def test_cannot_confirm_with_unanswered_questions(tmp_path):
    store, sid, token, _, _ = interview(tmp_path)
    with pytest.raises(ValueError):
        store.confirm_scenario(sid, token)


def test_duplicate_finish_does_not_duplicate_turns(tmp_path):
    store, sid, _, task, output = interview(tmp_path)
    store.worker_finish_grill(task['id'], 'worker', 'completed', output)
    assert len(store.get_turns(sid)) == 1


def test_wrong_worker_cannot_finish_a_task(tmp_path):
    store = GrillStore(':memory:', tmp_path / 'uploads')
    session, _ = store.create_session('Inspect cabinet')
    task = store.worker_claim_grill('owner', {'cpu_milli': 2000, 'memory_mb': 4096})
    with pytest.raises(PermissionError):
        store.worker_finish_grill(task['id'], 'other', 'completed', {'questions': []})
    assert store.get_session(session['id'])['status'] != 'completed'


def test_fallback_report_never_fabricates_verified_capabilities():
    report = run_grill.generate_fallback_report('session', 'Choose a robot', {})
    assert report.get('assessment_status') == 'incomplete'
    assert all(claim['status'] not in {'verified', 'feasible'} for claim in report['capabilities']['claims'])
    assert 'wiki/hardware/platform_specs.md' not in json.dumps(report)


def test_production_runner_does_not_silently_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(run_grill, 'WORKSPACE', tmp_path)
    monkeypatch.delenv('ROBOT_GRILL_USE_FALLBACK', raising=False)
    monkeypatch.setattr(run_grill.shutil, 'which', lambda _: None)
    assert run_grill.run_grill({'action': 'report', 'task_intent': 'Choose a robot'}) == 1
    assert json.loads((tmp_path / 'result.json').read_text())['status'] == 'failed'


def test_followup_history_returns_latest_page_and_stable_cursor(tmp_path):
    store = GrillStore(':memory:', tmp_path / 'uploads')
    session, _ = store.create_session('Inspect cabinet')
    for i in range(5):
        question = store.add_followup_question(session['id'], f'Question {i}')
        store.update_followup_question(question['id'], 'Recorded answer', 'completed')
    latest = store.list_followup_questions(session['id'], limit=2)
    assert [q['question'] for q in latest['items']] == ['Question 3', 'Question 4']
    older = store.list_followup_questions(session['id'], limit=2, before=latest['next_before'])
    assert [q['question'] for q in older['items']] == ['Question 1', 'Question 2']


def test_confirmation_note_reaches_report_worker(tmp_path):
    store, sid, token, _, _ = interview(tmp_path)
    store.submit_answers(sid, token, [{'question_id': 'q1', 'free_text': 'Glass'}])
    task = store.worker_claim_grill('worker', {'cpu_milli': 2000, 'memory_mb': 4096})
    store.worker_finish_grill(task['id'], 'worker', 'completed', {'scenario_state': {}, 'questions': [], 'ready_for_readback': True})
    store.confirm_scenario(sid, token, 'Include inner glass; no robot chosen yet')
    report_task = store.worker_claim_grill('worker', {'cpu_milli': 2000, 'memory_mb': 4096})
    assert report_task['confirmation_note'] == 'Include inner glass; no robot chosen yet'


def test_followup_retries_are_idempotent_and_restart_preserves_partial_answer(tmp_path):
    store, sid, _, _, _ = interview(tmp_path)
    item, created = store.begin_followup_question(sid, 'Why?', 'req1')
    assert created
    same, created = store.begin_followup_question(sid, 'Why?', 'req1')
    assert not created and same['id'] == item['id']
    with pytest.raises(ValueError):
        store.begin_followup_question(sid, 'Another question?', 'req2')
    store.update_followup_question(item['id'], 'Partial saved answer')
    store.interrupt_questions()
    history = store.list_followup_questions(sid)
    assert history['active'] is None
    assert history['items'][0]['status'] == 'interrupted'
    assert history['items'][0]['answer'] == 'Partial saved answer'


def test_report_normalizes_legacy_fields_and_preserves_open_blockers():
    report = normalize_report({
        'scenario_summary': {'task': 'Clean glass', 'target_robot': 'Walker_C1_EDU'},
        'architecture': {'ros_nodes': [{'name': 'inspect', 'package': 'vision', 'interfaces': ['/image [Topic]']}]},
        'capabilities': {'claims': [{'claim': 'Reach glass', 'detail': 'Unproven reach', 'status': 'verified'}]},
        'risk_matrix': {'risks': [{'description': 'Collision', 'mitigation': 'Stop', 'evidence_citation': 'inputs/site.md'}]},
    }, state={'issues': [{'id': 'reach', 'blocking': True}]})
    assert report['scenario_summary'] == 'Clean glass'
    assert report['system_architecture']['nodes'][0]['name'] == 'inspect'
    assert report['risk_matrix']['risks'][0]['title'] == 'Collision'
    assert report['risk_matrix']['risks'][0]['citations'] == ['inputs/site.md']
    assert report['capabilities']['claims'][0]['status'] == 'unknown'
    assert report['assessment_status'] == 'incomplete'
    assert 'Unresolved blocker: reach' in report['validation']['issues']


def test_report_runner_uses_model_and_rejects_missing_citation(tmp_path, monkeypatch):
    monkeypatch.setattr(run_grill, 'WORKSPACE', tmp_path)
    monkeypatch.delenv('ROBOT_GRILL_USE_FALLBACK', raising=False)
    monkeypatch.setenv('CODEX_PROVIDER_ENV_KEY', 'TEST_GRILL_KEY')
    monkeypatch.setenv('TEST_GRILL_KEY', 'fake-test-key')
    monkeypatch.setattr(run_grill.shutil, 'which', lambda _: '/fake/codex')
    calls = []

    def model_process(command, **kwargs):
        calls.append(kwargs['input'])
        output_path = command[command.index('--output-last-message') + 1]
        from pathlib import Path
        Path(output_path).write_text(json.dumps({
            'scenario_summary': 'Customer-specific assessment',
            'capabilities': {'claims': [{'title': 'Payload', 'statement': 'Payload unknown', 'status': 'verified', 'citations': ['wiki/missing.md#L1']}]},
        }))
        from subprocess import CompletedProcess
        return CompletedProcess(command, 0)

    monkeypatch.setattr(run_grill.subprocess, 'run', model_process)
    assert run_grill.run_grill({'action': 'report', 'task_intent': 'Clean inner glass', 'confirmation_note': 'No ladder'}) == 0
    report = json.loads((tmp_path / 'result.json').read_text())['report']
    assert len(calls) == 1 and 'No ladder' in calls[0] and 'Clean inner glass' in calls[0]
    assert report['scenario_summary'] == 'Customer-specific assessment'
    assert report['assessment_status'] == 'incomplete'
    assert report['capabilities']['claims'][0]['status'] == 'unknown'
    assert 'Citation not found: wiki/missing.md#L1' in report['validation']['issues']


def test_legacy_imports_use_the_staged_runner_implementation():
    from sandbox import run_codex
    from sandbox.runtime import run_codex as staged_codex, run_grill as staged_grill
    assert run_codex is staged_codex
    assert run_grill is staged_grill


def test_complete_assessment_remains_review_required_and_normalization_is_idempotent():
    report = normalize_report({
        'generation': 'model',
        'scenario_summary': 'Inspect glass',
        'target_robot': 'Walker_C1_EDU',
        'capabilities': {'claims': [{'title': 'Camera', 'statement': 'Documented camera interface', 'status': 'documented', 'citations': ['wiki/camera.md#L1']}]},
        'system_architecture': {'nodes': [{'name': 'inspect', 'package': 'vision'}]},
        'risk_matrix': {'risks': [{'title': 'Collision', 'mitigation': 'Measure clearance', 'severity': 'high', 'likelihood': 'medium', 'citations': ['inputs/site.md']}]},
        'behavior_tree': {'root_id': 'observe', 'nodes': [{'id': 'observe', 'type': 'Action', 'label': 'Observe glass'}]},
    })
    assert report['assessment_status'] == 'review_required'
    assert report['validation']['issues'] == []
    assert normalize_report(report) == report


@pytest.mark.parametrize('value', [None, 'unexpected', [], 42])
def test_malformed_optional_report_sections_do_not_crash_the_reader(value):
    report = normalize_report({key: value for key in ['capabilities', 'system_architecture', 'risk_matrix', 'behavior_tree']})
    assert report['assessment_status'] == 'incomplete'
    assert report['capabilities']['claims'] == []
    assert report['system_architecture']['nodes'] == []
