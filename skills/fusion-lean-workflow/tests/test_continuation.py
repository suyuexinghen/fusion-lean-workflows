"""Replay synthetic successor decisions with isolated queue and Git evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import time
import unittest

import test_queue
from test_queue import ROOT, task


class ContinuationTest(unittest.TestCase):
    setUp = test_queue.QueueTest.setUp
    cli = test_queue.QueueTest.cli
    proc = test_queue.QueueTest.proc
    plan = test_queue.QueueTest.plan

    def freeze(self, name, payload):
        path = self.repo / name
        path.write_text(json.dumps(payload, ensure_ascii=False))
        git = ["git", "-C", str(self.repo)]
        subprocess.run([*git, "add", name], check=True)
        subprocess.run([*git, "-c", "user.name=test", "-c", "user.email=test@local",
                        "commit", "-qm", name], check=True)
        commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        return f"fusion-lean@{commit}:{name}"

    def apply(self, ref, **kwargs):
        return self.cli("successors", ref, "--host", "fixture-host", "--session", "coordinator", **kwargs)

    def receipt(self, name, *decisions):
        return self.freeze(name, {"version": 1, "decisions": decisions})

    def decision(self, work_key, action, *, goal="pilot", **kwargs):
        return dict(host="fixture-host", goal=goal, work_key=work_key, action=action,
                    result_id=work_key, evidence=[self.ref], finding="Frozen historical gap",
                    acceptance="Replay fixed evidence", **kwargs)

    def dispatch(self, tid, work_key, rev, session="receiver"):
        return self.cli("dispatch", tid, "--host", "fixture-host", "--work-key", work_key,
                        "--session", "coordinator", "--to", session, "--rev", str(rev))

    def test_a01_reuse_and_lost_confirmation(self):
        plan_file = ROOT / "tests/fixtures/continuation/a01.json"
        plan = json.loads(plan_file.read_text())
        upstream = plan["tasks"][0]["needs"][0]
        self.cli("init", self.plan(task(upstream)))
        self.cli("claim", upstream, "--session", "source")
        self.cli("done", upstream, "--session", "source", "--rev", "1",
                 "--note", "Rehydrate antecedent", "--evidence", self.ref)
        self.cli("load", plan_file, "--session", "source")
        tid = plan["tasks"][0]["id"]
        work_key = "aggregation/follow-up"
        decision = self.decision(work_key, "reuse", goal=plan["tasks"][0]["goal"], task_ids=[tid])
        ref = self.receipt("a01.json", decision)
        self.assertEqual(self.apply(ref)[0]["tasks"][0]["state"], "ready")
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(lambda _: self.dispatch(tid, work_key, 0), range(2))
        self.assertEqual(first["dispatch"], second["dispatch"])
        self.assertEqual(sum(row["recorded"] for row in (first, second)), 1)
        self.assertEqual(self.dispatch(tid, work_key, 0)["status"], "pending_delivery")
        seq = str(first["dispatch"]["seq"])
        args = ("start", tid, "--session", "receiver", "--dispatch", seq)
        with ThreadPoolExecutor(2) as pool:
            starts = list(pool.map(lambda _: self.cli(*args)["status"], range(2)))
        self.assertEqual(sorted(starts), ["already_started", "started"])
        self.assertEqual(self.dispatch(tid, work_key, 1)["status"], "started")
        self.assertFalse(self.apply(ref)[0]["recorded"])
        self.assertEqual(self.apply(ref)[0]["tasks"][0]["owner"], "receiver")
        self.assertEqual([e["action"] for e in self.cli("history", tid)],
                         ["add", "dispatch", "claim", "start"])
        conflict = self.receipt("a01-conflict.json", dict(decision, task_ids=["different"]))
        self.assertIn("already maps", self.apply(conflict, code=1)["error"])

    def test_transferred_owner_requires_start(self):
        self.cli("init", self.plan(task("a")))
        ref = self.receipt("a.json", self.decision("a", "reuse", task_ids=["a"]))
        self.apply(ref)
        seq = self.dispatch("a", "a", 0)["dispatch"]["seq"]
        self.cli("claim", "a", "--session", "original")
        self.cli("handoff", "a", "--session", "original", "--to", "receiver",
                 "--rev", "1", "--note", "Transfer from an alternate executor")
        self.assertEqual(self.dispatch("a", "a", 2)["status"], "awaiting_confirmation")
        self.assertEqual(self.cli("start", "a", "--session", "receiver",
                                  "--dispatch", str(seq))["status"], "started")
        self.assertEqual(self.dispatch("a", "a", 2)["status"], "started")

    def test_existing_graph_and_reload_after_interruption(self):
        plan_data = json.loads((ROOT / "tests/fixtures/continuation/parallel.json").read_text())
        ids = ["LATER-EXPECT", "LATER-BUILD", "LATER-RUN", "LATER-REVIEW"]
        self.cli("init", self.plan(task("EARLIER-CLOSE")))
        plan = self.freeze("later-plan.json", plan_data)
        decision = self.decision("later/graph", "load", goal="later-aggregation",
                                 task_ids=ids, plan=plan)
        ref = self.receipt("later.json", decision)
        self.assertEqual(set(self.apply(ref)[0]["added"]), set(ids))
        self.assertEqual(self.apply(ref)[0]["added"], [])
        already_loaded = self.receipt("later-reuse.json", dict(decision, action="reuse"))
        self.assertEqual(self.apply(already_loaded)[0]["added"], [])
        review = self.cli("show", "LATER-REVIEW")
        self.assertEqual(review["independent_of"], ["LATER-BUILD", "LATER-RUN"])
        self.assertEqual(review["state"], "waiting")
        self.assertEqual(len(self.cli("history", "--goal", "later-aggregation")), 4)

    def test_external_material_missing_and_targeted_recovery(self):
        self.cli("init", self.plan(task("unrelated")))
        erratum = self.decision("source/erratum", "wait_external",
                                resume_when="Freeze the synthetic erratum")
        supplement = self.decision("source/supplement", "wait_external",
                                   resume_when="Freeze the synthetic supplement")
        wait = self.receipt("wait.json", erratum, supplement)
        before = self.cli("snapshot")
        self.assertEqual([row["added"] for row in self.apply(wait)], [[], []])
        self.assertEqual(self.cli("snapshot"), before)
        close = self.decision("close", "close")
        budget = self.decision("budget", "needs_decision", decision_needed="Increase trial budget")
        self.assertEqual([row["action"] for row in self.apply(self.receipt("stop.json", close, budget))],
                         ["close", "needs_decision"])
        plan = self.freeze("external-plan.json", {"version": 1, "tasks": [task("check-erratum"), task("check-supplement")]})
        recovery = dict(erratum, action="load", task_ids=["check-erratum"], plan=plan)
        no_page = self.receipt("no-page.json", recovery)
        self.assertIn("recovery_evidence", self.apply(no_page, code=1)["error"])
        old_page = self.receipt("old-page.json", dict(recovery, recovery_evidence=self.ref))
        self.assertIn("newly frozen", self.apply(old_page, code=1)["error"])
        page = self.freeze("synthetic-erratum.json", {"fixture": "isolation only"})
        new_page = self.receipt("new-page.json", dict(recovery, recovery_evidence=page))
        self.assertEqual(self.apply(new_page)[0]["added"], ["check-erratum"])
        self.assertEqual(self.apply(new_page)[0]["added"], [])
        self.assertEqual([t["id"] for t in self.cli("list")], ["check-erratum", "unrelated"])

    def test_watcher_loads_a_later_batch_after_its_cross_goal_predecessor(self):
        self.cli("init", self.plan(dict(task("initial"), goal="earlier-research")))
        self.cli("claim", "initial", "--session", "initial-author")
        plan = self.freeze("later-plan.json", {
            "version": 1, "tasks": [task("later", needs=["initial"], independent_of=["initial"])],
        })
        ref = self.receipt("later.json", self.decision(
            "later", "load", task_ids=["later"], plan=plan, after=["initial"],
        ))
        deferred = self.apply(ref)[0]
        self.assertEqual(deferred["deferred_on"], ["initial"])
        self.assertFalse(deferred["recorded"])
        self.assertIn("unknown task", self.cli("show", "later", code=1)["error"])

        receiver = self.work / "receiver.py"
        receiver.write_text("""
