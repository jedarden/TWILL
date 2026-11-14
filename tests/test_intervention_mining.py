"""Synthetic native transcripts exercise broad pass-one evidence and safety."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import twill_intervention_mining as mining
from twill_config import ConfigError


def claude(role, content, **extra):
    if role == "assistant" and isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return {"type": role, "sessionId": "synthetic-claude", "cwd": "/tmp/synthetic-project",
            "message": {"role": role, "content": content}, **extra}


def codex(kind, **payload):
    return {"type": kind, "payload": {"session_id": "synthetic-codex", "cwd": "/tmp/synthetic-project", **payload}}


def message(role, text):
    return codex("response_item", type="message", role=role,
                 content=[{"type": "input_text" if role == "user" else "output_text", "text": text}])


class MiningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.source = self.root / "source.jsonl"
        self.policy = {"scopes": {"synthetic": {"global": True}},
                       "two_pass": {"enabled": True, "scopes": ["synthetic"]},
                       "intervention_sources": [str(self.root / "*.jsonl")],
                       "intervention_settle_seconds": 60}

    def write(self, records, path=None):
        path = path or self.source
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        os.utime(path, (1, 1))
        return path

    def episodes(self):
        return [json.loads(p.read_text()) for p in (self.private / "learning" / "interventions").glob("IE-*.json")]

    def path(self, episode):
        return self.private / "learning" / "interventions" / (episode["id"] + ".json")

    def basic(self):
        return [claude("user", "Check the report"), claude("assistant", "I will stop here."),
                claude("user", "Actually, continue.\nPlease finish the report and include its summary."),
                claude("assistant", "Continuing now.")]

    def test_multiline_correction_native_context_and_receipt(self):
        self.write(self.basic())
        result = mining.discover(self.policy, self.private)
        self.assertEqual(result["discovered"], 2)
        correction = next(e for e in self.episodes() if e["classification"]["category"] == "correction")
        verified = mining.verify_episode(self.path(correction))
        self.assertIn("\n", verified["context"]["intervention"]["text"])
        self.assertEqual(verified["turns"][verified["intervention_index"]]["role"], "user")
        self.assertEqual(verified["outcome"]["status"], "unknown")
        self.assertEqual([(row["origin"], row["text"]) for row in verified["context"]["previous"]],
                         [("human_request", "Check the report"), ("assistant_text", "I will stop here.")])
        receipt = mining.consumed_sources(self.private)[str(self.source)]
        self.assertTrue(receipt["complete"] and receipt["compact_durable"])
        self.assertIn(str(self.source), mining.pinned_sources(self.private))
        self.assertEqual(mining.discover(self.policy, self.private)["discovered"], 0)

    def test_operator_privacy_hold_survives_scanner_and_native_generation_changes(self):
        records = self.basic()
        self.write(records)
        mining.discover(self.policy, self.private)
        held = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        classification = held["classification"]
        original_hash = held["source_original"]["file_sha256"]
        held.update(privacy_hold=True, state="needs_review")
        views = [*held["context"]["previous"], held["context"]["intervention"], *held["context"]["following"]]
        for view in views:
            view.update(text="", truncated=True)
        self.path(held).write_text(json.dumps(held))
        self.assertTrue(mining.verify_episode(self.path(held))["verified"])
        self.write(records + [claude("assistant", "An additional recorded step.")])
        raw = self.source.read_bytes()
        with patch.object(mining, "SCANNER_VERSION", "quarantine-fixture-generation"):
            mining.discover(self.policy, self.private)
            updated = json.loads(self.path(held).read_text())
            self.assertTrue(updated["privacy_hold"])
            self.assertEqual(updated["state"], "needs_review")
            self.assertEqual(updated["classification"], classification)
            self.assertNotEqual(updated["source_original"]["file_sha256"], original_hash)
            self.assertEqual(updated["scanner_version"], "quarantine-fixture-generation")
            verified = mining.verify_episode(self.path(held))
            self.assertTrue(verified["verified"])
            self.assertTrue(all(turn["text"] == "" and turn["truncated"] is True and turn["sha256"]
                                and turn["locator"] and turn["spans"] for turn in verified["turns"]))
            normal = next(e for e in self.episodes() if e["id"] != held["id"])
            self.assertEqual(normal["context"]["intervention"]["text"], "Check the report")
            self.assertNotIn("privacy_hold", normal)
            alias = self.root / "held-alias.jsonl"
            self.write(records + [claude("assistant", "An additional recorded step.")], alias)
            mining.discover(self.policy, self.private)
            updated = json.loads(self.path(held).read_text())
            self.assertTrue(updated["privacy_hold"])
            self.assertTrue(all(view["text"] == "" for view in
                                [*updated["context"]["previous"], updated["context"]["intervention"], *updated["context"]["following"]]))
            # The flag pins originals even if a lifecycle field is changed;
            # only explicit operator review can release the quarantine.
            updated["state"] = "archived"
            self.path(held).write_text(json.dumps(updated))
            self.assertIn(str(self.source), mining.pinned_sources(self.private))
            self.assertIn(str(alias), mining.pinned_sources(self.private))
        self.assertEqual(self.source.read_bytes(), raw)

    def test_privacy_hold_rejects_non_elided_context(self):
        self.write(self.basic())
        mining.discover(self.policy, self.private)
        held = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        held.update(privacy_hold=True, state="needs_review")
        self.path(held).write_text(json.dumps(held))
        with self.assertRaisesRegex(ValueError, "reference-only context"):
            mining.verify_episode(self.path(held))

    def test_codex_ordinary_question_preference_and_task_negatives(self):
        self.write([message("user", "Build a synthetic dashboard"), message("assistant", "Done."),
                    message("user", "How is the report progressing?\nWhat remains?"),
                    message("assistant", "Checking."), message("user", "I prefer concise updates."),
                    message("assistant", "Understood."), message("user", "Create a different project")])
        mining.discover(self.policy, self.private)
        labels = {e["classification"]["category"] for e in self.episodes()}
        self.assertTrue({"task_start", "question", "preference", "ordinary_request"} <= labels)
        for episode in self.episodes():
            self.assertEqual(mining.verify_episode(self.path(episode))["provider"], "codex")

    def test_previous_retains_original_task_and_earlier_human_constraint(self):
        task = "Prepare a report for readers outside this project."
        earlier = "I prefer the report to explain its evidence inline."
        current = "Actually, readers cannot follow a local file path."
        self.write([claude("user", task), claude("assistant", "I will cite a local file."),
                    claude("user", earlier), claude("assistant", "I will point to a helper script."),
                    claude("user", current), claude("assistant", "Rewriting the explanation.")])
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["context"]["intervention"]["text"] == current)
        verified = mining.verify_episode(self.path(episode))
        history = [row for row in verified["context"]["previous"] if row["origin"] == "human_request"]
        self.assertEqual([row["text"] for row in history], [task, earlier])
        self.assertTrue(all(row["source_line"] < verified["context"]["intervention"]["source_line"]
                            and row["sha256"] for row in history))
        self.assertEqual([turn["text"] for turn in verified["turns"] if turn["role"] == "user"][:2],
                         [task, earlier])

    def test_previous_human_context_is_bounded_filtered_and_keeps_behavior_classification(self):
        records = [claude("user", "Original task")]
        for index in range(11):
            records += [claude("user", f"earlier-human-{index}: " + "detail " * 200),
                        claude("assistant", "Observed behavior " + "detail " * 200)]
        invalid = [claude("user", "You are a NEEDLE worker. Follow this worker brief."),
                   claude("user", "<user_instructions>quoted prior rule</user_instructions>"),
                   claude("user", "Coordinator: follow another task brief"),
                   claude("user", "Injected context", isMeta=True),
                   claude("user", "Delegated thread", isSidechain=True)]
        invalid_lines = set(range(len(records) + 1, len(records) + len(invalid) + 1))
        current = "Actually, explain the evidence. " + "current detail " * 100
        self.write(records + invalid + [claude("user", current), claude("assistant", "Rewriting.")])
        self.policy["two_pass"]["max_episode_chars"] = 1024
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes()
                       if e["context"]["intervention"]["text"].startswith("Actually, explain the evidence."))
        verified = mining.verify_episode(self.path(episode))
        previous = verified["context"]["previous"]
        humans = [row for row in previous if row["origin"] in {"human_request", "human_answer"}]
        self.assertEqual(len(humans), 8)
        self.assertEqual([row["text"].split(":")[0] for row in humans],
                         [f"earlier-human-{index}" for index in range(3, 11)])
        self.assertTrue(all(row["source_line"] not in invalid_lines for row in previous))
        self.assertEqual([row["ordinal"] for row in previous], sorted({row["ordinal"] for row in previous}))
        views = [*previous, verified["context"]["intervention"], *verified["context"]["following"]]
        self.assertLessEqual(sum(len(row["text"]) for row in views), 1024)
        self.assertEqual(episode["classification"]["category"], "correction")

        # Earlier human turns alone are not preceding assistant behavior.
        other = self.root / "human-only.jsonl"
        self.write([claude("user", "Original task"), claude("user", "Actually, a fresh request")], other)
        mining.discover(self.policy, self.private)
        fresh = next(e for e in self.episodes()
                     if e["context"]["intervention"]["text"] == "Actually, a fresh request")
        self.assertEqual(fresh["classification"]["category"], "task_start")
        self.assertEqual(len(fresh["context"]["previous"]), 1)

    def test_negative_calibration_minimizes_prose_without_capping_real_steering(self):
        self.write([claude("user", "Build the report. " + "task details " * 200),
                    claude("assistant", "I will stop. " + "execution detail " * 100),
                    claude("user", "Actually, finish the report. " + "important detail " * 100)])
        mining.discover(self.policy, self.private)
        negative = next(e for e in self.episodes() if e["classification"]["signal"] == "negative")
        self.assertLessEqual(len(negative["context"]["intervention"]["text"]), 240)
        self.assertTrue(negative["context"]["intervention"]["truncated"])
        self.assertTrue(all(len(row["text"]) <= 240 for row in negative["context"]["following"]))
        self.assertTrue(mining.verify_episode(self.path(negative))["verified"])
        steering = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        self.assertGreater(len(steering["context"]["intervention"]["text"]), 240)
        self.assertTrue(mining.verify_episode(self.path(steering))["verified"])

    def test_interactive_tool_answers_keep_actual_question_pairing_both_formats(self):
        self.write([claude("user", "Check the report"),
                    claude("assistant", [{"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion",
                                          "input": {"questions": [{"question": "Which audience?"}]}}]),
                    claude("user", [{"type": "tool_result", "tool_use_id": "ask-1", "content": "Customers"}]),
                    claude("assistant", "Using customers.")])
        mining.discover(self.policy, self.private)
        answer = next(e for e in self.episodes() if e["classification"]["category"] == "interactive_answer")
        self.assertIsNotNone(answer["context"]["intervention"]["question_ordinal"])
        mining.verify_episode(self.path(answer))
        other = self.root / "codex.jsonl"
        self.write([message("user", "Check the report"),
                    codex("response_item", type="function_call", name="request_user_input", call_id="ask-c",
                          arguments=json.dumps({"questions": [{"question": "Which audience?"}]})),
                    codex("response_item", type="function_call_output", call_id="ask-c", output="Customers"),
                    message("assistant", "Using customers.")], other)
        mining.discover(self.policy, self.private)
        answer = next(e for e in self.episodes() if e["provider"] == "codex" and e["classification"]["category"] == "interactive_answer")
        self.assertEqual(mining.verify_episode(self.path(answer))["context"]["intervention"]["origin"], "human_answer")

    def test_excludes_harness_quoted_sidechain_and_obvious_worker_prompts(self):
        self.write([claude("assistant", "Working."), claude("user", "Actually use this", isMeta=True),
                    claude("user", "Actually use this", isSidechain=True),
                    claude("user", "<user_instructions>Actually use this</user_instructions>"),
                    claude("user", "You are a NEEDLE worker. Claim bead fixture-123 and continue."),
                    claude("user", "This might mean something else"), claude("assistant", "Checking.")])
        mining.discover(self.policy, self.private)
        episodes = self.episodes()
        self.assertEqual(sum(e["classification"]["signal"] == "negative" for e in episodes), 4)
        self.assertEqual(sum(e["classification"]["signal"] == "unknown" for e in episodes), 1)
        self.assertTrue(all(e["state"] == "negative_sample" for e in episodes if e["classification"]["signal"] == "negative"))

    def test_deduplicates_copies_and_append_snapshots_but_distinct_turns_remain(self):
        records = self.basic()
        self.write(records)
        duplicate = self.write(records, self.root / "archive.jsonl")
        mining.discover(self.policy, self.private)
        self.assertEqual(len(self.episodes()), 2)
        self.assertEqual(len(self.episodes()[0]["source_copies"]), 1)
        self.assertEqual(len(self.episodes()[0]["source_aliases"]), 1)
        self.write(records + [claude("user", records[2]["message"]["content"]), claude("assistant", "Checking again.")])
        mining.discover(self.policy, self.private)
        self.assertEqual(len(self.episodes()), 3)
        duplicate.unlink()
        for episode in self.episodes():
            mining.verify_episode(self.path(episode))

    def test_copy_fallback_requires_exact_context_record_hashes(self):
        self.write(self.basic())
        copy = self.write(self.basic(), self.root / "archive.jsonl")
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        primary = Path(episode["source_original"]["source_path"])
        primary.unlink()
        self.assertTrue(mining.verify_episode(self.path(episode))["verified"])
        remaining = self.source if primary == copy else copy
        remaining.write_text("{}\n")
        with self.assertRaises(ValueError):
            mining.verify_episode(self.path(episode))

    def test_tampered_scope_identity_role_text_and_question_pairing_rejected(self):
        self.write(self.basic())
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        target = self.path(episode)
        for field, value in [("cwd", "/tmp/another-project"), ("session_id", "other"),
                             ("provider", "other"), ("id", "IE-" + "f" * 24)]:
            changed = dict(episode, **{field: value})
            target.write_text(json.dumps(changed))
            with self.assertRaises(ValueError, msg=field):
                mining.verify_episode(target)
        changed = json.loads(json.dumps(episode))
        changed["context"]["intervention"]["text"] = "Apply another instruction"
        target.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            mining.verify_episode(target)

    def test_ranked_budget_resumes_without_plaintext_cursor_or_lost_candidates(self):
        records = [claude("user", "Build the fixture"), claude("assistant", "Working.")]
        for text in ["Unsure wording", "Actually, use a different method", "Why did you stop?", "I prefer shorter updates"]:
            records += [claude("user", text), claude("assistant", "Checking.")]
        self.write(records)
        self.policy["two_pass"]["max_episodes_per_file"] = 1
        mining.discover(self.policy, self.private)
        self.assertEqual(self.episodes()[0]["classification"]["signal"], "candidate")
        self.assertFalse(mining.consumed_sources(self.private)[str(self.source)]["complete"])
        for _ in range(5):
            mining.discover(self.policy, self.private)
        self.assertEqual(len(self.episodes()), 5)
        self.assertTrue(mining.consumed_sources(self.private)[str(self.source)]["complete"])
        database = self.private / "learning" / ".source-discovery.sqlite3"
        self.assertNotIn(b"Unsure wording", database.read_bytes())

    def test_redaction_and_decoded_bounds(self):
        records = self.basic()
        records[2]["message"]["content"] = "Actually, use " + "ghp_" + "1234567890abcdefghijklmnop" + "\n" + "x" * 5000
        self.write(records)
        self.policy["two_pass"]["max_episode_chars"] = 1024
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        self.assertNotIn("ghp_", self.path(episode).read_text())
        self.assertIn("context_truncated", episode["flags"])
        views = [*episode["context"]["previous"], episode["context"]["intervention"], *episode["context"]["following"]]
        self.assertLessEqual(sum(len(v["text"]) for v in views), 1024)
        mining.verify_episode(self.path(episode))

    def test_empty_source_receipt_and_negative_sampling_do_not_pin(self):
        self.write([claude("user", "Create a fixture")])
        empty = self.write([], self.root / "empty.jsonl")
        mining.discover(self.policy, self.private)
        self.assertTrue(mining.consumed_sources(self.private)[str(empty)]["complete"])
        self.assertEqual(mining.pinned_sources(self.private), set())

    def test_changed_source_during_parse_not_consumed(self):
        self.write(self.basic())
        original = mining._fingerprint
        calls = 0
        def changed(path):
            nonlocal calls
            calls += 1
            value = original(path)
            return [value[0] + 1, *value[1:]] if calls == 2 else value
        with patch.object(mining, "_fingerprint", side_effect=changed):
            result = mining.discover(self.policy, self.private)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.episodes(), [])
        self.assertNotIn(str(self.source), mining.consumed_sources(self.private))

    def test_parser_caps_not_marked_fully_consumed(self):
        self.write(self.basic())
        with patch.object(mining, "MAX_PARSE_INTERACTIONS", 2):
            result = mining.discover(self.policy, self.private)
        self.assertEqual(result["metrics"]["scan_reasons"], {"parser_interaction_limit": 1})
        self.assertFalse(mining.consumed_sources(self.private)[str(self.source)]["complete"])
        mining.discover(self.policy, self.private)
        self.assertTrue(mining.consumed_sources(self.private)[str(self.source)]["complete"])

    def test_sidecar_generation_change_requeues_identical_native_bytes(self):
        self.write(self.basic())
        sidecar = self.source.with_suffix(".manifest.json")
        sidecar.write_text(json.dumps({"source_version": "a" * 64}))
        mining.discover(self.policy, self.private)
        sidecar.write_text(json.dumps({"source_version": "b" * 64}))
        result = mining.discover(self.policy, self.private)
        self.assertEqual(result["deduplicated"], 2)
        self.assertEqual(mining.consumed_sources(self.private)[str(self.source)]["source_version"], "b" * 64)

    def test_terminal_nonlesson_releases_pin_but_changed_evidence_requeues(self):
        self.write(self.basic())
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["classification"]["signal"] == "candidate")
        episode.update(state="interpreted", interpretation_disposition="task_only", interpretation_id=episode["id"])
        self.path(episode).write_text(json.dumps(episode))
        self.assertEqual(mining.pinned_sources(self.private), set())
        self.write(self.basic() + [claude("assistant", "An additional check finished.")])
        mining.discover(self.policy, self.private)
        revised = json.loads(self.path(episode).read_text())
        self.assertEqual(revised["state"], "pending_interpretation")
        self.assertTrue(revised["interpretation_stale"])
        self.assertIn(str(self.source), mining.pinned_sources(self.private))

    def test_negative_sampling_counts_all_excluded_worker_records(self):
        self.write([claude("user", f"You are a NEEDLE worker. Claim bead fixture-{n} and continue.") for n in range(12)])
        result = mining.discover(self.policy, self.private)
        self.assertEqual(len(self.episodes()), 2)
        self.assertEqual(sum(result["metrics"]["sampled_negative_reasons"].values()), 12)

    def test_realistic_human_worker_topic_is_retained_but_harness_wrappers_excluded(self):
        self.write([claude("assistant", "Working."),
                    claude("user", "Why are the NEEDLE workers not making progress?"),
                    claude("assistant", "Checking."),
                    claude("user", "<task-notification>Worker finished</task-notification>"),
                    claude("user", "<subagent_notification>Worker finished</subagent_notification>"),
                    claude("user", "<hook_prompt type='review'>Check output</hook_prompt>"),
                    claude("user", "Coordinator: continue the assigned work."),
                    claude("user", "This session is being continued from a previous conversation. Summary: ...")])
        mining.discover(self.policy, self.private)
        episodes = self.episodes()
        human = next(e for e in episodes if "Why are" in e["context"]["intervention"]["text"])
        self.assertEqual(human["classification"]["signal"], "candidate")
        coordinator = next(e for e in episodes if "Coordinator:" in e["context"]["intervention"]["text"])
        self.assertEqual(coordinator["classification"]["flags"], ["uncertain_origin"])
        self.assertEqual(sum(e["classification"]["signal"] == "negative" for e in episodes), 3)

    def test_miner_upgrade_invalidates_receipts_and_reprocesses_completed_files(self):
        self.write(self.basic())
        mining.discover(self.policy, self.private)
        with patch.object(mining, "SCANNER_VERSION", "next-test-version"):
            self.assertFalse(mining.consumed_sources(self.private)[str(self.source)]["complete"])
            result = mining.discover(self.policy, self.private)
            self.assertEqual(result["deduplicated"], 2)
            receipt = mining.consumed_sources(self.private)[str(self.source)]
            self.assertTrue(receipt["complete"])
            self.assertEqual(receipt["scanner_version"], "next-test-version")

    def test_malformed_or_unsupported_records_never_receive_consumed_receipt(self):
        self.write(self.basic())
        with self.source.open("a") as stream:
            stream.write("malformed-json\n")
        os.utime(self.source, (1, 1))
        unknown = self.write([{"unrecognized": "native record"}], self.root / "unknown.jsonl")
        mining.discover(self.policy, self.private)
        receipts = mining.consumed_sources(self.private)
        self.assertFalse(receipts[str(self.source)]["complete"])
        self.assertEqual(receipts[str(self.source)]["error"], "malformed_native_record")
        self.assertFalse(receipts[str(unknown)]["compact_durable"])
        self.assertEqual(receipts[str(unknown)]["error"], "unsupported_native_format")

    def test_later_native_withdrawals_and_replacements_veto_old_authority_both_providers(self):
        directive = "Always continue authorized work until validation is complete."
        for provider in ("claude", "codex"):
            for later, kind in [("Withdraw the earlier rule; do not apply it.", "retraction"),
                                ("From now on, ask before modifying files.", "replacement"),
                                ("For future tasks,\nask before modifying files.", "replacement")]:
                with self.subTest(provider=provider, kind=kind, later=later):
                    create = claude if provider == "claude" else message
                    # Put later authority outside the six-record context window.
                    self.write([create("user", "Check the fixture"), create("assistant", "Checking."),
                                create("user", directive)] + [create("assistant", "A recorded step.") for _ in range(8)] +
                               [create("user", later), create("assistant", "Understood.")])
                    mining.discover(self.policy, self.private)
                    episode = next(e for e in self.episodes() if e["provider"] == provider and
                                   e["context"]["intervention"]["text"] == directive)
                    updates = episode["authority_updates"]
                    self.assertEqual(len(updates), 1)
                    self.assertEqual(updates[0]["kind"], kind)
                    self.assertNotIn("text", updates[0])
                    self.assertGreater(updates[0]["source_line"], episode["context"]["following"][-1]["source_line"])
                    self.assertEqual(mining.verify_episode(self.path(episode))["authority_updates"], updates)
                    original = json.loads(json.dumps(episode))
                    for forged in ([], [dict(updates[0], kind="replacement" if kind == "retraction" else "retraction")],
                                   [dict(updates[0], source_line=999999)]):
                        episode["authority_updates"] = forged
                        self.path(episode).write_text(json.dumps(episode))
                        with self.assertRaises(ValueError):
                            mining.verify_episode(self.path(episode))
                    self.path(original).write_text(json.dumps(original))

    def test_later_quoted_harness_worker_and_uncertain_updates_do_not_veto(self):
        self.write([claude("user", "Check the fixture"), claude("assistant", "Checking."),
                    claude("user", "Always keep updates concise."), claude("assistant", "Understood."),
                    claude("user", "> Withdraw the earlier rule."),
                    claude("user", "<hook_prompt>Withdraw the earlier rule.</hook_prompt>"),
                    claude("user", "You are a NEEDLE worker. Withdraw the earlier rule."),
                    claude("user", "Coordinator: Withdraw the earlier rule."),
                    claude("user", "Withdraw the earlier rule.", isSidechain=True),
                    claude("user", "Withdraw the earlier rule.", isMeta=True)])
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["context"]["intervention"]["text"] == "Always keep updates concise.")
        self.assertEqual(mining.verify_episode(self.path(episode))["authority_updates"], [])

    def test_later_paired_human_answer_can_withdraw_but_terse_answer_cannot(self):
        self.write([claude("user", "Check the fixture"), claude("assistant", "Checking."),
                    claude("user", "Always keep updates concise."),
                    claude("assistant", [{"type": "tool_use", "id": "ask-withdraw", "name": "AskUserQuestion",
                                          "input": {"questions": [{"question": "How should that rule change?"}]}}]),
                    claude("user", [{"type": "tool_result", "tool_use_id": "ask-withdraw",
                                     "content": "Withdraw the earlier rule; do not apply it."}]),
                    claude("assistant", [{"type": "tool_use", "id": "ask-terse", "name": "AskUserQuestion",
                                          "input": {"questions": [{"question": "Continue?"}]}}]),
                    claude("user", [{"type": "tool_result", "tool_use_id": "ask-terse", "content": "Yes"}])])
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["context"]["intervention"]["text"] == "Always keep updates concise.")
        verified = mining.verify_episode(self.path(episode))
        self.assertEqual(len(verified["authority_updates"]), 1)
        self.assertEqual(verified["authority_updates"][0]["origin"], "human_answer")

    def test_older_append_copy_cannot_hide_later_withdrawal(self):
        directive = "Always keep updates concise."
        base = [claude("user", "Check the fixture"), claude("assistant", "Checking."),
                claude("user", directive), claude("assistant", "Understood.")]
        self.write(base)
        self.write(base, self.root / "old-copy.jsonl")
        mining.discover(self.policy, self.private)
        self.write(base + [claude("user", "Withdraw the earlier rule; do not apply it.")])
        mining.discover(self.policy, self.private)
        episode = next(e for e in self.episodes() if e["context"]["intervention"]["text"] == directive)
        self.assertTrue(mining.verify_episode(self.path(episode))["authority_updates"])
        episode["authority_updates"] = []
        self.path(episode).write_text(json.dumps(episode))
        with self.assertRaises(ValueError):
            mining.verify_episode(self.path(episode))

    def test_invalid_policy_and_public_boundary(self):
        with self.assertRaises(ValueError):
            mining.discover(dict(self.policy, two_pass={"enabled": True, "scopes": []}), self.private)
        with self.assertRaises(ConfigError):
            mining.discover(self.policy, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    unittest.main()
