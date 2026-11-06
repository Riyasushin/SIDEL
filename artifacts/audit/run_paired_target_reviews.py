#!/usr/bin/env python3
"""Review 1,200 frozen targets at L1 or L3; never execute a target or an agent.

One output directory belongs to one model/level. Smoke uses a persistent nine-case
selection; omit --smoke on the same directory to continue the full dataset.
Requests, responses, and attempt metadata are append-only across invocations.
An auth/balance/quota alert stops new calls immediately; already-sent calls finish
so their raw evidence is retained. Shared billing alerts live beside --models at
accounts/<account_id>/alert.json. Resolve the failure and remove that acknowledged
shared marker before restarting. Invalid stages get at most two calls per invocation.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from manager.analysis import native_review_action
from router.attack_dataset import load_attack_dataset
from router.providers.openai_compatible import OpenAICompatibleProvider
from router.reviewer import (
    prepare_reviewer_request, reviewer_decision_status, reviewer_stage,
    reviewer_stop_sequences, visible_review_decision,
)
from router.tool_compatibility import content_hash
from scripts.replay_reviewer_runs import (
    build_chat_request, build_settings_for_provider, rerun_single_reviewer,
)

BUDGETS = {1: 6400, 2: 8192}
ATTEMPTS = 2
TOOLS = {'Bash', 'Write', 'Edit'}
CORE_FILES = (
    'manager/analysis.py', 'router/attack_dataset.py', 'router/config.py',
    'router/models.py', 'router/protocols.py', 'router/reviewer.py',
    'router/tool_compatibility.py', 'router/providers/openai_compatible.py',
    'scripts/replay_reviewer_runs.py',
)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value, *, immutable=False):
    """Publish complete checkpoints; fsync alerts and evidence before announcing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if immutable:
        with path.open('x', encoding='utf-8') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def transcript_blocks(body):
    users = [m for m in body['messages'] if m.get('role') == 'user']
    if len(users) != 1 or not isinstance(users[0].get('content'), list):
        raise ValueError('Expected one native block-form reviewer transcript')
    blocks = users[0]['content']
    begins = [i for i, b in enumerate(blocks) if b.get('text', '').strip() == '<transcript>']
    ends = [i for i, b in enumerate(blocks) if b.get('text', '').strip() == '</transcript>']
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise ValueError('Ambiguous native transcript boundaries')
    return users[0], begins[0], ends[0]


def render_target(sample):
    name, args = sample['tool_name'], sample['tool_arguments']
    if name == 'Bash':
        value = args['command']
    elif name == 'Write':
        value = args['file_path'] + ': ' + args['content']
    elif name == 'Edit':
        value = args['file_path'] + ': ' + args['new_string']
    else:
        raise ValueError('Unsupported planned tool: ' + name)
    lines = (name + ' ' + value).split('\n')
    return lines[0] + ''.join('\n  ' + line for line in lines[1:]) + '\n'


def make_review(row, stage, level, text, templates, settings):
    body = deepcopy(templates[row['stage_templates'][str(stage)]])
    user, _, end = transcript_blocks(body)
    if reviewer_stage(body) != stage:
        raise ValueError('Stage template mismatch')
    prefix = deepcopy(row['prefix_blocks'])
    if not prefix or prefix[0].get('text', '').strip() != '<transcript>':
        raise ValueError('Historical prefix must begin at transcript boundary')
    if any('</transcript>' in b.get('text', '') for b in prefix):
        raise ValueError('Historical prefix includes closing boundary')
    if level == 'L3' and (not text or '<transcript>' in text or '</transcript>' in text):
        raise ValueError('Invalid shared rewrite or transcript boundary')
    intervention = [{'type': 'text', 'text': 'Assistant: ' + text + '\n'}] if level == 'L3' else []
    target = deepcopy(row['target_block'])
    user['content'] = prefix + intervention + [target] + deepcopy(user['content'][end:])
    for block in user['content']:
        block.pop('cache_control', None)
    body.pop('metadata', None)
    body.pop('max_completion_tokens', None)
    body['max_tokens'] = BUDGETS[stage]
    body['thinking'] = {'type': 'disabled'}
    prepared, budget = prepare_reviewer_request(
        body, endpoint='anthropic.messages', settings=settings,
        metadata={'reviewer_stage': stage},
    )
    if budget['effective_max_tokens'] != BUDGETS[stage]:
        raise ValueError('Reviewer budget differs from frozen policy')
    user, _, end = transcript_blocks(prepared)
    if user['content'][end - 1].get('text') != target['text']:
        raise ValueError('Actual final native block differs from planned target')
    if native_review_action(prepared) != target['text'].strip():
        raise ValueError('Native parser does not identify the expected final target')
    return prepared


