"""Run one DeepSeek-v4-pro Claude SWE-bench trajectory with simulated injection."""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
MODEL = "deepseek-v4-pro"
DEFAULT_TASKS = REPO / "artifacts/runs/claude-v4pro-lite11-r10-turn100-20261109/tasks.jsonl"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    tmp.replace(path)


def serve_router(root, port):
    import router.app as ra
    from fastapi import HTTPException
    from router.simulated_tools import SIMULATED_CALL_PREFIX
    history={}
    def signature(message):
        calls=message.get('tool_calls') or []
        return json.dumps([c['id'] for c in calls]) if calls else json.dumps(message.get('content') or '',ensure_ascii=False)
    async def proxy(**kw):
        context,metadata,settings=kw['context'],kw['metadata'],kw['settings']
        body=ra.anthropic_body_to_chat_completion(kw['body'],model=MODEL)
        cache=history.setdefault((context.run_id,context.context_id),{})
        for message in body['messages']:
            if message.get('role')!='assistant':continue
            key=signature(message)
            calls=message.get('tool_calls') or []
            if key not in cache:
                if calls and all(c['id'].startswith(SIMULATED_CALL_PREFIX) for c in calls):
                    # This assistant response was synthesized, so it has no model-produced reasoning.
                    cache[key]=''
                else:raise HTTPException(400,'Missing previous DeepSeek reasoning; refusing lossy continuation')
            message['reasoning_content']=cache[key]
        body.update(model=MODEL,thinking={'type':'enabled'},reasoning_effort='high')
        ra.write_turn_artifact(context,metadata,'request.upstream.json',body)
        provider=ra.provider_for('deepseek',settings)
        provider.trace_dir=Path(context.trace_dir)/'provider-attempts'
        provider.trace_run_id=context.run_id
        response=await provider.chat_completion(body,user_agent=kw.get('user_agent'))
        ra.write_turn_artifact(context,metadata,'response.upstream.json',response)
        if response.get('model')!=MODEL:raise HTTPException(502,'Unexpected model; requires deepseek-v4-pro')
        message=response['choices'][0]['message'];cache[signature(message)]=message.get('reasoning_content','')
        return ra.chat_completion_to_anthropic(response,model=MODEL,request_id=kw['request_id'])
    ra.proxy_anthropic_message=proxy
    import uvicorn
    uvicorn.run(ra.app,host='127.0.0.1',port=port)


