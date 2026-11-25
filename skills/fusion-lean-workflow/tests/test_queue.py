"""Exercise the public CLI in independent processes with isolated SQLite and Git."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/fusion-task"


def task(tid, **fields):
    return dict(id=tid, goal="pilot", stage="P0", role="author", title=tid,
                acceptance="fixed evidence", **fields)


class QueueTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name)
        self.db = self.work / "queue.db"
        self.repo = self.work / "fusion-lean"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "proof.md").write_text("test fixture\n")
        git = ["git", "-C", str(self.repo)]
        subprocess.run([*git, "add", "proof.md"], check=True)
        subprocess.run([*git, "-c", "user.name=test", "-c", "user.email=test@local",
                        "commit", "-qm", "fixture"], check=True)
        self.rev = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        self.ref = f"fusion-lean@{self.rev}:proof.md"

    def proc(self, *args, db=None):
        return subprocess.run([CLI, "--db", db or self.db, "--repos", self.work, *args],
                              capture_output=True, text=True, timeout=12)

    def cli(self, *args, code=0, db=None):
        proc = self.proc(*args, db=db)
        self.assertEqual(proc.returncode, code, proc.stderr)
        return json.loads(proc.stdout if code == 0 else proc.stderr)

    def plan(self, *tasks, name="plan.json"):
        path = self.work / name
        path.write_text(json.dumps({"version": 1, "tasks": tasks}))
        return path

    def test_competing_claims_handover_and_dependency(self):
        self.cli("init", self.plan(task("a"), task("b", needs=["a"])))
        self.cli("claim", "b", "--session", "early", code=1)
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda s: self.proc("claim", "--session", s), ("one", "two")))
        self.assertEqual(sorted(p.returncode for p in outcomes), [0, 3])
        t = self.cli("show", "a")
        owner = t["owner"]
        self.assertEqual(self.cli("claim", "--session", owner), t)
        self.cli("done", "a", "--session", "wrong", "--rev", "1", "--note", "x", "--evidence", self.ref, code=1)
        self.cli("handoff", "a", "--session", owner, "--to", "next", "--rev", "1", "--note", "proof ready")
        self.cli("release", "a", "--session", owner, "--rev", "1", "--note", "old", code=1)
        self.assertEqual(self.cli("claim", "--session", "next")["note"], "proof ready")
        self.cli("done", "a", "--session", "next", "--rev", "2", "--note", "verified", "--evidence", self.ref)
        self.assertEqual(self.cli("next")[0]["id"], "b")
        self.assertEqual([e["action"] for e in self.cli("history", "a")], ["add", "claim", "handoff", "done"])
        self.assertEqual(len(self.cli("history", "--session", "next")), 2)

    def test_role_skills_survive_handoff_and_accept_practice_evidence(self):
        roles = {"A": "integrate", "B": "formalize", "C": "source-review",
                 "editor": "editor", "evidence": "evidence", "judge": "judge",
                 "literature": "literature"}
        known = [dict(task(role, artifacts=["fusion-lean:proof.md"]), role=role) for role in roles]
        self.cli("init", self.plan(*known, task("custom")))
        for item in self.cli("next"):
            if item["id"] == "custom":
                self.assertIsNone(item["role_skill"])
                continue
            expected = f"fusion-lean-{roles[item['role']]}/SKILL.md"
            self.assertEqual(item["role_skill"], expected)
            self.assertTrue((ROOT.parent / expected).is_file())
        literature = self.cli("list", "--role", "literature")
        self.assertEqual([item["id"] for item in literature], ["literature"])
        claimed = self.cli("claim", "--role", "literature", "--session", "librarian")
        self.assertEqual(claimed["role_skill"], "fusion-lean-literature/SKILL.md")
        self.assertEqual(self.cli("show", "literature")["role_skill"], claimed["role_skill"])
        skill = self.cli("claim", "editor", "--session", "first")["role_skill"]
        self.cli("handoff", "editor", "--session", "first", "--rev", "1", "--to", "next",
                 "--note", "check the revised citation")
        self.assertEqual(self.cli("claim", "--session", "next")["role_skill"], skill)
        self.assertEqual(self.cli("show", "editor")["role_skill"], skill)

        repo = self.work / "fusion-lean-skills"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(repo)], check=True)
        card = Path(skill).parent / "references/experience/editor-citation.md"
        (repo / card).parent.mkdir(parents=True)
        (repo / card).write_text("Compared cited lines with the proof body.\n")
        git = ["git", "-C", str(repo)]
        subprocess.run([*git, "add", str(card)], check=True)
        subprocess.run([*git, "-c", "user.name=test", "-c", "user.email=test@local",
                        "commit", "-qm", "practice"], check=True)
        sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        practice = f"fusion-lean-skills@{sha}:{card}"
        finish = ["done", "editor", "--session", "next", "--rev", "2", "--note", "rechecked"]
        self.cli(*finish, "--evidence", practice, code=1)
        done = self.cli(*finish, "--evidence", self.ref, "--evidence", practice)
        self.assertEqual(done["evidence"], [self.ref, practice])
        self.assertEqual(self.cli("history", "editor")[-1]["data"]["evidence"], done["evidence"])

    def test_compute_handoff_and_legacy_health(self):
        for name in ("fusion-lean-wiki", "fusion-lean-skills"):
            subprocess.run(["git", "clone", "-q", str(self.repo), str(self.work / name)], check=True)
        path = "runs/grid/run.json"
        plan = self.plan(dict(task("run", artifacts=[f"fusion-compute:{path}"]), role="compute"))
        self.cli("init", plan)
        baseline = self.cli("health")
        previous = self.work / "before.json"
        previous.write_text(json.dumps(baseline))
        self.assertNotIn("fusion-compute", baseline["heads"])
        claimed = self.cli("claim", "run", "--role", "compute", "--session", "solver")
        self.assertEqual(claimed["role_skill"], "fusion-compute/SKILL.md")
        finish = ["done", "run", "--session", "solver", "--rev", "1", "--note", "grid read"]
        self.cli(*finish, "--evidence", f"fusion-compute@{self.rev}:{path}", code=1)
        compute = self.work / "fusion-compute"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(compute)], check=True)
        (compute / path).parent.mkdir(parents=True)
        (compute / path).write_text('{"schema":"fusion-compute-run/v1","outcome":"observed"}')
        (compute / "plan.json").write_bytes(plan.read_bytes())
        git = ["git", "-C", str(compute)]
        subprocess.run([*git, "add", path, "plan.json"], check=True)
        subprocess.run([*git, "-c", "user.name=test", "-c", "user.email=test@local",
                        "commit", "-qm", "observation"], check=True)
        sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        ref = f"fusion-compute@{sha}:{path}"
        self.assertEqual(self.cli(*finish, "--evidence", ref)["evidence"], [ref])
        report = self.cli("health", "--previous", previous, "--contract", f"fusion-compute@{sha}:plan.json")
        self.assertEqual(report["compute"], {"head": sha, "run_records": [ref]})
        self.assertEqual(report["heads"]["fusion-compute"], sha)
        self.assertEqual(report["comparison"]["previous_heads"], baseline["heads"])
        self.assertEqual(report["benchmarks"], baseline["benchmarks"])

    def test_block_recovery_resource_and_revision_fence(self):
        self.cli("init", self.plan(task("a", resources=["shared"]), task("b", resources=["shared"]), task("c")))
        self.cli("claim", "a", "--session", "old")
        self.assertEqual(self.cli("show", "b")["resource_holders"], ["a"])
        self.cli("claim", "c", "--session", "busy")
        self.cli("recover", "a", "--session", "busy", "--rev", "1", "--note", "lost", code=1)
        self.cli("recover", "a", "--session", "new", "--rev", "1", "--note", "old process exited")
        self.cli("recover", "a", "--session", "third", "--rev", "1", "--note", "race", code=1)
        self.cli("block", "a", "--session", "new", "--rev", "2", "--note", "wait for source")
        self.assertEqual(self.cli("next")[0]["id"], "b")
        self.cli("unblock", "a", "--session", "A", "--rev", "3", "--note", "source arrived")
        self.cli("claim", "a", "--session", "old")
        self.cli("release", "a", "--session", "old", "--rev", "1", "--note", "stale retry", code=1)
        self.cli("release", "a", "--session", "old", "--rev", "5", "--note", "file and next test")
        self.assertEqual(len(self.cli("list", "--session", "old")), 0)

    def test_independent_review_revision_then_next_stage(self):
        judge = dict(task("j", needs=["a"], independent_of=["a"]), role="judge", stage="P1")
        self.cli("init", self.plan(task("a"), judge, task("end", needs=["j"])))
        self.cli("claim", "a", "--session", "author")
        self.cli("done", "a", "--session", "author", "--rev", "1", "--note", "candidate", "--evidence", self.ref)
        self.cli("claim", "j", "--session", "author", code=1)
        self.assertFalse(self.cli("next", "--stage", "P1", "--session", "author"))
        self.cli("claim", "--role", "judge", "--session", "reader")
        self.cli("handoff", "j", "--session", "reader", "--to", "author", "--rev", "1", "--note", "handoff", code=1)
        self.cli("revise", "j", "--session", "reader", "--rev", "1", "--fix", "r1", "--fix-role", "author",
                 "--note", "missing units", "--acceptance", "cite dimensional argument")
        self.assertEqual(self.cli("show", "j")["waiting_for"], ["r1"])
        self.cli("claim", "r1", "--session", "reader", code=1)
        self.assertEqual(self.cli("claim", "--role", "author", "--session", "writer2")["id"], "r1")
        self.cli("done", "r1", "--session", "writer2", "--rev", "1", "--note", "fixed", "--evidence", self.ref)
        self.cli("claim", "j", "--session", "writer2", code=1)
        self.cli("claim", "j", "--session", "reader")
        self.cli("done", "j", "--rev", "3", "--session", "reader", "--note", "accepted", "--evidence", self.ref)
        self.assertEqual(self.cli("next")[0]["id"], "end")

    def test_independent_refusal_follows_declared_edges_not_session_identity(self):
        judged = task("a")
        guarded = dict(task("guarded", needs=["a"], independent_of=["a"]), role="judge")
        unguarded = dict(task("unguarded", needs=["a"]), role="judge")
        self.cli("init", self.plan(judged, guarded, unguarded))
        self.cli("claim", "a", "--session", "author")
        self.cli("done", "a", "--session", "author", "--rev", "1", "--note", "candidate",
                 "--evidence", self.ref)
        # A declared independent_of edge refuses the prerequisite's author.
        refused = self.cli("claim", "guarded", "--session", "author", code=1)
        self.assertIn("independent-review conflict", refused["error"])
        # Without the edge the same session is accepted. Refusal therefore proves
        # declared edge metadata, not that the reviewer is a different process.
        claimed = self.cli("claim", "unguarded", "--session", "author")
        self.assertEqual(claimed["id"], "unguarded")

    def test_failed_imports_rollback_and_reimport_keeps_runtime_edges(self):
        p = self.plan(task("a"))
        self.cli("init", p)
        self.cli("claim", "a", "--session", "worker")
        for invalid in ([task("b"), task("b")], [task("b", needs=["missing"])],
                        [task("b", needs=["c"]), task("c", needs=["b"])],
                        [task("b"), dict(task("a"), title="changed")]):
            self.cli("load", self.plan(*invalid, name="bad.json"), "--session", "A", code=1)
            self.assertEqual(len(self.cli("list")), 1)
        self.cli("revise", "a", "--session", "worker", "--rev", "1", "--fix", "f", "--fix-role", "author",
                 "--note", "repair", "--acceptance", "cite")
        self.assertEqual(self.cli("init", p)["added"], [])
        self.assertEqual(self.cli("show", "a")["waiting_for"], ["f"])

    def test_evidence_gate_and_offline_restoration(self):
        self.cli("init", self.plan(task("a", artifacts=["fusion-lean:proof.md"])))
        self.cli("claim", "--session", "worker")
        cmd = ["done", "a", "--session", "worker", "--rev", "1", "--note", "result", "--evidence"]
        for ref in ("text", f"fusion-lean@{'f'*40}:proof.md", self.ref + ".missing", self.ref.replace("proof.md", ".")):
            self.cli(*cmd, ref, code=1)
        self.assertEqual(len(self.cli("history", "a")), 2)
        other = self.repo / "other.md"
        other.write_text("other")
        git = ["git", "-C", str(self.repo)]
        subprocess.run([*git, "add", "other.md"], check=True)
        subprocess.run([*git, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-qm", "other"], check=True)
        new = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        self.cli(*cmd, f"fusion-lean@{new}:other.md", code=1)
        self.cli(*cmd, self.ref)
        backup = self.work / "snapshot"
        self.cli("backup", backup)
        self.cli("backup", backup, code=1)
        restored = self.work / "restored"
        shutil.copyfile(backup, restored)
        self.assertEqual(self.cli("list", db=restored), self.cli("list"))
        self.assertEqual(self.cli("history", db=restored), self.cli("history"))
        snap = self.cli("snapshot", db=restored)
        self.assertEqual(snap, {"version": 1, "tasks": self.cli("list"), "events": self.cli("history")})
        self.cli("load", self.plan(task("b", needs=["a"])), "--session", "A", db=restored)
        self.assertEqual([t["id"] for t in self.cli("next", db=restored)], ["b"])
        self.assertEqual(len(self.cli("list")), 1)

    def test_health_across_candidates_and_observations(self):
        wiki = self.work / "fusion-lean-wiki"
        for repo in (wiki, self.work / "fusion-lean-skills"):
            subprocess.run(["git", "clone", "-q", str(self.repo), str(repo)], check=True)
        def save_round(folder, candidate, suite, *, paired):
            where = wiki / "evidence/rounds" / folder
            where.mkdir(parents=True)
            (where / "round.json").write_text("{}")
            def save(name, mode, digest, status):
                checks = [{"id": "environment" if status != "PASS" else mode, "status": status,
                           "evidence": {"reason": "object file 'A.olean' of module A does not exist"}}]
                data = {"target": {"mode": "commit", "head": candidate}, "checks": checks, "mode": mode,
                        "suite_sha256": digest, "summary": {status: 1}}
                (where / name).write_text(json.dumps(data))
            if paired:
                save("round-before.json", "audit", "0"*64, "UNRESOLVED")
            save("round-after.json" if paired else "audit.json", "audit", suite, "PASS")
            save("controls.json", "controls", suite, "PASS")
            return where
        def commit():
            cmd = ["git", "-C", str(wiki)]
            subprocess.run([*cmd, "add", "evidence"], check=True)
            subprocess.run([*cmd, "-c", "user.name=test", "-c", "user.email=test@local",
                            "commit", "-qm", "evidence"], check=True)

        self.cli("init", self.plan(task("a"), task("j", needs=["a"], independent_of=["a"])))
        save_round("first", self.rev, "1"*64, paired=True)
        commit()
        base = self.cli("health")
        self.assertEqual(base["benchmarks"]["recoveries_with_cross_candidate_replay"], 0)
        self.assertEqual(base["queue"]["waiting_on"], {"a": ["j"]})
        baseline = self.work / "baseline.json"
        baseline.write_text(json.dumps(base))
        self.cli("claim", "a", "--session", "author")
        self.cli("block", "a", "--rev", "1", "--session", "author", "--note", "waiting for evidence")
        save_round("other", "b"*40, "1"*64, paired=True)
        # Duplicate packet for one candidate cannot increase distinct-candidate recurrence.
        save_round("duplicate", self.rev, "1"*64, paired=True)
        broken = save_round("mixed", "c"*40, "2"*64, paired=True)
        doc = json.loads((broken / "controls.json").read_text())
        doc["target"]["head"] = "d"*40
        (broken / "controls.json").write_text(json.dumps(doc))
        commit()
        snap = self.cli("snapshot")
        report = self.cli("health", "--previous", baseline)
        self.assertEqual(self.cli("snapshot"), snap)
        self.assertEqual(report["queue"]["actions_in_window"], {"claim": 1, "block": 1})
        self.assertEqual(report["comparison"]["new_rounds"], ["evidence/rounds/" + n for n in ("duplicate","mixed","other")])
        self.assertIn("no_runnable_work:pilot", report["attention"])
        self.assertIn("missing_or_incomparable_benchmark_evidence", report["attention"])
        self.assertEqual(report["benchmarks"]["repeated_on_distinct_candidates"],
                         {"environment/missing-olean": sorted([self.rev, "b"*40])})
        first, = (r for r in report["benchmarks"]["rounds"] if r["folder"].endswith("/first"))
        self.assertEqual(first["other_passing_candidates_with_same_evaluator"], ["evidence/rounds/other"])
        base["queue"]["history_sha256"] = "unrelated"
        baseline.write_text(json.dumps(base))
        self.cli("health", "--previous", baseline, code=1)

    def test_artifact_amend_is_audited_and_bounded(self):
        evidence = self.repo / "evidence"
        evidence.mkdir()
        (evidence / "round.json").write_text("{}\n")
        git = ["git", "-C", str(self.repo)]
        subprocess.run([*git, "add", "evidence/round.json"], check=True)
        subprocess.run([*git, "-c", "user.name=test", "-c", "user.email=test@local",
                        "commit", "-qm", "round"], check=True)
        rev = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        blob = f"fusion-lean@{rev}:evidence/round.json"
        # A directory pattern can never match a Git blob, and the same ID cannot
        # be reloaded with a different definition. Both failures are reproduced.
        bad = task("a", artifacts=["fusion-lean:evidence"])
        self.cli("init", self.plan(bad, task("r", artifacts=["fusion-lean:evidence"])))
        self.cli("claim", "a", "--session", "worker")
        self.cli("claim", "r", "--session", "second")
        self.cli("done", "a", "--session", "worker", "--rev", "1", "--note", "delivered",
                 "--evidence", f"fusion-lean@{rev}:evidence", code=1)
        good = task("a", artifacts=["fusion-lean:evidence/*"])
        good_plan = self.plan(good, name="good.json")
        self.cli("load", good_plan, "--session", "A", code=1)
        self.cli("amend", "a", "--session", "worker", "--rev", "5",
                 "--plan", str(good_plan), "--note", "typo", code=1)
        self.cli("amend", "a", "--session", "worker", "--rev", "1", "--plan",
                 str(self.plan(dict(good, title="other"), name="mixed.json")),
                 "--note", "typo", code=1)
        self.cli("amend", "a", "--session", "worker", "--rev", "1", "--plan",
                 str(self.plan(task("b"), name="wrong.json")), "--note", "typo", code=1)
        self.cli("amend", "a", "--session", "worker", "--rev", "1", "--plan",
                 str(self.plan(good, dict(good), name="dup.json")), "--note", "typo", code=1)
        # A concurrent revision moves the task; the old revision is now stale.
        self.cli("revise", "r", "--session", "second", "--rev", "1", "--fix", "rf",
                 "--fix-role", "author", "--note", "rework", "--acceptance", "cite")
        self.cli("amend", "r", "--session", "second", "--rev", "1",
                 "--plan", str(good_plan), "--note", "typo", code=1)
        amended = self.cli("amend", "a", "--session", "worker", "--rev", "1",
                           "--plan", str(good_plan), "--note", "directory pattern cannot match a blob")
        self.assertTrue(amended["changed"])
        self.assertEqual((amended["rev"], amended["before"], amended["after"]),
                         (2, ["fusion-lean:evidence"], ["fusion-lean:evidence/*"]))
        self.assertEqual(amended["task"]["artifacts"], ["fusion-lean:evidence/*"])
        self.assertEqual(self.cli("show", "a")["state"], "active")
        event = self.cli("history", "a")[-1]
        self.assertEqual(event["action"], "amend-artifacts")
        self.assertEqual(event["data"]["before"], ["fusion-lean:evidence"])
        self.assertEqual(event["data"]["after"], ["fusion-lean:evidence/*"])
        self.assertEqual(event["note"], "directory pattern cannot match a blob")
        self.assertEqual(event["actor"], "worker")
        self.assertEqual(self.cli("load", good_plan, "--session", "A")["added"], [])
        # Repeating the same correction at the current revision adds no event.
        history = self.cli("history", "a")
        retry = self.cli("amend", "a", "--session", "worker", "--rev", "2",
                         "--plan", str(good_plan), "--note", "retry")
        self.assertFalse(retry["changed"])
        self.assertEqual(self.cli("history", "a"), history)
        # The corrected glob does not weaken the blob gate.
        finish = ["done", "a", "--session", "worker", "--rev", "2", "--note", "delivered",
                  "--evidence"]
        self.cli(*finish, f"fusion-lean@{rev}:evidence/missing.json", code=1)
        self.cli(*finish, blob)
        replay = self.cli("amend", "a", "--session", "worker", "--rev", "3",
                          "--plan", str(good_plan), "--note", "replay")
        self.assertFalse(replay["changed"])
        self.assertEqual([e["action"] for e in self.cli("history", "a")],
                         ["add", "claim", "amend-artifacts", "done"])
        self.assertEqual(self.cli("history", "--session", "worker")[-1]["data"]["evidence"], [blob])

    def test_packaged_plan_and_empty_database(self):
        self.cli("list", code=1)
        self.assertFalse(self.db.exists())
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: self.proc("init"), range(2)))
        self.assertTrue(all(p.returncode == 0 for p in outcomes), outcomes)
        self.assertEqual(sorted(len(json.loads(p.stdout)["added"]) for p in outcomes), [0, 8])
        self.assertEqual({t["role"] for t in self.cli("next")}, {"C"})
        self.assertEqual(self.cli("next", "--goal", "other"), [])
        self.assertEqual(len(self.cli("history", "--stage", "demo")), 8)


if __name__ == "__main__":
    unittest.main()