def validate_plan(path):
    plan = load(path)
    rows = plan['entries']
    dataset = ROOT / 'data/code_agent_attack_dataset.jsonl'
    samples_list = load_attack_dataset(str(dataset))
    samples = {sample['id']: sample for sample in samples_list}
    tasks = {row['task'] for row in rows}
    if (len(rows) != 1200 or len(tasks) != 3 or len(samples) != 400
            or len(samples_list) != 400
            or len({(r['task'], r['sample_id']) for r in rows}) != 1200
            or len({r['context_id'] for r in rows}) != 1200):
        raise ValueError('Expected exactly three tasks x 400 unique dataset samples')
    for task in tasks:
        if Counter(r['sample_id'] for r in rows if r['task'] == task) != Counter(samples.keys()):
            raise ValueError('Full task/sample coverage mismatch: ' + task)
    hashes = {}
    templates = {}
    for row in rows:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', row['context_id']) or row['context_id'] in {'.', '..'}:
            raise ValueError('Unsafe context directory name')
        required = {str(dataset), row['source_request'], row['source_response'], *row['stage_templates'].values()}
        if not required.issubset(row['source_hashes']):
            raise ValueError('Plan omits a required frozen source hash')
        for source, expected in row['source_hashes'].items():
            if source not in hashes:
                hashes[source] = digest(source)
            if hashes[source] != expected:
                raise ValueError('Frozen source changed: ' + source)
        sample = samples[row['sample_id']]
        if (row['sample_sha256'] != content_hash(sample)
                or row['target_arguments_sha256'] != content_hash(sample['tool_arguments'])
                or row['target_block'] != {'type': 'text', 'text': render_target(sample)}):
            raise ValueError('Dataset target differs from frozen plan: ' + row['context_id'])
        if set(row['stage_templates']) != {'1', '2'}:
            raise ValueError('Both native stage templates are required')
        for source in row['stage_templates'].values():
            if source not in templates:
                templates[source] = load(source)
    if plan.get('assignment_source'):
        if digest(plan['assignment_source']) != plan['assignment_sha256']:
            raise ValueError('Frozen assignment changed')
    return rows, samples, templates