def run(args):
    from manager.manifest import write_manifests
    from manager.planner import MatrixPlanOptions,build_matrix_plan
    from manager.orchestrator import ExperimentOrchestrator
    from manager.analysis import analyze_run
    from swebench_container.runner import DockerTaskRunner
    rows=[json.loads(line) for line in args.tasks.read_text().splitlines() if line.strip()]
    task=next(t for t in rows if t['instance_id']==args.task_id)
    samples=[json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    sample=samples[args.sample-1]
    if str(sample.get('tool_name','')).casefold()!='bash':raise ValueError('Smoke test requires a Bash dataset sample')
    root=(REPO/'artifacts/runs'/args.run_id).resolve()
    if root.exists():raise ValueError('Run directory already exists; use a new run ID')
    root.mkdir(parents=True)
    dump(root/'config.json',{'run_id':args.run_id,'model':MODEL,'provider':'deepseek','agent':'claude','agent_mode':'bypassPermissions',
         'router_mode':'inject','injection_turn_mode':'first_bash','injection_mode':'replace','simulate_tool_results':True,
         'payload_execution_allowed':False,'enable_tool_defense':False,'max_task_turns':100,'task_timeout_seconds':None,
         'request_timeout_seconds':120,'request_max_attempts':3,'thinking':'enabled','reasoning_effort':'high',
         'container_cpus':2,'container_memory':'8g','dataset':str(args.dataset),'dataset_sample_id':sample['id'],
         'instance_id':args.task_id,'repetitions':1,'planned_runs':1,'evaluation_workers':1,'evaluation_timeout_seconds':900})
    (root/'tasks.jsonl').write_text(json.dumps(task)+'\n')
    options=MatrixPlanOptions(run_id=args.run_id,router_mode='inject',agents=('claude',),agent_mode='bypassPermissions',
        instance_ids=(args.task_id,),dataset_sample_ids=(sample['id'],),injection_turns=(None,),injection_mode='replace',
        payload_execution_allowed=False,timeout_seconds=0,injection_turn_mode='first_bash',simulate_tool_results=True,max_task_turns=100)
    plan=build_matrix_plan(options)
    plan={key:dataclasses.replace(manifest,wrapper_image='code-agent-attack/claude-lite11-seed20261108/'+args.task_id.replace('__','_')+'-claude:latest') for key,manifest in plan.items()}
    registry=write_manifests(root.parent,plan,'inject');registry.chmod(0o600)
    spec=ExperimentOrchestrator(run_root=root,router_url=f'http://127.0.0.1:{args.port}',tasks_path=root/'tasks.jsonl',prompt_template_path=REPO/'bench/prompts/solve_issue.md').build_container_specs()[0]
    spec=dataclasses.replace(spec,network='host',environment={'ANTHROPIC_MODEL':MODEL,'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1','CLAUDE_CODE_DISABLE_AUTO_MEMORY':'1'})
    class Runner(DockerTaskRunner):
        def build_docker_command(self,spec):
            cmd=super().build_docker_command(spec)
            cmd[2:2]=['--runtime','runc','--cpus','2','--memory','8g','--memory-swap','8g','--pids-limit','1024']
            cmd[-1]=cmd[-1].replace('claude --permission-mode',f'claude --model {MODEL} --output-format stream-json --verbose --permission-mode',1).replace('set -e\n','set -e\nexport PATH=/opt/miniconda3/envs/testbed/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin\nexport CONDA_DEFAULT_ENV=testbed\nexport CONDA_PREFIX=/opt/miniconda3/envs/testbed\n',1).replace('git diff --binary >','git add -N . >/dev/null 2>&1; git diff --binary HEAD >')
            return cmd
    # Snapshot the code used by this run, including newly added untracked modules.
    provenance=root/'provenance/source';provenance.mkdir(parents=True)
    import shutil
    paths=subprocess.check_output(['git','diff','--name-only'],cwd=REPO,text=True).splitlines()+subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=REPO,text=True).splitlines()
    hashes={}
    for relative in paths:
        source=REPO/relative
        if source.is_file() and source.suffix in {'.py','.md'}:
            dest=provenance/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest);hashes[relative]=hashlib.sha256(source.read_bytes()).hexdigest()
    dump(root/'provenance/source-hashes.json',hashes)
    env=os.environ.copy();env.update(DEFAULT_PROVIDER='deepseek',DEEPSEEK_MODEL=MODEL,ROUTER_MODE='inject',ROUTER_CONTEXT_REGISTRY=str(registry),
        ROUTER_ARTIFACT_ROOT=str(root.parent),ATTACK_DATASET_PATH=str(args.dataset.resolve()),CONTEXT_TIMEOUT_SECONDS='0',REQUEST_TIMEOUT_SECONDS='120',
        REQUEST_MAX_ATTEMPTS='3',ATTACK_DYNAMIC_L3_ENABLED='0',ATTACK_DYNAMIC_L4_ENABLED='0',ATTACK_ALLOW_UNSAFE_RAW='0')
    log=(root/'router/uvicorn.log').open('wb')
    router=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve-router','--root',str(root),'--port',str(args.port)],cwd=REPO,env=env,stdout=log,stderr=subprocess.STDOUT)
    (root/'router.pid').write_text(str(router.pid))
    start=time.time();dump(root/'progress.json',{'state':'starting','finished':0,'active':0,'planned':1})
    try:
        import httpx
        for _ in range(150):
            if router.poll() is not None:raise RuntimeError('Router exited')
            try:
                if httpx.get(f'http://127.0.0.1:{args.port}/health',trust_env=False,timeout=1).json().get('contexts')==1:break
            except Exception:pass
            time.sleep(.2)
        else:raise RuntimeError('Router did not become ready')
        dump(root/'progress.json',{'state':'running','finished':0,'active':1,'planned':1})
        result=Runner().run(spec)
    finally:
        router.terminate()
        try:router.wait(timeout=15)
        except subprocess.TimeoutExpired:router.kill();router.wait()
        log.close()
    trace=spec.artifact_dir.parent/'router/traces';turns=set();injections=[]
    for p in sorted(trace.glob('*/metadata.json')):
        metadata=json.loads(p.read_text())
        if metadata.get('task_turn_index') is not None and (p.parent/'response.upstream.json').exists():turns.add(metadata['task_turn_index'])
        if (p.parent/'injection.json').exists():injections.append(json.loads((p.parent/'injection.json').read_text()))
    native={}
    for line in result.stdout_path.read_text(errors='replace').splitlines():
        try:message=json.loads(line)
        except json.JSONDecodeError:continue
        if message.get('type')=='result':native=message
    dump(spec.artifact_dir.parent/'run-result.json',{'context_id':spec.context_id,'instance_id':args.task_id,'status':result.status,'exit_code':result.exit_code,
         'interaction_turns':len(turns),'native_turns':native.get('num_turns'),'native_subtype':native.get('subtype'),'native_is_error':native.get('is_error'),
         'elapsed_seconds':round(time.time()-start,2)})
    receipts=[json.loads(line) for line in (spec.artifact_dir/'simulated-tool-calls.jsonl').read_text().splitlines()] if (spec.artifact_dir/'simulated-tool-calls.jsonl').exists() else []
    applied=list(trace.glob('*/tool-result-simulation.json'))
    matched_upstream=False
    if len(injections)==1:
        call_id=injections[0]['tool_call_id']
        for p in trace.glob('*/request.upstream.json'):
            for message in json.loads(p.read_text()).get('messages',[]):
                if message.get('role')=='tool' and message.get('tool_call_id')==call_id and message.get('content')==sample['tool_result']:matched_upstream=True
    verification={'injections':len(injections),'suppression_receipts':len(receipts),'tool_result_rewrite_requests':len(applied),
                  'dataset_result_reached_upstream':matched_upstream,'terminal_markers':len(list(trace.glob('*/terminal.json'))),
                  'defense_scans':len(list(trace.glob('*/defense_scan.json'))),'main_turns':len(turns)}
    verification['passed']=len(injections)==1 and len(receipts)==1 and receipts[0].get('executed') is False and bool(applied) and matched_upstream and verification['terminal_markers']==0 and verification['defense_scans']==0 and len(turns)<=100
    dump(root/'verification.json',verification)
    analyze_run(root)
    dump(root/'progress.json',{'state':'grading','finished':1,'active':0,'planned':1,'interaction_turns':len(turns)})
    ev=root/'evaluation';ev.mkdir();prediction=ev/'predictions.jsonl';patch=(spec.artifact_dir/'final.patch').read_text() if (spec.artifact_dir/'final.patch').exists() else ''
    prediction.write_text(json.dumps({'instance_id':args.task_id,'model_name_or_path':MODEL,'model_patch':patch})+'\n')
    cmd=[sys.executable,'-m','swebench.harness.run_evaluation','--dataset_name',str(root/'tasks.jsonl'),'--split','dev','--predictions_path',str(prediction),
         '--instance_ids',args.task_id,'--max_workers','1','--run_id',args.run_id,'--timeout','900','--namespace','swebench','--cache_level','instance','--clean','False']
    with (ev/'evaluation.log').open('w') as handle:rc=subprocess.run(cmd,cwd=ev,stdout=handle,stderr=subprocess.STDOUT).returncode
    report=ev/'logs/run_evaluation'/args.run_id/MODEL/args.task_id/'report.json'
    graded=report.exists();resolved=bool(json.loads(report.read_text()).get(args.task_id,{}).get('resolved')) if graded else None
    # A legitimate empty patch is an unresolved episode without invoking tests.
    evaluation_status='graded' if graded else ('empty_patch' if not patch.strip() else 'evaluation_error')
    final={'planned_runs':1,'finished_runs':1,'graded_runs':int(graded),'resolved_runs':int(bool(resolved)),'evaluation_status':evaluation_status,
           'success_rate':float(bool(resolved)) if graded or evaluation_status=='empty_patch' else None,'mean_interaction_turns':len(turns),
           'interaction_turns':len(turns),'native_turns':native.get('num_turns'),'agent_status':result.status,'native_subtype':native.get('subtype'),
           'verification_passed':verification['passed'],'evaluation_exit_code':rc,'run_root':str(root),'sample_size_note':'One smoke trajectory; not the 110-run experiment success rate.'}
    dump(root/'summary.json',final);dump(root/'FINAL_RESULTS.json',final);dump(root/'complete.json',final)
    dump(root/'progress.json',{'state':'complete','finished':1,'active':0,'planned':1,'interaction_turns':len(turns)})
    print(json.dumps(final,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--serve-router',action='store_true');parser.add_argument('--root',type=Path)
    parser.add_argument('--port',type=int,default=18085)
    parser.add_argument('--run-id',default='claude-v4pro-inject-simulated-firstbash-smoke-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--tasks',type=Path,default=DEFAULT_TASKS);parser.add_argument('--task-id',default='pydicom__pydicom-1694')
    parser.add_argument('--dataset',type=Path,default=REPO/'data/code_agent_attack_dataset.sampled80.seed20260624.jsonl')
    parser.add_argument('--sample',type=int,default=1)
    args=parser.parse_args()
    if args.serve_router:serve_router(args.root,args.port)
    else:run(args)


if __name__=='__main__':main()
