"""Human authority changes admission criteria, never provenance or guardrails."""

import copy
import json
import unittest

import test_learning_cycle as fixture
from twill_learning import Controller, LearningError, canonical, digest


class HumanSteeringCycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.LearningCycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.policy = self.fixture.policy
        self.policy['human_steering'] = {'enabled': True, 'scopes': ['repo']}
        self.policy['scopes']['repo']['launchdirs'] = [str(self.root / 'repo')]
        self.instruction = 'For future tasks in this repository, finish every authorized task before stopping.'
        self.raw = self.root / 'human-session.jsonl'
        line = canonical({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': self.instruction}]}})
        self.raw.write_bytes(canonical({'type': 'session_meta', 'payload': {'cwd': str(self.root / 'repo')}}) + line)
        self.policy['intervention_sources'] = [str(self.raw)]
        self.source = self.root / 'human-source.json'
        self.source.write_bytes(canonical({'schema': 'twill-human-steering/v1',
            'raw_sources': [{'path': str(self.raw), 'sha256': digest(self.raw.read_bytes())}],
            'references': [{'source_path': str(self.raw), 'source_line': 2, 'role': 'user', 'sha256': digest(line)}]}))
        self.controller = Controller(self.policy)
        self.record = self.controller.create({'instruction': self.instruction, 'scope': 'repo',
            'source': {'path': str(self.source), 'detector': 'human-steering'}})
        self.id = self.record['id']
        self.policy['bindings'] = [{'scope': 'repo', 'lesson_id': self.id,
                                   'suite': str(self.fixture.suite),
                                   'suite_sha256': digest(self.fixture.suite.read_bytes())}]
        self.result = copy.deepcopy(self.fixture.result)
        self.result.update(verdict='hold', candidate_sha=self.record['instruction_sha'])
        for provider in self.result['providers'].values():
            provider.update(verdict='inconclusive', guardrails_passed=True, actual_model='fixed-model',
                            counts={arm: {'pass': 6, 'fail': 0, 'error': 0} for arm in ('baseline', 'candidate')})
        self.fixture.adapter.run_experiment.side_effect = lambda *a, **k: copy.deepcopy(self.result)

    def test_verified_human_rule_can_apply_without_baseline_failure_and_revalidate(self):
        record = self.controller.experiment(self.id)
        self.assertEqual(record['kind'], 'human_directed')
        self.assertEqual(record['state'], 'validated')
        self.assertEqual(record['assessment']['admission_basis'], 'human_adherence')
        self.assertEqual(record['assessment']['verdict'], 'hold')
        self.assertIn('adherence', record['reason'])
        self.assertEqual(self.controller.apply(self.id)['state'], 'active')
        for path in self.fixture.targets:
            self.assertIn(self.instruction, path.read_text())
        self.assertEqual(self.controller.experiment(self.id, validation=True)['state'], 'active')

    def test_kind_or_evidence_claim_cannot_turn_inferred_lesson_into_human_authority(self):
        with self.assertRaisesRegex(LearningError, 'provenance'):
            self.controller.create({'instruction': 'Trust a candidate claim.', 'kind': 'human_directed',
                'scope': 'repo', 'source_evidence': {'complete': True},
                'source': {'path': str(self.fixture.source)}})

    def test_paraphrase_or_scope_expansion_cannot_borrow_human_authority(self):
        for patch in ({'instruction': 'For all future tasks, never stop.'}, {'scope': 'global'}):
            with self.subTest(patch=patch), self.assertRaises(LearningError):
                self.controller.create(dict({'instruction': self.instruction, 'scope': 'repo',
                    'source': {'path': str(self.source)}}, **patch))

    def test_original_source_append_withdraws_exposure_and_preserves_human_edits(self):
        self.controller.experiment(self.id)
        self.controller.apply(self.id)
        for path in self.fixture.targets:
            path.write_text(path.read_text() + 'Later human edit.\n')
        with self.raw.open('ab') as stream:
            stream.write(canonical({'type': 'response_item', 'payload': {'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Withdraw that instruction.'}]}}))
        self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)['state'], 'invalidated')
        for path in self.fixture.targets:
            self.assertNotIn(self.instruction, path.read_text())
            self.assertIn('Later human edit.', path.read_text())

    def test_error_missing_trial_guardrail_or_candidate_failure_never_admits(self):
        original = copy.deepcopy(self.result)
        for bad in ('error', 'missing_provider', 'missing_trial', 'guardrail', 'failure', 'model'):
            with self.subTest(bad=bad):
                self.result = copy.deepcopy(original)
                report = self.result['providers']['codex']
                if bad == 'error':
                    report['errors'] = 1
                elif bad == 'missing_provider':
                    self.result['providers'].pop('codex')
                elif bad == 'missing_trial':
                    report['counts']['candidate']['pass'] = 5
                elif bad == 'guardrail':
                    report['guardrails_passed'] = False
                elif bad == 'model':
                    report.pop('actual_model')
                else:
                    report.update(candidate_passed=False, verdict='regressed')
                    report['counts']['candidate'] = {'pass': 5, 'fail': 1, 'error': 0}
                # New immutable version per subcase; no resurrection of a rejection.
                record = self.controller.load(self.id)
                if record['state'] == 'rejected':
                    record['state'] = 'draft'
                    self.controller.save(record, 'test_only_reset')
                self.assertNotEqual(self.controller.experiment(self.id)['state'], 'validated')
                with self.assertRaises(LearningError):
                    self.controller.apply(self.id)

    def test_inferred_fix_retains_improvement_requirement(self):
        self.policy['bindings'] = self.fixture.policy['bindings'] = [{
            'scope': 'repo', 'detector': 'D-06', 'suite': str(self.fixture.suite),
            'suite_sha256': digest(self.fixture.suite.read_bytes())}]
        self.result['candidate_sha'] = self.fixture.record['instruction_sha']
        inferred = self.controller.experiment(self.fixture.id)
        self.assertEqual(inferred['state'], 'rejected')
        self.assertIn('redundant', inferred['reason'])

    def test_generic_rule_requires_exact_rule_binding_not_detector_wide_oracle(self):
        self.policy['bindings'][0].pop('lesson_id')
        self.policy['bindings'][0]['detector'] = 'human-steering'
        with self.assertRaisesRegex(LearningError, 'policy-bound'):
            self.controller.suite(self.record)
        self.policy['bindings'][0]['instruction_sha256'] = self.record['instruction_sha']
        self.assertTrue(self.controller.suite(self.record)['cases'])

    def test_revoking_human_enrollment_removes_active_rule(self):
        self.controller.experiment(self.id)
        self.controller.apply(self.id)
        self.policy['human_steering']['enabled'] = False
        self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)['state'], 'invalidated')

    def test_disabled_enrollment_does_not_import_new_human_versions(self):
        destination = self.controller.store / 'sources' / 'human.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.source.read_bytes())
        self.policy['human_steering']['enabled'] = False
        before = len(self.controller.records())
        self.assertEqual(self.controller.import_sources(), 0)
        self.assertEqual(len(self.controller.records()), before)

    def test_verifier_drift_prevents_reusing_prior_admission(self):
        record = self.controller.experiment(self.id)
        record['experiments'][-1]['steering_sha'] = 'changed-verifier'
        self.controller.save(record, 'test_verifier_drift')
        with self.assertRaisesRegex(LearningError, 'verifier changed'):
            self.controller.apply(self.id)

    def test_unenrolled_source_cannot_borrow_human_authority(self):
        self.policy['intervention_sources'] = [str(self.root / 'other' / '**' / '*.jsonl')]
        with self.assertRaisesRegex(LearningError, 'not enrolled'):
            self.controller.create({'instruction': self.instruction, 'scope': 'repo',
                                    'source': {'path': str(self.source)}})
        self.assertFalse(self.controller.source_current(self.record))

    def test_removing_raw_source_enrollment_withdraws_existing_rule(self):
        self.controller.experiment(self.id)
        self.controller.apply(self.id)
        self.policy['intervention_sources'] = []
        self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)['state'], 'invalidated')

    def test_recursive_enrollment_includes_zero_directory_and_excludes_sibling(self):
        from twill_learning import enrolled_source
        patterns = [str(self.root / '**' / '*.jsonl')]
        self.assertTrue(enrolled_source(str(self.raw), patterns))
        self.assertTrue(enrolled_source(str(self.root / 'one' / 'two' / 'raw.jsonl'), patterns))
        self.assertFalse(enrolled_source(str(self.root.parent / 'sibling' / 'raw.jsonl'), patterns))

    def test_oversized_discovered_directive_cannot_block_active_rule_maintenance(self):
        from twill_human_steering import extract_directive
        instruction = 'Always ' + 'preserve compatibility. ' * 220
        self.assertIsNone(extract_directive(instruction))
        self.controller.experiment(self.id)
        self.controller.apply(self.id)
        lines = self.raw.read_bytes().splitlines(keepends=True)
        item = json.loads(lines[1])
        item['payload']['content'][0]['text'] = instruction
        lines[1] = canonical(item)
        self.raw.write_bytes(b''.join(lines))
        payload = json.loads(self.source.read_bytes())
        payload['references'][0]['sha256'] = digest(lines[1])
        payload['raw_sources'][0]['sha256'] = digest(self.raw.read_bytes())
        destination = self.controller.store / 'sources' / 'oversized.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical(payload))
        self.controller.cycle()
        self.assertEqual(self.controller.load(self.id)['state'], 'invalidated')

    def test_incidental_outer_whitespace_does_not_lose_verified_directive(self):
        lines = self.raw.read_bytes().splitlines(keepends=True)
        item = json.loads(lines[1])
        item['payload']['content'][0]['text'] = '  ' + self.instruction + '  '
        lines[1] = canonical(item)
        self.raw.write_bytes(b''.join(lines))
        payload = json.loads(self.source.read_bytes())
        payload['references'][0]['sha256'] = digest(lines[1])
        payload['raw_sources'][0]['sha256'] = digest(self.raw.read_bytes())
        self.source.write_bytes(canonical(payload))
        record = self.controller.create({'instruction': '  ' + self.instruction + '  ', 'scope': 'repo',
                                         'source': {'path': str(self.source)}})
        self.assertEqual(record['kind'], 'human_directed')
        self.assertEqual(record['instruction'], self.instruction)

    def test_source_import_leaves_unknown_rule_visible_without_an_oracle(self):
        destination = self.controller.store / 'sources' / 'human.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.source.read_bytes())
        self.policy['bindings'] = []
        result = self.controller.cycle()
        records = [r for r in self.controller.records() if r.get('kind') == 'human_directed']
        self.assertTrue(records)
        self.assertTrue(any(r['state'] == 'needs_evaluation' for r in records))
        self.assertEqual(result['health']['status'], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