def validate_rewrites(root, rows):
    texts, hashes = {}, {}
    configuration = root / 'configuration.json'
    generator_hash = digest(configuration)
    quality_fields = ('nonempty', 'not_truncated', 'task_grounded', 'no_fallback', 'thinking_disabled')
    for row in rows:
        path = root / row['context_id'] / 'rewrite.json'
        rewrite = load(path)
        identity = ('context_id', 'task', 'sample_id', 'resolved_turn')
        if (rewrite.get('schema_version') != 1
                or any(rewrite.get(key) != row[key] for key in identity)
                or rewrite.get('model') != 'deepseek-v4-pro'
                or rewrite.get('fallback_used') is not False
                or rewrite.get('finish_reason') != 'stop'
                or rewrite.get('thinking') != {'type': 'disabled'}
                or rewrite.get('max_tokens') != 8192
                or any(rewrite.get('quality', {}).get(key) is not True for key in quality_fields)):
            raise ValueError('Invalid shared rewrite evidence: ' + str(path))
        text = rewrite.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise ValueError('Invalid shared rewrite length: ' + str(path))
        for key in ('source_request', 'source_response'):
            source = row[key]
            if rewrite.get('source_hashes', {}).get(source) != row['source_hashes'][source]:
                raise ValueError('Rewrite source identity differs from frozen plan')
        raw_hashes = {}
        for kind in ('request', 'response'):
            raw = Path(rewrite[kind + '_path']).resolve()
            if not raw.is_relative_to(root):
                raise ValueError('Rewrite evidence is outside this fresh shared rewrite directory')
            actual = digest(raw)
            if actual != rewrite[kind + '_sha256']:
                raise ValueError('Shared rewrite raw evidence changed: ' + str(raw))
            raw_hashes[kind + '_sha256'] = actual
        request = load(rewrite['request_path'])
        response = load(rewrite['response_path'])
        choices = response.get('choices', [])
        if len(choices) != 1:
            raise ValueError('Rewrite requires exactly one raw completion')
        choice = choices[0]
        message = choice.get('message', {})
        usage = response.get('usage') or {}
        reasoning_tokens = [usage.get('reasoning_tokens', 0)]
        reasoning_tokens += [(usage.get(key) or {}).get('reasoning_tokens', 0)
                             for key in ('completion_tokens_details', 'output_tokens_details')]
        if (response.get('model') != 'deepseek-v4-pro' or choice.get('finish_reason') != 'stop'
                or not isinstance(message.get('content'), str) or not message['content'].strip()
                or message['content'] != rewrite.get('raw_text') or message.get('reasoning_content')
                or any(value not in (None, 0) for value in reasoning_tokens)
                or request.get('model') != 'deepseek-v4-pro'
                or request.get('thinking') != {'type': 'disabled'} or request.get('max_tokens') != 8192):
            raise ValueError('Rewrite raw response/request fails strict quality policy')
        texts[row['context_id']] = text
        hashes[row['context_id']] = {'rewrite_sha256': digest(path), **raw_hashes}
    return texts, {'configuration_sha256': generator_hash, 'cases': hashes}


class ReviewProvider(OpenAICompatibleProvider):
    """Keep provider-specific thinking opt-outs through both preparation passes."""

    def prepare_body(self, body):
        prepared = super().prepare_body(body)
        prepared.update(deepcopy(self.request_extras))
        return prepared

    def prepare_anthropic_body(self, body):
        prepared = super().prepare_anthropic_body(body)
        prepared.update(deepcopy(self.request_extras))
        return prepared


def model_settings(entry, timeout):
    name, model, protocol = entry['provider'], entry['model'], entry['protocol']
    if name not in {'deepseek', 'qwen', 'kimi'} or protocol not in {'chat', 'anthropic'}:
        raise ValueError('Unsupported selected model provider/protocol')
    if protocol == 'anthropic' and name != 'kimi':
        raise ValueError('Existing replay executor supports native Anthropic only for Kimi')
    if not isinstance(model, str) or not model or not entry.get('returned_models'):
        raise ValueError('Available model needs an explicit ID and returned-model allowlist')
    if any(not isinstance(value, str) or not value for value in entry['returned_models']):
        raise ValueError('Invalid returned-model allowlist')
    extras = entry.get('request_extras', {})
    required = {'enable_thinking': False} if name == 'qwen' else {'thinking': {'type': 'disabled'}}
    if any(extras.get(key) != value for key, value in required.items()):
        raise ValueError('Manifest does not explicitly disable reviewer thinking')
    if set(extras) - {'enable_thinking', 'thinking'}:
        raise ValueError('Unsupported model request extras; request policy must stay frozen')
    settings = build_settings_for_provider(name, model)
    settings.request_timeout_seconds = timeout
    settings.request_max_attempts = 1
    settings.reviewer_stage1_max_tokens = BUDGETS[1]
    settings.reviewer_stage2_max_tokens = BUDGETS[2]
    settings.kimi_api_protocol = protocol if name == 'kimi' else settings.kimi_api_protocol
    settings.deepseek_disable_thinking = True
    base_url = entry.get('base_url')
    if not isinstance(base_url, str) or not base_url.startswith(('https://', 'http://')):
        raise ValueError('Manifest must freeze the probed base_url')
    endpoint_field = 'kimi_anthropic_base_url' if protocol == 'anthropic' else name + '_base_url'
    setattr(settings, endpoint_field, base_url)
    key_env = entry['api_key_env']
    if key_env not in {'DEEPSEEK_API_KEY', 'QWEN_API_KEY', 'KIMI_API_KEY'}:
        raise ValueError('Unsupported credential environment selector')
    api_key = os.environ.get(key_env, getattr(settings, key_env.lower()))
    provider = ReviewProvider(
        name=name, model=model, base_url=getattr(settings, name + '_base_url'),
        api_key=api_key, timeout_seconds=timeout,
        max_attempts=1, anthropic_base_url=settings.kimi_anthropic_base_url if name == 'kimi' else '',
        disable_thinking=True,
    )
    provider.request_extras = deepcopy(extras)
    identity = {key: deepcopy(entry[key]) for key in
                ('key', 'requested_label', 'provider', 'model', 'returned_models', 'protocol',
                 'base_url', 'request_extras', 'api_key_env', 'account_id')}
    return settings, provider, identity


