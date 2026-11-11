from __future__ import annotations
import json,tempfile,unittest
from pathlib import Path
from scripts.run_simulated_injection_matrix import pairing_order,report_path,summarize

class InjectionMatrixTest(unittest.TestCase):
    def inputs(self):
        tasks=[{'instance_id':f'task{i}'} for i in range(11)]
        samples=[{'id':f'sample{i}','tool_name':['Write','Edit','Bash'][i%3]} for i in range(80)]
        return tasks,samples

    def test_pairing_has_every_sample_task_once_and_same_order_for_both_models(self):
        tasks,samples=self.inputs();first,canaries=pairing_order(tasks,samples,20261109);second,_=pairing_order(tasks,samples,20261109)
        self.assertEqual(first,second);self.assertEqual(len(first),880);self.assertEqual(len(set(first)),880)
        self.assertEqual(first[:3],canaries)
        tools={s['id']:s['tool_name'] for s in samples}
        self.assertEqual([tools[p[0]] for p in canaries],['Write','Edit','Bash'])
        self.assertEqual(set(first),{(s['id'],t['instance_id']) for s in samples for t in tasks})

    def test_ungraded_results_never_publish_a_formal_success_rate(self):
        _,samples=self.inputs()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows={'a':{'context_id':'a','model':'deepseek-v4-pro','dataset_sample_id':'sample0','instance_id':'task0','attack_tool':'Write','valid':True,'status':'completed','interaction_turns':20}}
            summary=summarize(root,rows,samples,'running')
            self.assertIsNone(summary['success_rate']);self.assertFalse(summary['success_rate_is_final']);self.assertEqual(summary['mean_interaction_turns'],20)

    def test_valid_turn_average_and_success_counts_are_separate_from_infrastructure_failures(self):
        _,samples=self.inputs()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rows={}
            for cid,valid,turns in [('good',True,20),('bad',False,0)]:
                rows[cid]={'context_id':cid,'model':'deepseek-v4-pro','dataset_sample_id':'sample0','instance_id':cid,'attack_tool':'Write','valid':valid,'status':'completed' if valid else 'agent_crashed','interaction_turns':turns}
            report=report_path(root,'deepseek-v4-pro',1,'good');report.parent.mkdir(parents=True);report.write_text(json.dumps({'good':{'resolved':True}}))
            summary=summarize(root,rows,samples,'grading')
            self.assertEqual(summary['resolved_runs'],1);self.assertEqual(summary['graded_runs'],1)
            self.assertEqual(summary['mean_interaction_turns'],20);self.assertEqual(summary['mean_interaction_turns_finished'],10)
            self.assertEqual(summary['infrastructure_or_protocol_failures'],1);self.assertIsNone(summary['success_rate'])

    def test_final_denominator_is_880_including_deterministic_empty_patch_failures(self):
        tasks,samples=self.inputs()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rows={}
            for i,(sample,task) in enumerate((s,t) for s in samples for t in tasks):
                rows[str(i)]={'context_id':str(i),'model':'deepseek-flash','dataset_sample_id':sample['id'],'instance_id':task['instance_id'],'attack_tool':sample['tool_name'],'valid':True,'status':'completed','interaction_turns':21}
            summary=summarize(root,rows,samples,'complete')
            self.assertTrue(summary['success_rate_is_final']);self.assertEqual(summary['success_rate'],0);self.assertEqual(summary['empty_patch_runs'],880);self.assertEqual(summary['mean_interaction_turns'],21)

if __name__=='__main__':unittest.main()
