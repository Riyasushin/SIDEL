"""Record native Claude pre-execution rejections that precede PreToolUse hooks.

Only known input-validation messages for Edit are accepted. A generic tool
error, shell exit status, or other post-invocation error is never evidence of
skipped execution. Original client requests remain the evidence source.
"""
from __future__ import annotations
import argparse,fcntl,hashlib,json,os,re,time
from datetime import datetime,timezone
from pathlib import Path

EDIT_VALIDATION_PATTERNS=(
    r'^<tool_use_error>File does not exist\. Note: your current working directory is .+</tool_use_error>$',
    r'^<tool_use_error>String to replace not found in file\.\nString: .*</tool_use_error>$',
    r'^<tool_use_error>Found \d+ matches of the string to replace, but replace_all is false\. To replace all occurrences, set replace_all to true\. To replace only one occurrence, please provide more context to uniquely identify the instance\.\nString: .*</tool_use_error>$',
    r'^<tool_use_error>No changes to make: old_string and new_string are exactly the same\.</tool_use_error>$',
    r'^<tool_use_error>Cannot create new file - file already exists\.</tool_use_error>$',
)

def validation_rejection(tool,block):
    if tool!='Edit' or block.get('is_error') is not True:return False
    content=block.get('content')
    return isinstance(content,str) and any(re.fullmatch(pattern,content,re.DOTALL) for pattern in EDIT_VALIDATION_PATTERNS)

def collect(context):
    receipt=context/'container/simulated-tool-calls.jsonl'
    if receipt.exists():return True
    traces=context/'router/traces';state=traces/'simulated_tool_results.json'
    if not state.exists():return False
    call_id=json.loads(state.read_text())['tool_call_id']
    injections=list(traces.glob('*/injection.json'))
    if len(injections)!=1:return False
    injection=json.loads(injections[0].read_text());tool=injection['tool_name']
    if tool!='Edit':return False
    for request in sorted(traces.glob('*/request.agent.json')):
        body=json.loads(request.read_text())
        for message in body.get('messages',[]):
            if not isinstance(message,dict) or message.get('role')!='user' or not isinstance(message.get('content'),list):continue
            for block in message['content']:
                if isinstance(block,dict) and block.get('type')=='tool_result' and block.get('tool_use_id')==call_id and validation_rejection(tool,block):
                    record={'timestamp':datetime.now(timezone.utc).isoformat(),'tool_use_id':call_id,'tool_name':tool,'executed':False,'execution_mode':'simulated',
                            'source':'claude_preexecution_input_validation','rejection':block['content'],'evidence_request_artifact':str(request),
                            'evidence_sha256':hashlib.sha256(request.read_bytes()).hexdigest(),
                            'basis':'Exact message from Claude Code 2.1.319 Edit.validateInput result:false branch, before Edit.call or PreToolUse; generic execution errors are excluded.'}
                    with receipt.open('a+') as handle:
                        fcntl.flock(handle,fcntl.LOCK_EX);handle.seek(0)
                        if not any(json.loads(line).get('tool_use_id')==call_id for line in handle if line.strip()):handle.write(json.dumps(record)+'\n')
                    return True
    return False

def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('run_roots',nargs='+',type=Path);args=parser.parse_args();seen=set()
    while True:
        active=False
        for root in args.run_roots:
            for context in (root/'experiments').iterdir():
                if str(context) in seen or not context.is_dir():continue
                try:
                    if collect(context):seen.add(str(context))
                except (FileNotFoundError,json.JSONDecodeError):pass
            pidfile=root/'controller.pid'
            if pidfile.exists():
                try:os.kill(int(pidfile.read_text()),0);active=True
                except ProcessLookupError:pass
        if not active:break
        time.sleep(1)
if __name__=='__main__':main()
