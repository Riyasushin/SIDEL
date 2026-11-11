from __future__ import annotations
import json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from scripts.run_simulated_injection_matrix import AdoptRunner,admission_allowed
from swebench_container.models import ContainerRunSpec

class ControllerHandoverTest(unittest.TestCase):
 def test_available_memory_admits_flash_without_reserving_all_running_caps(self):
  gib=1024**3
  self.assertTrue(admission_allowed(340*gib,7,48))
  self.assertFalse(admission_allowed(79*gib,7,48))
  self.assertFalse(admission_allowed(340*gib,90,48))
  self.assertFalse(admission_allowed(340*gib,7,64))
  self.assertFalse(admission_allowed(90*gib,7,48,16*gib))
 def spec(self,path):
  return ContainerRunSpec(run_id='run',context_id='ctx',instance_id='task',agent='claude',agent_mode='bypassPermissions',router_url='http://router',router_api_key='rk',artifact_dir=path,swebench_image='image',wrapper_image='wrapper',issue_prompt='Fix the issue')
 def test_adoption_waits_for_existing_container_without_starting_or_truncating(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);original=b'{"type":"result","subtype":"success","num_turns":3}\n';(p/'stdout.log').write_bytes(original);(p/'stderr.log').write_bytes(b'')
   with patch('scripts.run_simulated_injection_matrix.subprocess.run',side_effect=[subprocess.CompletedProcess([],0,'exited\n',''),subprocess.CompletedProcess([],0,'0\n','')]) as run,patch('scripts.run_simulated_injection_matrix.container_runner.inspect_container'),patch('scripts.run_simulated_injection_matrix.container_runner.remove_container') as remove,patch('scripts.run_simulated_injection_matrix.time.sleep'):
    result=AdoptRunner().run(self.spec(p))
   self.assertEqual(result.status,'completed');self.assertEqual((p/'stdout.log').read_bytes(),original)
   self.assertEqual([call.args[0][1] for call in run.call_args_list],['inspect','wait']);remove.assert_called_once()
 def test_adoption_recovers_a_completed_container_already_removed_by_old_controller(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'stdout.log').write_text('{"type":"result","subtype":"success"}\n');(p/'stderr.log').write_text('');(p/'exit.json').write_text(json.dumps({'exit_code':0,'status':'completed'}))
   with patch('scripts.run_simulated_injection_matrix.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','No such object')) as run:
    result=AdoptRunner().run(self.spec(p))
   self.assertEqual(result.status,'completed');self.assertEqual(run.call_count,1)
if __name__=='__main__':unittest.main()
