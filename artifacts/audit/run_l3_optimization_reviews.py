#!/usr/bin/env python3
"""Review one frozen L3 candidate on a sample-grouped internal partition.

Example (run from the repository root):
  .venv/bin/python artifacts/audit/run_l3_optimization_reviews.py \
    --split artifacts/runs/rq4-l3-opt-20261104/split.json --partition dev \
    --models artifacts/runs/rq4-paired-four-models-20261104/models.json \
    --model-key deepseek-v4-pro \
    --rewrites artifacts/runs/rq4-l3-opt-20261104/candidates/v1/rewrites \
    --output artifacts/runs/rq4-l3-opt-20261104/candidates/v1/reviews/dev/deepseek-v4-pro \
    --concurrency 4 --timeout-seconds 300 --smoke

Omit --smoke on the SAME output to continue; it does not change configuration.
All partition rewrites are required even for smoke, but no other partition's
rewrites are required. Smoke preserves a fixed three-task x three-tool selection.
No payload or agent is executed. Only the existing reviewer replay sends requests.

Validation requires <split-directory>/selection.json with schema_version=1,
candidate_id, rewrites, generator_configuration_path,
generator_configuration_sha256 (file SHA256), generator_configuration (full JSON),
and split_sha256. Paths must identify this exact candidate. Extra selection
fields may document the development decision. Configuration binds the selection
file hash; changing it is not a resume.

Outputs: configuration.json and smoke-selection.json are immutable. Mutable
status.json reports full-partition coverage; results.json is an ordered array
containing EVERY partition case, including not_started and unresolved cases.
Case records contain identity, configuration_sha256, partition, model_key,
status, decision, stage1 and stage2. Stage evidence uses the paired runner's
strict read_attempt schema. cases/<context>/status.json is a resumable checkpoint;
invocations/<id>/{invocation,finished,status,results}.json and per-case gate/final
snapshots preserve invocation history. review-attempts/<id>/attempt-<1|2>/...
retains fresh native/upstream requests, raw responses/errors and validation.json.
configuration_sha256 is the existing content_hash of configuration JSON; fields
explicitly naming file hashes use SHA256 of bytes. Exit 0 means the requested
scope finished (smoke can still leave the partition incomplete); exit 2 means
paused, blocked, unresolved or preflight failure. Account pauses emit a JSON
L3_OPTIMIZATION_REVIEW_ALERT marker on stdout and retain local/shared alerts.
Resolve the account failure and acknowledge/remove the shared alert before resume.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from artifacts.audit import run_paired_target_reviews as paired
from artifacts.audit.run_paired_target_reviews import (
    ATTEMPTS, BUDGETS, CORE_FILES, TOOLS, digest, expected_upstream, load,
    make_review, model_settings, now, pause_reason, read_attempt, save,
    smoke_selection, validate_plan, validate_rewrites,
)
from router.reviewer import reviewer_stop_sequences
from router.tool_compatibility import content_hash
from scripts.replay_reviewer_runs import rerun_single_reviewer

MODELS = {'deepseek-v4-pro': 'deepseek-v4-pro', 'deepseek-v4.1-flash': 'deepseek-flash'}
ORIGIN = 'l3_optimization_partition_review'
ACCOUNT_REASONS = {'authentication_or_access_denied', 'authentication_not_configured',
                   'balance_exhausted', 'quota_exhausted'}


def partition_inputs(args):
    split = load(args.split)
    if split.get('schema_version') != 1:
        raise ValueError('Unsupported split schema')
    plan_path = Path(split['plan_path']).resolve()
    dataset_path = Path(split['dataset_path']).resolve()
    if (digest(plan_path) != split['plan_sha256']
            or dataset_path != ROOT / 'data/code_agent_attack_dataset.jsonl'
            or digest(dataset_path) != split['dataset_sha256']):
        raise ValueError('Frozen split plan/dataset hash mismatch')
    rows, samples, templates = validate_plan(plan_path)
    by_id = {row['context_id']: row for row in rows}
    case_sets, sample_sets = {}, {}
    for partition, count in (('dev', 240), ('validation', 960)):
        ids, sample_ids = split[partition + '_case_ids'], split[partition + '_sample_ids']
        if (not isinstance(ids, list) or len(ids) != count or len(set(ids)) != count
                or not isinstance(sample_ids, list) or len(sample_ids) != count // 3
                or len(set(sample_ids)) != count // 3 or not set(ids) <= by_id.keys()):
            raise ValueError('Invalid partition case/sample membership: ' + partition)
        case_sets[partition], sample_sets[partition] = set(ids), set(sample_ids)
        expected = {row['context_id'] for row in rows if row['sample_id'] in sample_ids}
        if set(ids) != expected or not set(sample_ids) <= samples.keys():
            raise ValueError('Sample grouping differs from frozen partition: ' + partition)
    if (case_sets['dev'] & case_sets['validation']
            or case_sets['dev'] | case_sets['validation'] != by_id.keys()
            or sample_sets['dev'] & sample_sets['validation']
            or sample_sets['dev'] | sample_sets['validation'] != samples.keys()):
        raise ValueError('Split must cover the plan exactly without sample overlap')
    return split, plan_path, [by_id[key] for key in split[args.partition + '_case_ids']], samples, templates


def selection_identity(args, generator, generator_hash):
    if args.partition == 'dev':
        return None
    path = args.split.parent / 'selection.json'
    selection = load(path)  # Missing selection fails closed before any reviewer request.
    expected = {
        'schema_version': 1, 'candidate_id': args.rewrites.parent.name,
        'rewrites': str(args.rewrites),
        'generator_configuration_path': str(args.rewrites / 'configuration.json'),
        'generator_configuration_sha256': generator_hash,
        'generator_configuration': generator, 'split_sha256': digest(args.split),
    }
    if any(selection.get(key) != value for key, value in expected.items()):
        raise ValueError('Validation selection does not lock this exact candidate/configuration/split')
    return {'path': str(path), 'sha256': digest(path), 'selection': selection}


def publish_account_alert(path, alert):
    """Publish a complete first-writer-wins shared marker across model processes."""
    temporary = path.with_name('.alert-' + uuid4().hex + '.json')
    save(temporary, alert, immutable=True)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # Never overwrite another process's unacknowledged failure.
    finally:
        temporary.unlink()


async def run(args):
    root = args.output
    split, plan_path, rows, samples, templates = partition_inputs(args)
    entries = [entry for entry in load(args.models)['models'] if entry.get('key') == args.model_key]
    if len(entries) != 1:
        raise ValueError('Select exactly one explicit model manifest entry')
    entry = entries[0]
    if (entry.get('provider') != 'deepseek' or entry.get('protocol') != 'chat'
            or entry.get('model') != MODELS[args.model_key]
            or entry.get('returned_models') != [MODELS[args.model_key]]
            or entry.get('api_key_env') != 'DEEPSEEK_API_KEY'
            or entry.get('account_id') != 'deepseek' or entry.get('available') is not True
            or entry.get('identity_verified') is not True):
        raise ValueError('Only the frozen, available DeepSeek V4 Pro/V4.1 Flash entries are eligible')
    settings, provider, identity = model_settings(entry, args.timeout_seconds)
    texts, rewrite_hashes = validate_rewrites(args.rewrites, rows)
    generator_path = args.rewrites / 'configuration.json'
    generator = load(generator_path)
    if (generator.get('split_sha256') != digest(args.split)
            or generator.get('candidate_id') != args.rewrites.parent.name):
        raise ValueError('Generator configuration is not bound to this candidate and frozen split')
    selection = selection_identity(args, generator, rewrite_hashes['configuration_sha256'])
    account_alert = args.models.parent / 'accounts/deepseek/alert.json'
    grouped = {}
    for row in rows:
        grouped.setdefault((row['task'], samples[row['sample_id']]['tool_name']), row['context_id'])
    smoke_ids = list(grouped.values())
    if len(smoke_ids) != 9 or {tool for _, tool in grouped} != TOOLS:
        raise ValueError('Partition lacks the fixed three-task x three-tool smoke coverage')
    sources = {source: value for row in rows for source, value in row['source_hashes'].items()}
    configuration = {
        'schema_version': 1, 'origin': ORIGIN, 'level': 'L3', 'partition': args.partition,
        'candidate': args.rewrites.parent.name, 'output_directory': str(root),
        'rewrites_directory': str(args.rewrites), 'selected_model_identity': identity,
        'models_path': str(args.models), 'models_sha256': digest(args.models),
        'split_path': str(args.split), 'split_sha256': digest(args.split),
        'case_ids': [row['context_id'] for row in rows], 'smoke_case_ids': smoke_ids,
        'plan_path': str(plan_path), 'plan_sha256': split['plan_sha256'],
        'dataset_path': split['dataset_path'], 'dataset_sha256': split['dataset_sha256'],
        'source_sha256': sources, 'executor_sha256': digest(__file__),
        'paired_runner_sha256': digest(paired.__file__),
        'core_executor_sha256': {path: digest(ROOT / path) for path in CORE_FILES},
        'generator_configuration_path': str(generator_path), 'generator_configuration': generator,
        'shared_rewrites': rewrite_hashes, 'selection': selection, 'planned_cases': len(rows),
        'timeout_seconds': args.timeout_seconds, 'stage1_max_tokens': BUDGETS[1],
        'stage2_max_tokens': BUDGETS[2], 'reviewer_thinking': 'disabled', 'stop_policy': 'preserve',
        'template_stop_sequences': {path: list(reviewer_stop_sequences(templates[path]))
                                    for path in sorted({p for row in rows for p in row['stage_templates'].values()})},
        'stage_gating': 'stage1_allow_terminates; stage1_deny_requires_same_action_stage2',
        'max_attempts_per_stage_per_invocation': ATTEMPTS, 'transport_max_attempts': 1,
        'ordinary_429_delay_seconds': [5, 10], 'payload_execution': False,
        'reuse_prior_campaign_results': False, 'account_alert_path': str(account_alert),
    }
    config_path = root / 'configuration.json'
    if config_path.exists():
        if load(config_path) != configuration:
            raise ValueError('Resume configuration differs; refusing candidate/partition/input reuse')
    else:
        if any(path.name != '.writer.lock' for path in root.iterdir()):
            raise ValueError('Unconfigured output contains artifacts; refusing prior-run reuse')
        save(config_path, configuration, immutable=True)
    config_hash = content_hash(configuration)
    smoke_rows = smoke_selection(root, rows, samples)
    if [row['context_id'] for row in smoke_rows] != smoke_ids:
        raise ValueError('Persistent smoke selection differs from frozen configuration')
    selected = smoke_rows if args.smoke else rows

    def requests(row, stage):
        body = make_review(row, stage, 'L3', texts[row['context_id']], templates, settings)
        return body, expected_upstream(body, stage, entry, settings, provider)

    # Verify all selected input requests before any calls, without executing a target.
    for row in rows:
        for stage in (1, 2):
            requests(row, stage)

    def case_record(row):
        return {**{key: row[key] for key in ('context_id', 'task', 'sample_id', 'resolved_turn')},
                'origin': ORIGIN, 'configuration_sha256': config_hash, 'model_key': args.model_key,
                'level': 'L3', 'partition': args.partition, 'candidate': args.rewrites.parent.name,
                'status': 'not_started', 'decision': 'unknown', 'stage1': None, 'stage2': None}

    def verify_attempt(row, stage, directory):
        directory = directory.resolve()
        metadata = load(directory / 'attempt.json')
        expected = (root / 'review-attempts' / metadata['invocation_id'] /
                    f"attempt-{metadata['attempt']}" / row['context_id'] /
                    f'{stage:04d}-reviewer-anthropic.messages')
        if (not directory.is_relative_to(root / 'review-attempts')
                or directory != expected or metadata['attempt'] not in (1, 2)):
            raise ValueError('Attempt evidence is outside its exact candidate/partition/invocation')
        body, upstream = requests(row, stage)
        record, error = read_attempt(directory, row, stage, body, upstream, entry, config_hash)
        if (directory / 'validation.json').exists() and load(directory / 'validation.json') != record:
            raise ValueError('Attempt evidence changed after validation')
        return record, error

    outcomes = {}
    for row in rows:
        record = case_record(row)
        path = root / 'cases' / row['context_id'] / 'status.json'
        if path.exists():
            prior = load(path)
            for key in ('context_id', 'task', 'sample_id', 'resolved_turn', 'origin',
                        'configuration_sha256', 'model_key', 'partition', 'candidate', 'level'):
                if prior.get(key) != record[key]:
                    raise ValueError('Case checkpoint belongs to different frozen inputs')
            if prior.get('status') not in ('not_started', 'unresolved', 'completed'):
                raise ValueError('Invalid case checkpoint status')
            record = prior
        # Recover accepted evidence even if interrupted before the case checkpoint.
        # Never retry an accepted verdict, or choose a preferred verdict among attempts.
        accepted = {}
        for stage in (1, 2):
            stage_key = 'stage' + str(stage)
            previous = record.get(stage_key)
            latest = None
            pattern = f'*/attempt-*/{row["context_id"]}/{stage:04d}-reviewer-anthropic.messages/attempt.json'
            for metadata_path in sorted((root / 'review-attempts').glob(pattern)):
                directory = metadata_path.parent
                if not all((directory / name).exists() for name in ('request.agent.json', 'request.upstream.json')):
                    continue  # An interrupted attempt was allocated but never sent.
                actual, _ = verify_attempt(row, stage, directory)
                latest = actual
                if actual['health'] == 'present' and actual['decision'] in ('allowed', 'denied'):
                    if stage in accepted:
                        raise ValueError('Multiple accepted attempts for one stage; refusing best-of selection')
                    accepted[stage] = actual
            if isinstance(previous, dict) and previous.get('health') == 'present':
                if accepted.get(stage) != previous:
                    raise ValueError('Accepted checkpoint lacks unchanged local attempt evidence')
            if stage in accepted:
                record[stage_key] = accepted[stage]
            elif latest is not None:
                record[stage_key] = latest
                if record['status'] == 'not_started':
                    record['status'] = 'unresolved'
        initial, final = accepted.get(1), accepted.get(2)
        if final is not None and (initial is None or initial['decision'] != 'denied'):
            raise ValueError('Stage 2 lacks an accepted same-action stage 1 denial')
        decision = initial['decision'] if initial else None
        if decision == 'denied':
            decision = final['decision'] if final else None
        if record['status'] == 'completed' and (decision is None or record.get('decision') != decision):
            raise ValueError('Completed case lacks strict staged evidence')
        if decision is not None:
            record.update(status='completed', decision=decision)
            if initial['decision'] == 'allowed':
                record['stage2'] = 'not_needed_stage1_allowed'
        elif initial is not None:
            record['status'] = 'unresolved'
        outcomes[row['context_id']] = record

    invocation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex
    invocation_root = root / 'invocations' / invocation
    invocation_root.mkdir(parents=True, exist_ok=False)
    save(invocation_root / 'invocation.json', {
        'invocation_id': invocation, 'started_at': now(), 'configuration_sha256': config_hash,
        'concurrency': args.concurrency, 'smoke': args.smoke,
        'selected_case_ids': [row['context_id'] for row in selected],
    }, immutable=True)
    stop, stop_details = asyncio.Event(), {}
    semaphore = asyncio.Semaphore(args.concurrency)
    rate_limit_until = 0.0

    def checkpoint(state):
        results = [outcomes[row['context_id']] for row in rows]
        counts = Counter(record['status'] for record in results)
        status = {
            'schema_version': 1, 'status': state, 'reason': stop_details.get('reason'),
            'stop_reason': dict(stop_details), 'model_key': args.model_key,
            'model': entry['model'], 'provider': 'deepseek', 'level': 'L3',
            'candidate': args.rewrites.parent.name, 'partition': args.partition,
            'planned_cases': len(rows), 'completed_cases': counts['completed'],
            'unresolved_cases': counts['unresolved'], 'not_started_cases': counts['not_started'],
            'remaining_cases': len(rows) - counts['completed'],
            'selected_cases': len(selected), 'smoke': args.smoke,
            'decisions': dict(Counter(record['decision'] for record in results if record['status'] == 'completed')),
            'configuration_sha256': config_hash, 'invocation_id': invocation,
            'concurrency': args.concurrency, 'timeout_seconds': args.timeout_seconds,
            'reused_prior_campaign_cases': 0, 'payload_execution': False, 'updated_at': now(),
        }
        save(root / 'results.json', results)
        save(root / 'status.json', status)
        return status, results

    def pause(reason, *, row=None, stage=None, directory=None, http_status=None):
        if stop.is_set() and (reason not in ACCOUNT_REASONS or stop_details.get('reason') in ACCOUNT_REASONS):
            return
        stop.set()
        stop_details.update(reason=reason, model_key=args.model_key, model=entry['model'],
                            provider='deepseek', account_id='deepseek', level='L3',
                            candidate=args.rewrites.parent.name, partition=args.partition,
                            context_id=row['context_id'] if row else None, stage=stage,
                            http_status=http_status, evidence_dir=str(directory) if directory else None,
                            invocation_id=invocation, detected_at=now())
        alert = {'event': 'L3_OPTIMIZATION_REVIEW_ALERT', **stop_details,
                 'action': 'paused_new_calls; already_sent_calls_retain_evidence; resolve_before_restart'}
        if reason in ACCOUNT_REASONS:
            publish_account_alert(account_alert, alert)
        save(root / 'alert.json', alert)
        alert_path = invocation_root / 'alert.json'
        if alert_path.exists():
            alert_path = invocation_root / 'account-alert.json'
        save(alert_path, alert, immutable=True)
        print(json.dumps(alert, ensure_ascii=False), flush=True)
        checkpoint('paused')

    def account_is_paused():
        if not account_alert.exists():
            return False
        try:
            shared = load(account_alert)
        except (OSError, ValueError):
            shared = {'reason': 'unreadable_shared_account_marker'}
        stop_details.update(account_reason=shared.get('reason'), account_alert=str(account_alert))
        pause('shared_account_paused', directory=account_alert.parent, http_status=shared.get('http_status'))
        return True

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
        previous = outcomes[row['context_id']].get('stage' + str(stage))
        if isinstance(previous, dict) and previous.get('health') == 'present':
            return previous  # All accepted stages were verified before this invocation.
        body, upstream = requests(row, stage)
        source = (invocation_root / 'inputs/experiments' / row['context_id'] / 'router/traces' /
                  f'{stage:04d}-reviewer-anthropic.messages')
        save(source / 'request.agent.json', body, immutable=True)
        save(source / 'metadata.json', {'endpoint': 'anthropic.messages', 'reviewer_stage': stage,
             'reviewed_action_id': row['target_call_id'], 'review_group_id': row['context_id'],
             'source_kind': ORIGIN}, immutable=True)
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
            # Detect billing/auth errors even if subsequent evidence validation fails.
            raw_error = load(directory / 'error.json') if (directory / 'error.json').exists() else {}
            reason = pause_reason(raw_error) if raw_error else None
            if reason:
                pause(reason, row=row, stage=stage, directory=directory,
                      http_status=raw_error.get('status_code'))
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
                rate_limit_until = max(rate_limit_until, time.monotonic() + 5 * attempt)
        return record

    async def one(row):
        async with semaphore:
            if stop.is_set():
                return
            record = dict(outcomes[row['context_id']])
            record.update(status='unresolved', decision='unknown', invocation_id=invocation)
            try:
                initial = await stage_review(row, 1)
                record['stage1'] = initial
                outcomes[row['context_id']] = record
                save(root / 'cases' / row['context_id'] / 'status.json', record)
                save(invocation_root / 'cases' / row['context_id'] / 'stage1.json', record, immutable=True)
                if initial['health'] == 'present' and initial['decision'] == 'allowed':
                    record.update(status='completed', decision='allowed', stage2='not_needed_stage1_allowed')
                elif initial['health'] == 'present' and initial['decision'] == 'denied':
                    final = await stage_review(row, 2)
                    record['stage2'] = final
                    if final['health'] == 'present' and final['decision'] in ('allowed', 'denied'):
                        record.update(status='completed', decision=final['decision'])
            except Exception as exc:
                record.update(error_type=type(exc).__name__, error=str(exc))
                save(invocation_root / 'cases' / row['context_id'] / 'local-error.json',
                     {'error_type': type(exc).__name__, 'detail': str(exc), 'detected_at': now()}, immutable=True)
                pause('local_evidence_or_request_failure', row=row)
            record['finished_at'] = now()
            outcomes[row['context_id']] = record
            save(root / 'cases' / row['context_id'] / 'status.json', record)
            save(invocation_root / 'cases' / row['context_id'] / 'finished.json', record, immutable=True)
            checkpoint('paused' if stop.is_set() else 'running')
            print(json.dumps({'event': 'L3_OPTIMIZATION_CASE', 'context_id': row['context_id'],
                  'model_key': args.model_key, 'partition': args.partition,
                  'status': record['status'], 'decision': record['decision']}), flush=True)

    if not account_is_paused() and not provider.api_key:
        pause('authentication_not_configured')
    if not stop.is_set():
        checkpoint('running')
        pending = [row for row in selected if outcomes[row['context_id']]['status'] != 'completed']
        await asyncio.gather(*(one(row) for row in pending))
    complete = all(record['status'] == 'completed' for record in outcomes.values())
    selected_complete = all(outcomes[row['context_id']]['status'] == 'completed' for row in selected)
    state = ('paused' if stop.is_set() else 'completed' if complete else
             'smoke_finished' if args.smoke and selected_complete else 'finished_with_unresolved')
    status, results = checkpoint(state)
    save(invocation_root / 'status.json', status, immutable=True)
    save(invocation_root / 'results.json', results, immutable=True)
    save(invocation_root / 'finished.json', {'status': state, 'finished_at': now(),
         'selected_cases': len(selected), 'planned_cases': len(rows),
         'configuration_sha256': config_hash}, immutable=True)
    return 0 if not stop.is_set() and (complete or (args.smoke and selected_complete)) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', type=Path, required=True)
    parser.add_argument('--partition', choices=('dev', 'validation'), required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--model-key', choices=tuple(MODELS), required=True)
    parser.add_argument('--rewrites', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--timeout-seconds', type=float, default=300)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    if args.concurrency < 1 or not 0 < args.timeout_seconds < float('inf'):
        parser.error('Concurrency and finite timeout must be positive')
    for field in ('split', 'models', 'rewrites', 'output'):
        setattr(args, field, getattr(args, field).resolve())
    candidate = args.rewrites.parent
    expected_output = candidate / 'reviews' / args.partition / args.model_key
    if (args.rewrites.name != 'rewrites' or candidate.parent != args.split.parent / 'candidates'
            or args.output != expected_output or args.output.is_relative_to(args.models.parent)):
        parser.error('Use exactly <split-dir>/candidates/<candidate>/rewrites and reviews/<partition>/<model-key>')
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / '.writer.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, 'Another reviewer owns this output directory\n')
        try:
            return asyncio.run(run(args))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(json.dumps({'event': 'L3_OPTIMIZATION_REVIEW_PREFLIGHT_FAILED',
                  'model_key': args.model_key, 'partition': args.partition,
                  'error_type': type(exc).__name__, 'detail': str(exc)}), flush=True)
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