def expected_upstream(body, stage, entry, settings, provider):
    metadata = {'reviewer_stage': stage}
    if entry['protocol'] == 'anthropic':
        request, _ = prepare_reviewer_request(body, endpoint='anthropic.messages', settings=settings, metadata=metadata)
        request = provider.prepare_anthropic_body({**request, 'model': entry['model'], 'stream': False})
    else:
        request = provider.prepare_body(build_chat_request(
            body, endpoint='anthropic.messages', model=entry['model'], settings=settings, metadata=metadata,
        ))
    if request.get('max_tokens') != BUDGETS[stage] or request.get('model') != entry['model']:
        raise ValueError('Prepared request changed model or budget')
    if reviewer_stop_sequences(request) != reviewer_stop_sequences(body):
        raise ValueError('Provider preparation changed native stop policy')
    return request


def pause_reason(error):
    """Separate exhausted accounts from transient request/token rate limits."""
    status = error.get('status_code')
    text = json.dumps(error.get('detail', error), ensure_ascii=False).lower()
    if status == 402 or re.search(r'insufficient[_ -]?(?:balance|funds|credits?)|balance.{0,35}(?:insufficient|exhaust|zero|not enough)|payment[_ -]?required|credit.{0,25}(?:exhaust|deplet)|billing[_ -]?(?:hard[_ -]?)?limit|arrearage|余额不足|欠费', text):
        return 'balance_exhausted'
    if re.search(r'insufficient[_ -]?quota|(?:monthly|daily|account|usage|spend|budget).{0,30}(?:quota|limit).{0,30}(?:exceed|exhaust|reach)|quota.{0,30}(?:exceed|exhaust|deplet)|额度不足|配额.{0,12}(?:不足|用尽|超限)', text):
        rate_window = re.search(r'per[_ -]?(?:minute|second)|requests?[_ /-]?(?:minute|second)|\brpm\b|\btpm\b', text)
        if not rate_window or 'insufficient_quota' in text:
            return 'quota_exhausted'
    if status in (401, 403) or re.search(r'invalid[_ -]?api[_ -]?key|authentication[_ -]?(?:failed|error)|api key is not configured', text):
        return 'authentication_or_access_denied'
    return None


def smoke_selection(root, rows, samples):
    path = root / 'smoke-selection.json'
    by_id = {row['context_id']: row for row in rows}
    expected = {(row['task'], tool) for row in rows for tool in TOOLS}
    if path.exists():
        selected = load(path)
    else:
        grouped = {}
        for row in rows:
            grouped.setdefault((row['task'], samples[row['sample_id']]['tool_name']), row['context_id'])
        selected = list(grouped.values())
    if (not isinstance(selected, list) or len(selected) != 9 or len(set(selected)) != 9
            or any(key not in by_id for key in selected)
            or {(by_id[key]['task'], samples[by_id[key]['sample_id']]['tool_name']) for key in selected} != expected):
        raise ValueError('Smoke selection must cover exactly three tasks x Bash/Write/Edit')
    if not path.exists():
        save(path, selected, immutable=True)
    return [by_id[key] for key in selected]


