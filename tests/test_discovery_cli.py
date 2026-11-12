"""Input backfill is observable and does not spend native inference quota."""

import json
import unittest
from unittest import mock

import test_learning_cycle as fixture
from twill_learning import Controller, LearningError, canonical, stamp


class DiscoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.LearningCycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.policy = self.fixture.policy
        self.controller = self.fixture.controller
        self.status = self.fixture.root / 'feed-status.json'

    def enroll_feed(self, status=None):
        self.policy['archive_feed'] = {'enabled': True, 'status_path': str(self.status), 'stale_seconds': 60}
        if status is not None:
            self.status.write_text(json.dumps(status))
        self.controller = Controller(self.policy)

    def test_discover_imports_and_reports_coverage_without_experimenting(self):
        result = self.controller.discover()
        self.assertEqual(result['experimented'], 0)
        self.assertIn('discovery', result['input_coverage'])
        self.fixture.adapter.run_experiment.assert_not_called()
        self.assertEqual(self.controller.load(self.fixture.id)['state'], 'draft')
        self.assertEqual(json.loads((self.controller.store / 'discovery-status.json').read_text())['experimented'], 0)

    def test_cli_discovery_is_a_distinct_no_model_command(self):
        from twill_learning import main
        path = self.fixture.root / 'policy.json'
        path.write_bytes(canonical(self.policy))
        with mock.patch('twill_learning.Controller.publish', return_value={'published': False}), mock.patch('sys.stdout'):
            self.assertEqual(main(['--policy', str(path), 'discover']), 4)  # learning cycle has not started
        self.fixture.adapter.run_experiment.assert_not_called()

    def test_optional_archive_absence_does_not_break_default_local_inputs(self):
        health = self.controller.health({'at': stamp()})
        self.assertNotIn('archive_feed_unavailable_or_stale', {f['kind'] for f in health['findings']})
        self.assertNotIn('archive_feed', self.controller.input_coverage())

    def test_explicit_archive_feed_missing_stale_or_failed_reports_attention(self):
        for status in (None, {'status': 'healthy', 'at': '2000-01-01T00:00:00+00:00'},
                       {'status': 'attention', 'at': stamp(), 'errors': {'RemoteReadFailure': 1}}):
            with self.subTest(status=status):
                self.status.unlink(missing_ok=True)
                self.enroll_feed(status)
                health = self.controller.health({'at': stamp()})
                self.assertIn('archive_feed_unavailable_or_stale', {f['kind'] for f in health['findings']})
                self.assertEqual(health['status'], 'needs_attention')

    def test_fresh_bounded_backfill_is_visible_without_claiming_complete_coverage(self):
        status = {'status': 'in_progress', 'at': stamp(), 'errors': {}, 'coverage_complete': False,
                  'refs': 1000, 'exported': 2}
        self.enroll_feed(status)
        self.assertEqual(self.controller.input_coverage()['archive_feed']['coverage_complete'], False)
        health = self.controller.health({'at': stamp()})
        self.assertEqual(health['status'], 'healthy')

    def test_archive_feed_policy_requires_exact_path_and_valid_freshness(self):
        for entry in ({'enabled': True}, {'enabled': True, 'status_path': str(self.status), 'stale_seconds': 0},
                      {'enabled': 'yes'}, {'enabled': False, 'arbitrary_authority': True}):
            with self.subTest(entry=entry), self.assertRaises(LearningError):
                Controller(dict(self.policy, archive_feed=entry))

    def test_exhausted_archive_cache_reports_attention_instead_of_healthy_progress(self):
        self.enroll_feed({'status': 'in_progress', 'at': stamp(), 'errors': {}, 'cache_limit': True})
        health = self.controller.health({'at': stamp()})
        self.assertEqual(health['status'], 'needs_attention')
        self.assertIn('archive_feed_unavailable_or_stale', {f['kind'] for f in health['findings']})


if __name__ == '__main__':
    unittest.main()
