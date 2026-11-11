import json,tempfile,unittest
from pathlib import Path
from scripts.collect_simulated_validation_receipts import validation_rejection,collect
class NativeValidationReceiptTest(unittest.TestCase):
 def test_accepts_only_known_preexecution_validation_errors_for_edit(self):
  block={'is_error':True,'content':'<tool_use_error>File does not exist. Note: your current working directory is /testbed.</tool_use_error>'}
  self.assertTrue(validation_rejection('Edit',block));self.assertFalse(validation_rejection('Bash',block))
  self.assertFalse(validation_rejection('Edit',{**block,'is_error':False}))
  for text in ['Exit code 1','<tool_use_error>Permission denied</tool_use_error>','File does not exist.']:
   self.assertFalse(validation_rejection('Edit',{'is_error':True,'content':text}))
 def test_receipt_is_correlated_idempotent_and_retains_original_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   c=Path(tmp);trace=c/'router/traces';turn=trace/'0000-anthropic.messages';follow=trace/'0001-anthropic.messages'
   turn.mkdir(parents=True);follow.mkdir();(c/'container').mkdir();cid='toolu_sidel_sim_123'
   (trace/'simulated_tool_results.json').write_text(json.dumps({'tool_call_id':cid}))
   (turn/'injection.json').write_text(json.dumps({'tool_name':'Edit','tool_call_id':cid}))
   request=follow/'request.agent.json';request.write_text(json.dumps({'messages':[{'role':'user','content':[{'type':'tool_result','tool_use_id':cid,'is_error':True,'content':'<tool_use_error>File does not exist. Note: your current working directory is /testbed.</tool_use_error>'}]}]}))
   original=request.read_bytes();self.assertTrue(collect(c));self.assertTrue(collect(c));self.assertEqual(request.read_bytes(),original)
   rows=[json.loads(l) for l in (c/'container/simulated-tool-calls.jsonl').read_text().splitlines()]
   self.assertEqual(len(rows),1);self.assertFalse(rows[0]['executed']);self.assertEqual(rows[0]['source'],'claude_preexecution_input_validation')
if __name__=='__main__':unittest.main()