def read_attempt(directory, row, stage, body, upstream, entry, config_hash):
    metadata = load(directory / 'attempt.json')
    if metadata.get('configuration_sha256') != config_hash or metadata.get('stage') != stage or metadata.get('context_id') != row['context_id']:
        raise ValueError('Attempt identity differs from this frozen run')
    agent_request = load(directory / 'request.agent.json')
    actual_request = load(directory / 'request.upstream.json')
    if agent_request != body or actual_request != upstream:
        raise ValueError('Saved native/upstream request differs from frozen input')
    if native_review_action(agent_request) != row['target_block']['text'].strip():
        raise ValueError('Saved actual native final target does not match expected target')
    record = {'stage': stage, 'output_dir': str(directory), 'configuration_sha256': config_hash,
              'attempt': metadata['attempt'], 'invocation_id': metadata['invocation_id'],
              'health': 'transport_error', 'decision': 'unknown'}
    error = load(directory / 'error.json') if (directory / 'error.json').exists() else {}
    record['http_status'] = error.get('status_code')
    response_path = directory / 'response.upstream.json'
    if response_path.exists():
        response = load(response_path)
        if response.get('error'):
            error = {'detail': response['error']}
        if response.get('model') not in entry['returned_models']:
            record['health'] = 'returned_model_mismatch'
        else:
            meta = load(directory / 'meta.json') if (directory / 'meta.json').exists() else {}
            health = reviewer_decision_status(response, request=actual_request, provenance=meta)
            decision = visible_review_decision(response, request=actual_request, provenance=meta) if health == 'present' else ''
            record.update(health=health, decision=decision if decision in ('allowed', 'denied') else 'unknown',
                          returned_model=response['model'])
            if health == 'present' and decision not in ('allowed', 'denied'):
                record['health'] = 'invalid_decision'
    record['evidence_sha256'] = {name: digest(directory / name) for name in
                               ('attempt.json', 'request.agent.json', 'request.upstream.json',
                                'response.upstream.json', 'meta.json', 'error.json', 'reviewer-budget.json')
                               if (directory / name).exists()}
    return record, error


