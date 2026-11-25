"""Exercise declared improvement trials against frozen benchmark evidence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import workflow_health  # noqa: E402

PROJECTS = ("fusion-lean", "fusion-lean-wiki", "fusion-lean-skills")
AUDIT_IDS = ["density.si", "gyro.pilot", "gyro.transfer"]
CONTROL_IDS = ["control.density.1", "control.density.2", "control.gyro.wrong-radius"]


def sha(char):
    return char * 64


def head(char):
    return char * 40


def run(mode, candidate, suite, ids, failing=()):
    checks = [{"id": i, "status": "FAIL" if i in failing else "PASS"} for i in ids]
    summary = {}
    for check in checks:
        summary[check["status"]] = summary.get(check["status"], 0) + 1
    return {"mode": mode, "target": {"mode": "commit", "head": candidate},
            "suite_sha256": suite, "checks": checks, "summary": summary}


class HealthTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.repo = self.root / "fusion-lean-wiki"
        for name in PROJECTS:
            repo = self.root / name
            repo.mkdir()
            self.git(repo, "init", "-q")
            (repo / "README.md").write_text("fixture\n")
            self.git(repo, "add", "README.md")
            self.git(repo, "commit", "-qm", "fixture")

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=test",
                               "-c", "user.email=test@local", *args],
                              check=True, capture_output=True, text=True).stdout

    def put(self, rel, data):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def commit(self):
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-qm", "fixture", "--allow-empty")
        return self.git(self.repo, "rev-parse", "HEAD").strip()

    def recovered(self, name, candidate, before_suite, after_suite, failing=("density.si",)):
        self.put(f"evidence/rounds/{name}/round.json", {})
        self.put(f"evidence/rounds/{name}/round-before.json",
                 run("audit", candidate, before_suite, AUDIT_IDS, failing))
        self.put(f"evidence/rounds/{name}/round-after.json",
                 run("audit", candidate, after_suite, AUDIT_IDS))
        self.put(f"evidence/rounds/{name}/controls.json",
                 run("controls", candidate, after_suite, CONTROL_IDS))

    def observation(self, name, candidate, suite, failing=(), audit_ids=None, control_ids=None,
                    negative=("density.si",)):
        audit_ids = audit_ids if audit_ids is not None else AUDIT_IDS
        control_ids = control_ids if control_ids is not None else CONTROL_IDS
        self.put(f"evidence/rounds/{name}/round.json", {})
        self.put(f"evidence/rounds/{name}/audit.json",
                 run("audit", candidate, suite, audit_ids, failing))
        self.put(f"evidence/rounds/{name}/controls.json",
                 run("controls", candidate, suite, control_ids))
        self.put(f"evidence/rounds/{name}/negative.json",
                 run("audit", candidate, suite, audit_ids, negative))

    def trial(self, name="trial", **overrides):
        data = {"schema": "fusion-improvement-trial/v1", "mechanism": "demo",
                "addresses": ["FAIL/density.si"], "verdict": "promoted",
                "conditions": {"same_checks": True},
                "trial_folder": "evidence/rounds/trialrun",
                "promotion_record": "evidence/rounds/trialrun/round.json",
                "negative_control": "evidence/rounds/trialrun/negative.json"}
        data.update(overrides)
        self.put(f"evidence/workflow-trials/{name}/trial.json", data)

    def bench(self):
        return workflow_health.benchmark_summary(self.repo, self.commit())

    def report(self):
        self.commit()
        return workflow_health.report([], [], self.root, PROJECTS, None)

    def review_task(self, tid, **overrides):
        data = dict(id=tid, goal="pilot", state="done", role="editor", needs=[],
                    independent_of=[], evidence=[], owner=None, rev=1, note="",
                    waiting_for=[], resource_holders=[])
        data.update(overrides)
        return data

    def queue_event(self, seq, task, action, actor="worker", owner=None, data=None):
        return {"seq": seq, "time": "2026-11-22T00:00:00.000Z", "task": task,
                "rev": seq, "action": action, "actor": actor, "previous_owner": None,
                "owner": owner, "note": "", "data": data or {}}

    def registry(self, *reviews, identity=None, unknowns=None):
        data = {"schema": "fusion-review-registry/v1", "reviews": list(reviews)}
        if identity is not None:
            data["identity_ambiguities"] = identity
        if unknowns is not None:
            data["unknowns"] = unknowns
        return data

    def review_report(self, tasks, events, registry=None):
        if registry is not None:
            self.put("evidence/workflow-trials/review-evidence/reviews.json", registry)
        self.commit()
        return workflow_health.report(tasks, events, self.root, PROJECTS, None)

    def anchor(self, name):
        (self.repo / name).write_text(name)
        return self.commit()

    def packet(self, bench, name="base"):
        return next(r for r in bench["rounds"] if r["folder"] == f"evidence/rounds/{name}")

    def test_no_declaration_preserves_the_old_shape(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        bench = self.bench()
        packet = self.packet(bench)
        self.assertEqual(bench["improvement_trials"], [])
        self.assertEqual(bench["same_candidate_recoveries"], 1)
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 0)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 0)
        self.assertNotIn("superseded_by_promotion", packet)
        self.assertIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_promoted_trial_clears_the_flag(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial()
        bench = self.bench()
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 0)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 1)
        self.assertEqual(self.packet(bench)["superseded_by_promotion"],
                         ["evidence/rounds/trialrun"])
        self.assertEqual(bench["improvement_trials"][0]["status"], "accepted")
        self.assertNotIn("cross_candidate_replay_needed", self.report()["attention"])

    def assert_kept(self, bench):
        self.assertEqual(bench["same_candidate_recoveries"], 1)
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 0)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 0)
        self.assertNotIn("superseded_by_promotion", self.packet(bench))
        self.assertIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_other_issue_class_does_not_cover(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial(addresses=["FAIL/gyro.transfer"])
        self.assert_kept(self.bench())

    def test_same_candidate_does_not_cover(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("a"), sha("3"))
        self.trial()
        self.assert_kept(self.bench())

    def test_same_evaluator_is_redundant_with_the_existing_pairing(self):
        # A clean different-candidate run with the same digest is already the
        # original cross-candidate replay; declaring it adds nothing and must
        # not be needed for the flag to clear.
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("2"))
        bench = self.bench()
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 1)
        self.assertEqual(self.packet(bench)["other_passing_candidates_with_same_evaluator"],
                         ["evidence/rounds/trialrun"])
        self.assertNotIn("declared_improvement_trials", self.packet(bench))

    def test_smaller_audit_set_does_not_cover(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"), audit_ids=AUDIT_IDS[:2])
        self.trial()
        self.assert_kept(self.bench())

    def test_smaller_controls_set_does_not_cover(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"), control_ids=CONTROL_IDS[:2])
        self.trial()
        self.assert_kept(self.bench())

    def test_larger_check_set_still_covers(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"),
                         audit_ids=AUDIT_IDS + ["extra.check"],
                         control_ids=CONTROL_IDS + ["control.extra"])
        self.trial()
        bench = self.bench()
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 1)
        self.assertEqual(self.packet(bench)["superseded_by_promotion"],
                         ["evidence/rounds/trialrun"])

    def test_missing_promotion_record_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial(promotion_record="evidence/rounds/trialrun/absent.json")
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_negative_control_on_another_check_does_not_cover(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"), negative=("gyro.pilot",))
        self.trial()
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "accepted")

    def test_missing_negative_control_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial(negative_control="evidence/rounds/trialrun/absent.json")
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_negative_control_on_another_candidate_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.put("evidence/rounds/trialrun/negative-other.json",
                 run("audit", head("d"), sha("3"), AUDIT_IDS, ("density.si",)))
        self.trial(negative_control="evidence/rounds/trialrun/negative-other.json")
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_negative_control_without_failure_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"), negative=())
        self.trial()
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_trial_with_failed_check_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"), failing=("gyro.pilot",))
        self.trial()
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")
        self.assertIn("non-PASS", bench["improvement_trials"][0]["reason"])

    def test_false_condition_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial(conditions={"same_checks": True, "unlisted_imports_built": False})
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_unpromoted_verdict_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial(verdict="candidate")
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_missing_trial_folder_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.trial(trial_folder="evidence/rounds/absent")
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_unknown_schema_is_rejected(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial(schema="fusion-improvement-trial/v0")
        bench = self.bench()
        self.assert_kept(bench)
        self.assertEqual(bench["improvement_trials"][0]["status"], "rejected")

    def test_second_unaddressed_recovery_keeps_the_flag(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.recovered("other", head("b"), sha("4"), sha("5"), failing=("gyro.transfer",))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial()
        bench = self.bench()
        self.assertEqual(bench["same_candidate_recoveries"], 2)
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 0)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 1)
        self.assertIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_historical_flags_are_untouched(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("bad-one", head("d"), sha("4"), failing=("gyro.pilot",), negative=())
        self.observation("bad-two", head("e"), sha("5"), failing=("gyro.pilot",), negative=())
        self.observation("trialrun", head("c"), sha("3"))
        self.trial()
        attention = self.report()["attention"]
        self.assertNotIn("cross_candidate_replay_needed", attention)
        self.assertNotIn("missing_or_incomparable_benchmark_evidence", attention)
        self.assertIn("recurrent_benchmark_failure", attention)
        self.assertIn("inspect_recorded_failed_or_unresolved_checks", attention)


    def test_two_kinds_on_one_row_do_not_cover_an_uncovered_row(self):
        # Equal totals (2, 1, 1) as the split case below, but the second row has
        # no follow-up evidence at all, so it must still be named and flagged.
        self.recovered("both", head("a"), sha("1"), sha("2"))
        self.observation("same-digest", head("b"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial()
        self.recovered("uncovered", head("d"), sha("5"), sha("6"), failing=("gyro.transfer",))
        bench = self.bench()
        self.assertEqual(bench["same_candidate_recoveries"], 2)
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 1)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 1)
        self.assertEqual(bench["recoveries_needing_followup"], ["evidence/rounds/uncovered"])
        self.assertIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_split_coverage_clears_the_flag(self):
        # Same totals (2, 1, 1): each row is closed by a different kind of
        # evidence, so nothing needs another run.
        self.recovered("crossed", head("a"), sha("1"), sha("2"), failing=("gyro.transfer",))
        self.observation("same-digest", head("b"), sha("2"))
        self.recovered("covered", head("d"), sha("5"), sha("6"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial()
        bench = self.bench()
        self.assertEqual(bench["same_candidate_recoveries"], 2)
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 1)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 1)
        self.assertEqual(bench["recoveries_needing_followup"], [])
        self.assertNotIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_one_row_with_both_kinds_needs_nothing(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        self.observation("same-digest", head("b"), sha("2"))
        self.observation("trialrun", head("c"), sha("3"))
        self.trial()
        bench = self.bench()
        self.assertEqual(bench["same_candidate_recoveries"], 1)
        self.assertEqual(bench["recoveries_needing_followup"], [])
        self.assertNotIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_uncovered_row_is_named_without_any_declaration(self):
        self.recovered("crossed", head("a"), sha("1"), sha("2"), failing=("gyro.transfer",))
        self.observation("same-digest", head("b"), sha("2"))
        self.recovered("uncovered", head("d"), sha("5"), sha("6"))
        bench = self.bench()
        self.assertEqual(bench["improvement_trials"], [])
        self.assertEqual(bench["same_candidate_recoveries"], 2)
        self.assertEqual(bench["recoveries_with_cross_candidate_replay"], 1)
        self.assertEqual(bench["recoveries_superseded_by_promotion"], 0)
        self.assertEqual(bench["recoveries_needing_followup"], ["evidence/rounds/uncovered"])
        self.assertIn("cross_candidate_replay_needed", self.report()["attention"])

    def test_review_missing_edge_positive_control_and_ordinary_prerequisites(self):
        tasks = [
            self.review_task("FIX1"),
            self.review_task("RECHECK", role="judge", needs=["FIX1"],
                             independent_of=["FIX1"]),
            self.review_task("RECHECK2", role="judge", needs=["FIX1"]),
            self.review_task("ORDINARY"),
            self.review_task("AUTHOR-JUDGE", role="judge", needs=["ORDINARY"]),
        ]
        events = [self.queue_event(1, "RECHECK2", "claim", "recheck-session", "recheck-session"),
                  self.queue_event(2, "RECHECK2", "done", "recheck-session")]
        registry = self.registry(
            {"id": "RECHECK", "kind": "queue", "revision_prerequisites": ["FIX1"]},
            {"id": "RECHECK2", "kind": "queue", "revision_prerequisites": ["FIX1"]})
        evidence = self.review_report(tasks, events, registry)["review_evidence"]
        self.assertEqual([m["review"] for m in evidence["missing_independence_edges"]],
                         ["RECHECK2"])
        missing = evidence["missing_independence_edges"][0]
        self.assertEqual(missing["revision_prerequisites"], ["FIX1"])
        self.assertEqual(missing["missing"], ["FIX1"])
        by_id = {r["id"]: r for r in evidence["reviews"]}
        self.assertEqual(by_id["RECHECK"]["edge_status"], "covered")
        self.assertEqual(by_id["RECHECK"]["missing_independence_edges"], [])
        self.assertEqual(by_id["RECHECK2"]["edge_status"], "missing")
        self.assertEqual(by_id["RECHECK2"]["claimed_by"]["session"], "recheck-session")
        self.assertEqual(by_id["RECHECK2"]["answer_frozen"]["source"], "queue done event 2")
        self.assertEqual(by_id["AUTHOR-JUDGE"]["edge_status"], "not-applicable")
        self.assertIn("ORDINARY", evidence["ordinary_prerequisites_not_flagged"])
        self.assertEqual(evidence["registry"]["status"], "loaded")
        declared = {m["task"]: m for m in evidence["missing_declarations"]}
        self.assertEqual(sorted(declared), ["AUTHOR-JUDGE", "RECHECK2"])
        self.assertTrue(declared["RECHECK2"]["registry_entry"])
        self.assertTrue(declared["RECHECK2"]["covered_elsewhere"])
        self.assertFalse(declared["AUTHOR-JUDGE"]["registry_entry"])
        self.assertFalse(declared["AUTHOR-JUDGE"]["covered_elsewhere"])

    def test_revise_event_derives_a_missing_edge_without_a_registry(self):
        tasks = [self.review_task("FIX-A"),
                 self.review_task("J", role="judge", needs=["FIX-A"])]
        events = [self.queue_event(1, "J", "revise", "judge", data={"fix": "FIX-A"})]
        evidence = self.review_report(tasks, events)["review_evidence"]
        self.assertEqual(evidence["registry"]["status"], "unregistered")
        self.assertEqual(evidence["out_of_queue"], [])
        self.assertEqual([m["review"] for m in evidence["missing_independence_edges"]], ["J"])
        self.assertIn("queue-revise", evidence["missing_independence_edges"][0]["source"])

    def test_judge_without_declaration_is_listed_not_silent(self):
        # Reproduction of QUEUE-JUDGE-EDGE-DECL: a done judge task without
        # independent_of used to drop out of judge_completed_with_independence
        # _edges silently (edge_status "not-applicable", no warning anywhere).
        tasks = [self.review_task("UP1"),
                 self.review_task("UP2"),
                 self.review_task("J-DECLARED", role="judge", needs=["UP1"],
                                  independent_of=["UP1"]),
                 self.review_task("J-BARE", role="judge", needs=["UP1", "UP2"])]
        events = [self.queue_event(1, "J-DECLARED", "claim", "s1", "s1"),
                  self.queue_event(2, "J-DECLARED", "done", "s1"),
                  self.queue_event(3, "J-BARE", "claim", "s2", "s2"),
                  self.queue_event(4, "J-BARE", "done", "s2")]
        result = self.review_report(tasks, events)
        evidence = result["review_evidence"]
        by_id = {r["id"]: r for r in evidence["reviews"]}
        self.assertEqual(by_id["J-BARE"]["independent_of"], [])
        self.assertEqual(by_id["J-BARE"]["edge_status"], "not-applicable")
        declared = {m["task"]: m for m in evidence["missing_declarations"]}
        self.assertEqual(sorted(declared), ["J-BARE"])
        self.assertEqual(declared["J-BARE"]["state"], "done")
        self.assertFalse(declared["J-BARE"]["registry_entry"])
        self.assertFalse(declared["J-BARE"]["covered_elsewhere"])
        goals = result["queue"]["goals"]["pilot"]
        self.assertIn("J-BARE", goals["judge_tasks"])
        self.assertNotIn("J-BARE", goals["judge_completed_with_independence_edges"])
        self.assertIn("judge_independence_undeclared", result["attention"])

    def test_out_of_queue_evidence_requires_explicit_registration(self):
        task = self.review_task("J", role="judge")
        first = self.review_report([task], [])
        self.assertEqual(first["review_evidence"]["out_of_queue"], [])
        self.assertEqual(first["review_evidence"]["registry"]["status"], "unregistered")
        self.assertIn("no explicit review registry", " ".join(first["review_evidence"]["unknowns"]))
        anchor = self.anchor("anchor.md")
        registry = self.registry({
            "id": "OUT-REVIEW", "kind": "out-of-queue", "session": "blind-session",
            "commits": [{"commit": anchor, "role": "blind-answer"},
                        {"commit": anchor, "role": "review"}],
            "scope": {"countable_subquestions": 14, "total_subquestions": 15,
                      "excluded_subquestions": ["1(e)"], "source": "declared"},
            "disclosures": [{"item": "1(e)", "kind": "over-read",
                             "sources": ["evidence/blind.md §0.1"],
                             "consequence": "excluded from independent evidence"}]})
        second = self.review_report([task], [], registry)
        entry = second["review_evidence"]["out_of_queue"][0]
        self.assertFalse(entry["task_in_queue"])
        self.assertEqual(entry["claim_status"], "none")
        self.assertEqual(entry["queue_claims"], [])
        self.assertEqual(entry["session_events"], 0)
        self.assertEqual(entry["scope"]["countable_subquestions"], 14)
        self.assertEqual(entry["scope"]["total_subquestions"], 15)
        self.assertEqual(entry["scope"]["excluded_subquestions"], ["1(e)"])
        self.assertFalse(entry["disclosures"][0]["detected"])
        self.assertEqual(entry["disclosures"][0]["status"], "declared")
        self.assertEqual(entry["disclosures"][0]["detection"], "not-observable")
        self.assertEqual([c["status"] for c in entry["commits"]], ["verified", "verified"])
        self.assertEqual(entry["answer_frozen"]["commit"], anchor)
        self.assertEqual(entry["review_frozen_at"]["commit"], anchor)
        self.assertIn("not the instant", entry["answer_frozen"]["source"])

    def test_incomplete_registration_is_explicit(self):
        tasks = [self.review_task("J", role="judge", needs=["FIX1"])]
        registry = self.registry(
            {"id": "J", "kind": "queue", "revision_prerequisites": ["NOT-A-NEED"]},
            {"id": "OUT", "kind": "out-of-queue"})
        evidence = self.review_report(tasks, [], registry)["review_evidence"]
        by_id = {r["id"]: r for r in evidence["reviews"]}
        self.assertEqual(by_id["J"]["registration_status"], "incomplete")
        self.assertEqual(by_id["J"]["declared_revision_prerequisites_not_in_needs"],
                         ["NOT-A-NEED"])
        out = evidence["out_of_queue"][0]
        self.assertEqual(out["registration_status"], "incomplete")
        self.assertIn("session", out["registration_missing"])
        self.assertIn("commits", out["registration_missing"])
        self.assertIn("scope", out["registration_missing"])
        self.assertEqual(out["claim_status"], "none")

    def test_same_name_identity_is_unverified(self):
        tasks = [self.review_task("J", role="judge")]
        events = [self.queue_event(1, "J", "claim", "judge-name", "judge-name"),
                  self.queue_event(2, "J", "done", "judge-name")]
        first, second = self.anchor("one.md"), self.anchor("two.md")
        registry = self.registry(identity=[{"session": "judge-name", "task": "J",
                                            "reports": [first, second]}])
        identity = self.review_report(tasks, events, registry)["review_evidence"][
            "identity_ambiguities"][0]
        self.assertEqual(identity["status"], "identity-unverified")
        self.assertEqual(identity["claim_count"], 1)
        self.assertEqual(identity["report_count"], 2)
        self.assertIn("cannot identify", identity["reason"])

    def test_normal_reclaim_updates_count_without_a_false_edge(self):
        tasks = [self.review_task("ORDINARY"),
                 self.review_task("J", role="judge", needs=["ORDINARY"])]
        events = [self.queue_event(1, "J", "claim", "worker", "worker"),
                  self.queue_event(2, "J", "release", "worker"),
                  self.queue_event(3, "J", "claim", "worker", "worker")]
        evidence = self.review_report(tasks, events)["review_evidence"]
        review = {r["id"]: r for r in evidence["reviews"]}["J"]
        self.assertEqual(review["claim_count"], 2)
        self.assertEqual(review["claimed_by"]["event"], 3)
        self.assertEqual(review["edge_status"], "not-applicable")
        self.assertEqual(evidence["missing_independence_edges"], [])

    def plan(self, rel, tasks):
        repo = self.root / "fusion-lean-skills"
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "tasks": tasks}))
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "plan")
        return f"fusion-lean-skills@{self.git(repo, 'rev-parse', 'HEAD').strip()}:{rel}"

    def skills_head(self):
        return self.git(self.root / "fusion-lean-skills", "rev-parse", "HEAD").strip()

    def contract_report(self, tasks, events, contracts=None, registry=None):
        if registry is not None:
            self.put("evidence/workflow-trials/review-evidence/reviews.json", registry)
        self.commit()
        return workflow_health.report(tasks, events, self.root, PROJECTS, None,
                                      contracts=contracts)

    def test_contract_audits_non_judge_tasks_and_keeps_ordinary_needs(self):
        tasks = [
            self.review_task("ORACLE", role="judge"),
            self.review_task("ENTRY", role="A", needs=["ORACLE"], independent_of=["ORACLE"]),
            self.review_task("VERIFY", role="judge", needs=["ENTRY", "ORACLE"],
                             independent_of=["ENTRY"]),
        ]
        ref = self.plan("fusion-lean-workflow/plans/p.json", [
            {"id": "ENTRY", "role": "A", "needs": ["ORACLE"],
             "independent_of": ["ORACLE"]},
            {"id": "VERIFY", "role": "judge", "needs": ["ENTRY", "ORACLE"],
             "independent_of": ["ENTRY"]},
        ])
        evidence = self.contract_report(tasks, [], [ref])["review_evidence"]
        by_id = {r["id"]: r for r in evidence["reviews"]}
        self.assertIn("ENTRY", by_id)  # a non-judge task with an explicit contract
        self.assertEqual(by_id["ENTRY"]["role"], "A")
        self.assertEqual(by_id["ENTRY"]["independence_status"], "covered")
        self.assertEqual(by_id["VERIFY"]["independence_status"], "covered")
        self.assertIn("ORACLE", evidence["ordinary_prerequisites_not_flagged"])
        self.assertEqual(evidence["contracts"][0]["status"], "loaded")
        self.assertEqual(evidence["contracts"][0]["tasks"], 2)
        pairs = {(p["task"], p["need"]): p for p in evidence["contract_checks"]}
        self.assertEqual(pairs[("VERIFY", "ORACLE")]["status"], "ordinary")
        self.assertEqual(pairs[("VERIFY", "ENTRY")]["expected"], "independent")
        self.assertTrue(pairs[("VERIFY", "ENTRY")]["actual_independent"])
        self.assertEqual(evidence["contract_summary"]["status"], "covered")

    def test_contract_names_a_missing_independence_edge(self):
        tasks = [
            self.review_task("ORACLE", role="judge"),
            self.review_task("ENTRY", role="A", needs=["ORACLE"], independent_of=[]),
        ]
        ref = self.plan("fusion-lean-workflow/plans/p.json", [
            {"id": "ENTRY", "role": "A", "needs": ["ORACLE"],
             "independent_of": ["ORACLE"]},
        ])
        evidence = self.contract_report(tasks, [], [ref])["review_evidence"]
        entry = {r["id"]: r for r in evidence["reviews"]}["ENTRY"]
        self.assertEqual(entry["edge_status"], "missing")
        self.assertEqual(entry["missing_independence_edges"], ["ORACLE"])
        self.assertEqual(entry["independence_status"], "missing-independence-edge")
        self.assertEqual(evidence["contract_summary"]["missing_independence_edges"][0]
                         ["task"], "ENTRY")
        self.assertEqual(evidence["contract_summary"]["status"],
                         "missing-independence-edge")
        self.assertFalse(evidence["contract_summary"]["complete"])

    def test_contract_names_a_missing_dependency_without_calling_it_covered(self):
        tasks = [self.review_task("VERIFY", role="judge")]
        ref = self.plan("fusion-lean-workflow/plans/p.json", [
            {"id": "ENTRY", "role": "A", "needs": [], "independent_of": []},
            {"id": "VERIFY", "role": "judge", "needs": ["ENTRY"],
             "independent_of": ["ENTRY"]},
        ])
        evidence = self.contract_report(tasks, [], [ref])["review_evidence"]
        verify = {r["id"]: r for r in evidence["reviews"]}["VERIFY"]
        self.assertEqual(verify["edge_status"], "missing")
        self.assertEqual(verify["missing_dependencies"], ["ENTRY"])
        self.assertEqual(verify["independence_status"], "missing-dependency")
        self.assertEqual(evidence["contract_summary"]["missing_contract_tasks"], ["ENTRY"])
        missing = evidence["contract_summary"]["missing_dependencies"][0]
        self.assertEqual((missing["task"], missing["need"], missing["status"]),
                         ("VERIFY", "ENTRY", "missing-dependency"))

    def test_unreadable_contract_is_an_insufficient_source(self):
        tasks = [self.review_task("J", role="judge")]
        ref = (f"fusion-lean-skills@{self.skills_head()}:"
               "fusion-lean-workflow/plans/absent.json")
        evidence = self.contract_report(tasks, [], [ref])["review_evidence"]
        self.assertEqual(evidence["contracts"][0]["status"], "unreadable")
        self.assertEqual(evidence["contract_summary"]["status"], "insufficient-source")
        self.assertFalse(evidence["contract_summary"]["complete"])
        self.assertEqual(evidence["contract_summary"]["sources_failed"], 1)

    def test_invalid_plan_definition_is_not_treated_as_passing(self):
        tasks = [self.review_task("J", role="judge")]
        ref = self.plan("fusion-lean-workflow/plans/p.json", [
            {"id": "J", "needs": [], "independent_of": ["NOT-A-NEED"]},
        ])
        evidence = self.contract_report(tasks, [], [ref])["review_evidence"]
        self.assertEqual(evidence["contracts"][0]["status"], "invalid")
        self.assertEqual(evidence["contract_summary"]["status"], "insufficient-source")

    def test_without_contracts_the_legacy_shape_is_kept(self):
        tasks = [self.review_task("FIX1"),
                 self.review_task("J", role="judge", needs=["FIX1"]),
                 self.review_task("AUTHOR", role="A")]
        evidence = self.contract_report(tasks, [])["review_evidence"]
        self.assertEqual(evidence["contracts"], [])
        self.assertEqual(evidence["contract_checks"], [])
        self.assertIsNone(evidence["contract_summary"])
        self.assertEqual([r["id"] for r in evidence["reviews"]], ["J"])
        self.assertNotIn("independence_status", evidence["reviews"][0])

    def test_registry_does_not_change_benchmark_or_queue_semantics(self):
        self.recovered("base", head("a"), sha("1"), sha("2"))
        tasks = [self.review_task("FIX1"),
                 self.review_task("J", role="judge", needs=["FIX1"])]
        events = [self.queue_event(1, "J", "claim", "s", "s")]
        before = self.review_report(tasks, events)
        after = self.review_report(tasks, events, self.registry(
            {"id": "J", "kind": "queue", "revision_prerequisites": ["FIX1"]}))
        self.assertEqual(before["schema"], "fusion-workflow-health/v1")
        self.assertEqual(before["benchmarks"], after["benchmarks"])
        self.assertEqual(before["queue"], after["queue"])
        self.assertEqual(before["attention"], after["attention"])
        self.assertEqual(before["review_evidence"]["out_of_queue"], [])
        self.assertEqual(after["review_evidence"]["registry"]["status"], "loaded")


if __name__ == "__main__":
    unittest.main()
