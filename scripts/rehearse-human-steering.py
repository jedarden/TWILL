#!/usr/bin/env python3
"""Real native-provider acceptance for synthetic human-steering admission.

No production target is changed. A passing baseline is intentional: explicit
human guidance is validated for adherence rather than inferred improvement.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twill_learning import Controller, POLICY_SCHEMA, canonical  # noqa: E402


RULE = ('For all future tasks, After answering a status question, continue the authorized task '
        'until complete unless I stop it or required approval is missing.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--claude-model', required=True)
    parser.add_argument('--codex-model', required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='human-steering-rehearsal-', dir=args.scratch)).resolve()
    targets = [root / name for name in ('AGENTS.md', 'CLAUDE.md')]
    for target in targets:
        target.write_text('Continue authorized work after status exchanges. Honor cancellation and reserved approvals.\n')
    raw = root / 'synthetic-native.jsonl'
    raw.write_bytes(canonical({'type': 'session_meta', 'payload': {'cwd': str(root)}}) +
                    canonical({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                              'content': [{'type': 'input_text', 'text': RULE}]}}))
    os.utime(raw, (time.time() - 8000, time.time() - 8000))
    policy = {'schema': POLICY_SCHEMA, 'enabled': True, 'artifacts_root': str(root / 'private'),
              'work_root': str(root / 'work'), 'providers': {
                  name: {'model': model, 'executable': shutil.which(name) or name}
                  for name, model in [('claude', args.claude_model), ('codex', args.codex_model)]},
              'scopes': {'fixture': {'global': True, 'auto_apply': True, 'local_only': True,
                                     'targets': [str(p) for p in targets]}},
              'human_steering': {'enabled': True, 'scopes': ['fixture']},
              'intervention_sources': [str(raw)], 'bindings': [], 'limits': {'max_calls_per_cycle': 36}}
    (root / 'policy.json').write_bytes(canonical(policy))
    controller = Controller(policy)
    cycle = controller.cycle()
    records = controller.records()
    assert len(records) == 1, 'automatic discovery must yield exactly one lesson'
    record = records[0]
    assert record['state'] == 'active', record.get('reason')
    assert record['assessment']['admission_basis'] == 'human_adherence'
    assert record['assessment']['verdict'] == 'hold', 'proof must not depend on an improvement'
    for report in record['assessment']['providers'].values():
        assert report['counts']['baseline'] == {'pass': 8, 'fail': 0, 'error': 0}
        assert report['counts']['candidate'] == {'pass': 8, 'fail': 0, 'error': 0}
    assert cycle['imported'] == 1 and cycle['experimented'] == 1
    for target in targets:
        assert RULE in target.read_text()
    record = controller.experiment(record['id'], validation=True)
    assert record['state'] == 'active', 'fresh adherence validation must retain the rule'
    for target in targets:
        target.write_text(target.read_text() + 'Later human edit.\n')
    with raw.open('ab') as stream:
        stream.write(canonical({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': 'Withdraw that instruction.'}]}}))
    # Force settlement so this also tests rescan/re-extraction, not only the
    # temporary protection of the source's settlement window.
    os.utime(raw, (time.time() - 8000, time.time() - 8000))
    controller.cycle()
    assert controller.load(record['id'])['state'] == 'invalidated'
    assert len(controller.records()) == 1, 'withdrawn steering must not be rediscovered as a new version'
    for target in targets:
        assert RULE not in target.read_text()
        assert 'Later human edit.' in target.read_text()
    controller.cycle()
    assert controller.load(record['id'])['state'] == 'invalidated'
    result = {'kind': 'synthetic-human-steering-native-proof', 'root': str(root), 'id': record['id'],
              'result': 'passed', 'production_improvement_proven': False,
              'checks': ['automatic_discovery', 'verified_human_provenance', 'both_baselines_pass',
                         'both_candidates_pass', 'applied_without_improvement', 'fresh_revalidation',
                         'source_drift_removal', 'human_edits_preserved', 'no_stale_reapply']}
    (root / 'result.json').write_bytes(canonical(result))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
