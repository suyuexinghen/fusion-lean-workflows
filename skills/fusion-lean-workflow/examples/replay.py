"""Replay a synthetic task graph with real Git blobs and an isolated SQLite queue."""
from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


SKILL = Path(__file__).resolve().parents[1]
PROJECTS = ("fusion-lean", "fusion-compute", "fusion-lean-wiki", "fusion-lean-skills")
AGGREGATE = """\
import json
import sys
from fractions import Fraction

inputs = json.load(sys.stdin)
result = {}
for name, supplier in inputs.items():
    missing = {supplier["missing"]}
    if "--drops-zero" in sys.argv:
        missing.add("0")
    values = [int(value) for value in supplier["readings"] if value not in missing]
    result[name] = [len(values), str(Fraction(sum(values), len(values)))]
print(json.dumps(result))
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args], text=True,
    ).strip()


def replay(root):
    for project in PROJECTS:
        repo = root / project
        repo.mkdir()
        git(repo, "init", "--quiet", "--initial-branch=main", "--object-format=sha1")
        git(repo, "config", "user.name", "Workflow Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        git(repo, "config", "commit.gpgsign", "false")
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "Initialize synthetic checkout")

    def freeze(project, path, content):
        repo = root / project
        file = repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content, indent=2) + "\n"
        file.write_text(text, encoding="utf-8")
        git(repo, "add", "--", path)
        git(repo, "commit", "--quiet", "-m", "Freeze " + path)
        return f"{project}@{git(repo, 'rev-parse', 'HEAD')}:{path}"

    def queue(*args, exit_code=0):
        proc = subprocess.run(
            [sys.executable, str(SKILL / "scripts/fusion-task"), "--db", str(root / "queue.sqlite3"),
             "--repos", str(root), *map(str, args)], text=True, capture_output=True,
        )
        require(proc.returncode == exit_code, f"{args}: {proc.returncode} {proc.stderr}")
        return json.loads(proc.stdout if exit_code == 0 else proc.stderr)

    def claim(task, session, **kwargs):
        return queue("claim", f"DEMO-{task}", "--session", session, **kwargs)

    def finish(task, session, artifact):
        current = queue("show", f"DEMO-{task}")
        return queue("done", current["id"], "--session", session, "--rev", current["rev"],
                     "--note", "Fixed synthetic delivery", "--evidence", artifact)

    def aggregate(inputs, *options):
        script = root / "fusion-lean/aggregate.py"
        out = subprocess.check_output(
            [sys.executable, str(script), *options], input=json.dumps(inputs), text=True,
        )
        return json.loads(out)

    queue("init", SKILL / "plans/demo.json")
    before = root / "before.json"
    before.write_text(json.dumps(queue("health")), encoding="utf-8")
    claim("SOURCE", "source")
    inputs = {"A": {"missing": "", "readings": ["0", "12", ""]},
              "B": {"missing": "NR", "readings": ["4", "0", "18", "NR"]}}
    expected = {"A": [2, "6"], "B": [3, "22/3"]}
    source = freeze("fusion-lean-skills", "demo/source.json", {"inputs": inputs, "expected": expected})
    finish("SOURCE", "source", source)
    require({row["id"] for row in queue("next")} == {"DEMO-BUILD", "DEMO-RUN"},
            "Both branches must be ready together")
    build = claim("BUILD", "builder")
    claim("RUN", "numerics")
    queue("handoff", build["id"], "--session", "builder", "--rev", build["rev"],
          "--to", "builder-next", "--note", "Resume with frozen supplier contracts")
    entry = freeze("fusion-lean", "aggregate.py", AGGREGATE)
    actual = aggregate(inputs)
    require(actual == expected, "The public entry must match the source contract")
    finish("BUILD", "builder-next", entry)
    require(queue("show", "DEMO-RECEIPT")["waiting_for"] == ["DEMO-RUN"],
            "Receipt must still wait for the other branch")
    run_results = {"A": [2, str((0 + 12) // 2)], "B": [3, f"{4 + 0 + 18}/3"]}
    run = freeze("fusion-compute", "runs/demo/run.json", {"actual": run_results, "source": source})
    finish("RUN", "numerics", run)

    claim("RECEIPT", "coordinator")
    require(actual == run_results, "Receipt must reconcile both branches")
    receipt = freeze("fusion-lean-skills", "demo/receipt.json", {"entry": entry, "run": run, "actual": actual})
    finish("RECEIPT", "coordinator", receipt)
    require({row["id"] for row in queue("next")} == {"DEMO-WIKI", "DEMO-BENCH"},
            "Documentation and benchmark must be ready together")
    claim("WIKI", "writer")
    claim("BENCH", "replicator")
    draft_text = "The mean was 6 for A and 22/3 for B.\n"
    draft = freeze("fusion-lean-wiki", "wiki/report.md", draft_text)
    finish("WIKI", "writer", draft)
    wrong = aggregate(inputs, "--drops-zero")
    require(actual == expected and all(wrong[key] != expected[key] for key in inputs),
            "Same checks must accept the baseline and reject the fault for both input families")
    bench = freeze("fusion-lean-wiki", "evidence/benchmark.json",
                   {"source": source, "entry": entry, "actual": actual, "wrong": wrong})
    finish("BENCH", "replicator", bench)

    denied = claim("JUDGE", "writer", exit_code=1)
    require("independent-review conflict" in denied["error"], "The author must be ineligible")
    judge = claim("JUDGE", "reviewer")
    blind = freeze("fusion-lean-wiki", "evidence/independent.json",
                   {"source": source, "expected": expected, "kind": "scripted teaching fixture"})
    queue("revise", judge["id"], "--session", "reviewer", "--rev", judge["rev"],
          "--fix", "DEMO-REVISE", "--fix-role", "editor", "--acceptance", "Explain both denominators",
          "--note", "Draft gives two means but omits the valid-day counts")
    require(queue("show", "DEMO-JUDGE")["waiting_for"] == ["DEMO-REVISE"], "Judge must wait for the fix")
    claim("REVISE", "writer")
    final_text = (draft_text
                  + "For A, 2 of 3 days counted. For B, 3 of 4 days counted.\n"
                  + "A valid zero counts; an unreported reading does not.\n"
                  + f"Source: {source}\nComputation: {receipt}\n")
    final = freeze("fusion-lean-wiki", "wiki/report.md", final_text)
    finish("REVISE", "writer", final)
    claim("JUDGE", "reviewer")
    review = freeze("fusion-lean-wiki", "evidence/review.json",
                    {"verdict": "pass", "target": final, "first_draft": draft, "independent": blind,
                     "target_sha256": sha256(final_text.encode()).hexdigest(),
                     "location": "wiki/report.md:2", "checks": ["mean", "included days", "missing values"]})
    finish("JUDGE", "reviewer", review)
    claim("CLOSE", "coordinator")
    close = freeze("fusion-lean-skills", "demo/close.json", {"consumed": [receipt, final, review, bench]})
    finish("CLOSE", "coordinator", close)
    report = queue("health", "--previous", before)
    states = queue("list")
    require(len(states) == 9 and all(task["state"] == "done" for task in states), "All nine tasks must be done")
    history = {t: [event["action"] for event in queue("history", f"DEMO-{t}")] for t in ("BUILD", "JUDGE")}
    return {"finished": len(states), "parallel": [["BUILD", "RUN"], ["WIKI", "BENCH"]],
            "self_review_rejected": True, "task_events": history, "draft": draft, "final": final,
            "review": review, "wrong_output": wrong, "health_attention": report["attention"]}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="preserve artifacts in a new directory")
    args = parser.parse_args()
    if args.keep:
        root = args.keep.resolve()
        root.mkdir()
        report = dict(replay(root), workspace=str(root))
    else:
        with TemporaryDirectory(prefix="fusion-workflow-demo-") as directory:
            report = replay(Path(directory))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