import json
import os
import subprocess
import sys

packet = json.loads(os.environ["FUSION_CONTINUATION"])
tid = packet["task"]["id"]
session = packet["session"]
start = subprocess.check_output(
    [*packet["queue"], "start", tid, "--session", session, "--dispatch", str(packet["dispatch"])],
    text=True,
)
row = json.loads(start)
assert row["status"] == "started"
subprocess.run(
    [*packet["queue"], "done", tid, "--session", session, "--rev", str(row["task"]["rev"]),
     "--note", "Confirmed isolated delivery", "--evidence", sys.argv[1]], check=True,
)
""")
        routes = self.work / "routes.json"
        routes.write_text(json.dumps({"later": {
            "work_key": "later", "session": "review-receiver",
            "command": [sys.executable, str(receiver), self.ref],
        }}))
        logfile = self.work / "watch.jsonl"
        with logfile.open("w") as out, subprocess.Popen([
            ROOT / "scripts/fusion-continue", "--db", self.db, "--repos", self.work,
            "--host", "fixture-host", "--session", "coordinator", "--receipt", ref,
            "--routes", routes, "--interval", "0.05", "--retry-seconds", "1",
        ], stdout=out, stderr=subprocess.STDOUT) as watcher:
            try:
                deadline = time.monotonic() + 7
                while '"event": "watch_start"' not in logfile.read_text():
                    if time.monotonic() > deadline or watcher.poll() is not None:
                        self.fail(logfile.read_text())
                    time.sleep(0.02)
                self.assertEqual(self.cli("list")[0]["id"], "initial")
                self.cli("done", "initial", "--session", "initial-author", "--rev", "1",
                         "--note", "Finish first batch", "--evidence", self.ref)
                self.assertEqual(watcher.wait(timeout=10), 0, logfile.read_text())
            finally:
                if watcher.poll() is None:
                    watcher.terminate()
        events = self.cli("history")
        self.assertEqual([(e["task"], e["action"]) for e in events[2:]], [
            ("initial", "done"), ("later", "add"), ("later", "dispatch"),
            ("later", "claim"), ("later", "start"), ("later", "done"),
        ])
        self.assertEqual(events[-3]["actor"], "review-receiver")
        observed = [json.loads(line) for line in logfile.read_text().splitlines()]
        first_ready = next(row for row in observed if row.get("status") == "pending_delivery")
        self.assertIsNotNone(first_ready["ready_observed_at"])
        self.assertEqual(first_ready["predecessor_done"], {"initial": events[2]["seq"]})
        self.assertEqual(self.apply(ref)[0]["added"], [])