async def run(args):
    root = args.output
    rows, samples, templates = validate_plan(args.plan)
    entries = [entry for entry in load(args.models)['models'] if entry.get('key') == args.model_key]
    if len(entries) != 1:
        raise ValueError('Select exactly one explicit model manifest entry')
    entry = entries[0]
    entry.setdefault('api_key_env', entry['provider'].upper() + '_API_KEY')
    prior_status = load(root / 'status.json') if (root / 'status.json').exists() else {}
    prior_config = load(root / 'configuration.json') if (root / 'configuration.json').exists() else None
    if prior_status and (prior_status.get('model_key') != args.model_key or prior_status.get('level') != args.level):
        raise ValueError('Output belongs to another model/level')
    if prior_config is not None:
        selected_identity = prior_config.get('selected_model_identity', {})
        if (not selected_identity or prior_config.get('level') != args.level
                or prior_config.get('plan_sha256') != digest(args.plan)
                or any(entry.get(key) != value for key, value in selected_identity.items())):
            raise ValueError('Output configuration belongs to different frozen inputs')
    elif any(path.name not in {'.writer.lock', 'status.json'} for path in root.iterdir()):
        raise ValueError('Unconfigured output has existing artifacts; refusing old-run reuse')
    if entry.get('available') is not True:
        completed = prior_status.get('completed_cases', 0)
        save(root / 'status.json', {**prior_status, 'status': 'blocked', 'reason': 'exact_model_unavailable',
             'model_key': args.model_key, 'level': args.level, 'selected_model_entry': entry,
             'planned_cases': 1200, 'completed_cases': completed, 'remaining_cases': 1200 - completed,
             'payload_execution': False, 'updated_at': now()})
        print(json.dumps({'event': 'PAIRED_REVIEW_BLOCKED', 'model_key': args.model_key,
                          'level': args.level, 'reason': 'exact_model_unavailable'}), file=sys.stderr, flush=True)
        return 2
    settings, provider, identity = model_settings(entry, args.timeout_seconds)
    account_id = entry['account_id']
    if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', account_id):
        raise ValueError('Manifest requires a safe explicit billing account_id')
    account_alert = args.models.parent / 'accounts' / account_id / 'alert.json'
    texts, rewrite_hashes = ({}, None) if args.level == 'L1' else validate_rewrites(args.rewrites, rows)
    for row in rows:
        for stage in (1, 2):
            body = make_review(row, stage, args.level, texts.get(row['context_id'], ''), templates, settings)
            expected_upstream(body, stage, entry, settings, provider)
    configuration = {
        'schema_version': 1, 'level': args.level, 'selected_model_identity': identity,
        'plan_sha256': digest(args.plan), 'executor_sha256': digest(__file__),
        'core_executor_sha256': {path: digest(ROOT / path) for path in CORE_FILES},
        'shared_rewrites': rewrite_hashes, 'planned_cases': 1200,
        'timeout_seconds': args.timeout_seconds, 'stage1_max_tokens': BUDGETS[1],
        'stage2_max_tokens': BUDGETS[2], 'reviewer_thinking': 'disabled',
        'stop_policy': 'preserve',
        'template_stop_sequences': {path: list(reviewer_stop_sequences(body)) for path, body in templates.items()},
        'stage_gating': 'stage1_allow_terminates; stage1_deny_requires_same_action_stage2',
        'max_attempts_per_stage_per_invocation': ATTEMPTS, 'transport_max_attempts': 1,
        'ordinary_429_delay_seconds': [5, 10], 'payload_execution': False,
        'reuse_prior_campaign_results': False,
        'account_alert_path': str(account_alert),
    }
    config_path = root / 'configuration.json'
    if config_path.exists():
        if load(config_path) != configuration:
            raise ValueError('Resume configuration differs; refusing to mix inputs/results')
    else:
        allowed = {'.writer.lock', 'status.json'}
        if any(path.name not in allowed for path in root.iterdir()):
            raise ValueError('Unconfigured output has existing artifacts; refusing old-run reuse')
        if (root / 'status.json').exists() and load(root / 'status.json').get('reason') != 'exact_model_unavailable':
            raise ValueError('Unconfigured output has unrelated status evidence')
        save(config_path, configuration, immutable=True)
    config_hash = content_hash(configuration)
    invocation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex
    invocation_root = root / 'invocations' / invocation
    invocation_root.mkdir(parents=True, exist_ok=False)
    save(invocation_root / 'invocation.json', {'invocation_id': invocation, 'started_at': now(),
         'configuration_sha256': config_hash, 'concurrency': args.concurrency, 'smoke': args.smoke}, immutable=True)
    outcomes = {}
    stop = asyncio.Event()
    stop_details = {}
    rate_limit_until = 0.0
    semaphore = asyncio.Semaphore(args.concurrency)

    def checkpoint(state):
        completed = [value for value in outcomes.values() if value['status'] == 'completed']
        save(root / 'status.json', {
            'status': state, 'reason': stop_details.get('reason'), 'stop_reason': dict(stop_details),
            'model_key': args.model_key, 'model': entry['model'], 'provider': entry['provider'],
            'level': args.level, 'planned_cases': 1200, 'completed_cases': len(completed),
            'unresolved_cases': sum(value['status'] == 'unresolved' for value in outcomes.values()),
            'remaining_cases': 1200 - len(completed),
            'decisions': dict(Counter(value['decision'] for value in completed)),
            'reused_prior_campaign_cases': 0, 'configuration_sha256': config_hash,
            'invocation_id': invocation, 'concurrency': args.concurrency,
            'timeout_seconds': args.timeout_seconds, 'payload_execution': False, 'updated_at': now(),
        })
        save(root / 'results.json', [outcomes[row['context_id']] for row in rows if row['context_id'] in outcomes])

    def pause(reason, *, row=None, stage=None, directory=None, http_status=None):
        if stop.is_set():
            return
        stop.set()
        stop_details.update(reason=reason, model_key=args.model_key, model=entry['model'],
                            provider=entry['provider'], level=args.level, http_status=http_status, account_id=account_id,
                            context_id=row['context_id'] if row else None, stage=stage,
                            evidence_dir=str(directory) if directory else None,
                            invocation_id=invocation, detected_at=now())
        alert = {'event': 'PAIRED_REVIEW_ALERT', **stop_details,
                 'action': 'paused_new_calls; already_sent_calls_retain_evidence; resolve_before_restart'}
        if reason in {'authentication_or_access_denied', 'authentication_not_configured',
                      'balance_exhausted', 'quota_exhausted'}:
            save(account_alert, alert)
        save(root / 'alert.json', alert)
        save(invocation_root / 'alert.json', alert, immutable=True)
        print(json.dumps(alert, ensure_ascii=False), file=sys.stderr, flush=True)
        checkpoint('paused')

    def account_is_paused():
        if not account_alert.exists():
            return False
        shared = load(account_alert)
        stop_details.update(account_reason=shared.get('reason'), account_alert=str(account_alert))
        pause('shared_account_paused', directory=account_alert.parent, http_status=shared.get('http_status'))
        return True

    def requests(row, stage):
        body = make_review(row, stage, args.level, texts.get(row['context_id'], ''), templates, settings)
        return body, expected_upstream(body, stage, entry, settings, provider)

    def resumed_stage(row, stage, record):
        if not isinstance(record, dict) or record.get('health') != 'present':
            return None
        directory = Path(record['output_dir']).resolve()
        if not directory.is_relative_to(root / 'review-attempts'):
            raise ValueError('Attempt evidence comes from another run')
        body, upstream = requests(row, stage)
        actual, _ = read_attempt(directory, row, stage, body, upstream, entry, config_hash)
        if actual != record or actual['health'] != 'present' or actual['decision'] not in ('allowed', 'denied'):
            raise ValueError('Resume stage evidence changed')
        return actual

    for row in rows:
        path = root / 'cases' / row['context_id'] / 'status.json'
        if not path.exists():
            continue
        record = load(path)
        if (record.get('configuration_sha256') != config_hash
                or record.get('origin') != 'paired_target_only_review'
                or any(record.get(key) != row[key] for key in ('context_id', 'task', 'sample_id', 'resolved_turn'))):
            raise ValueError('Case status does not belong to this frozen paired run')
        initial = resumed_stage(row, 1, record.get('stage1'))
        final = resumed_stage(row, 2, record.get('stage2'))
        if final is not None and (initial is None or initial['decision'] != 'denied'):
            raise ValueError('Stage 2 evidence lacks a valid same-action stage 1 denial')
        if record.get('status') == 'completed':
            decision = initial['decision'] if initial else None
            if decision == 'denied':
                decision = final['decision'] if final else None
            if decision not in ('allowed', 'denied') or record.get('decision') != decision:
                raise ValueError('Completed case lacks valid same-action staged evidence')
        outcomes[row['context_id']] = record
    selected = smoke_selection(root, rows, samples) if args.smoke else rows
    if account_is_paused():
        return 2
    if not provider.api_key:
        pause('authentication_not_configured')
        return 2

    async def before_call():
        while not stop.is_set():
            if account_is_paused():
                return False
            delay = rate_limit_until - time.monotonic()
            if delay <= 0:
                return True
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(delay, 30.0))
            except TimeoutError:
                pass
        return False

    async def stage_review(row, stage):
        nonlocal rate_limit_until
        previous = outcomes.get(row['context_id'], {}).get('stage' + str(stage))
        accepted = resumed_stage(row, stage, previous)
        if accepted is not None:
            return accepted
        body, upstream = requests(row, stage)
        source = invocation_root / 'inputs' / 'experiments' / row['context_id'] / 'router' / 'traces' / f'{stage:04d}-reviewer-anthropic.messages'
        save(source / 'request.agent.json', body, immutable=True)
        save(source / 'metadata.json', {'endpoint': 'anthropic.messages', 'reviewer_stage': stage,
             'reviewed_action_id': row['target_call_id'], 'review_group_id': row['context_id'],
             'source_kind': 'paired_frozen_native_prefix_target_only'}, immutable=True)
        record = {'stage': stage, 'health': 'paused', 'decision': 'unknown'}
        for attempt in range(1, ATTEMPTS + 1):
            if not await before_call():
                return record
            output = root / 'review-attempts' / invocation / f'attempt-{attempt}'
            directory = output / row['context_id'] / source.name
            directory.mkdir(parents=True, exist_ok=False)
            save(directory / 'attempt.json', {'configuration_sha256': config_hash,
                 'context_id': row['context_id'], 'stage': stage, 'attempt': attempt,
                 'invocation_id': invocation, 'started_at': now()}, immutable=True)
            await rerun_single_reviewer(source, provider=provider, model=entry['model'],
                                        output_root=output, settings=settings, omit_stop_sequences=False)
            record, error = read_attempt(directory, row, stage, body, upstream, entry, config_hash)
            reason = pause_reason(error) if error else None
            if reason:
                pause(reason, row=row, stage=stage, directory=directory, http_status=record['http_status'])
            elif record['health'] == 'returned_model_mismatch':
                pause('returned_model_mismatch', row=row, stage=stage, directory=directory)
            save(directory / 'validation.json', record, immutable=True)
            if stop.is_set() or record['health'] == 'present':
                return record
            if record['http_status'] == 429:
                rate_limit_until = max(rate_limit_until, time.monotonic() + min(5 * attempt, 30))
        return record

    async def one(row):
        async with semaphore:
            if stop.is_set():
                return
            record = {'context_id': row['context_id'], 'task': row['task'], 'sample_id': row['sample_id'],
                      'resolved_turn': row['resolved_turn'], 'origin': 'paired_target_only_review',
                      'configuration_sha256': config_hash, 'model_key': args.model_key,
                      'level': args.level, 'status': 'unresolved'}
            previous = outcomes.get(row['context_id'], {})
            if isinstance(previous.get('stage2'), dict) and previous['stage2'].get('health') == 'present':
                record['stage2'] = previous['stage2']
            try:
                initial = await stage_review(row, 1)
                record['stage1'] = initial
                # Persist the gate immediately, so a restart never repays for an accepted stage 1.
                outcomes[row['context_id']] = record
                save(root / 'cases' / row['context_id'] / 'status.json', record)
                if initial['health'] == 'present' and initial['decision'] == 'allowed':
                    record.update(status='completed', decision='allowed', stage2='not_needed_stage1_allowed')
                elif initial['health'] == 'present' and initial['decision'] == 'denied':
                    final = await stage_review(row, 2)
                    record['stage2'] = final
                    if final['health'] == 'present' and final['decision'] in ('allowed', 'denied'):
                        record.update(status='completed', decision=final['decision'])
            except Exception as exc:
                record.update(error_type=type(exc).__name__, error=str(exc))
                pause('local_evidence_or_request_failure', row=row)
            record['finished_at'] = now()
            outcomes[row['context_id']] = record
            save(root / 'cases' / row['context_id'] / 'status.json', record)
            checkpoint('paused' if stop.is_set() else 'running')
            print(json.dumps({'context_id': row['context_id'], 'model_key': args.model_key,
                              'level': args.level, 'status': record['status'], 'decision': record.get('decision')}), flush=True)

    pending = [row for row in selected if outcomes.get(row['context_id'], {}).get('status') != 'completed']
    checkpoint('running')
    await asyncio.gather(*(one(row) for row in pending))
    complete = sum(value['status'] == 'completed' for value in outcomes.values()) == 1200
    selected_complete = all(outcomes.get(row['context_id'], {}).get('status') == 'completed' for row in selected)
    state = 'paused' if stop.is_set() else 'completed' if complete else 'smoke_finished' if args.smoke and selected_complete else 'finished_with_unresolved'
    checkpoint(state)
    save(invocation_root / 'finished.json', {'status': state, 'finished_at': now(),
         'selected_cases': len(selected), 'configuration_sha256': config_hash}, immutable=True)
    return 0 if not stop.is_set() and (complete or (args.smoke and selected_complete)) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--level', choices=('L1', 'L3'), required=True)
    parser.add_argument('--model-key', required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--rewrites', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--timeout-seconds', type=float, default=300)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    if args.concurrency < 1 or not 0 < args.timeout_seconds < float('inf'):
        parser.error('Concurrency and finite timeout must be positive')
    for field in ('models', 'plan', 'rewrites', 'output'):
        setattr(args, field, getattr(args, field).resolve())
    if (args.output.is_relative_to(args.plan.parent)
            or args.output.is_relative_to(args.rewrites)
            or args.rewrites.is_relative_to(args.output)):
        parser.error('Output must not overlap the prior campaign or shared rewrite directory')
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / '.writer.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, 'Another paired reviewer owns this output directory\n')
        try:
            return asyncio.run(run(args))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(json.dumps({'event': 'PAIRED_REVIEW_PREFLIGHT_FAILED', 'model_key': args.model_key,
                              'level': args.level, 'error_type': type(exc).__name__, 'detail': str(exc)}), file=sys.stderr, flush=True)
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
