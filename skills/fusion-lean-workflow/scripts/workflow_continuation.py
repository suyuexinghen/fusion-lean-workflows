"""Reconcile frozen successor decisions and track delivery on the authoritative queue."""
from argparse import Namespace
import json
import subprocess


ACTIONS = {"reuse", "load", "wait_external", "close", "needs_decision"}


def register_commands(sub):
    cmd = sub.add_parser("successors", help="apply a frozen version 1 successor receipt")
    cmd.add_argument("receipt", help="project@full-commit:path")
    cmd.add_argument("--host", required=True, help="logical name of this authoritative queue")
    cmd.add_argument("--session", required=True)
    cmd = sub.add_parser("dispatch", help="record or reconcile delivery of a ready task")
    cmd.add_argument("id")
    cmd.add_argument("--session", required=True)
    cmd.add_argument("--to", required=True)
    cmd.add_argument("--host", required=True)
    cmd.add_argument("--work-key", required=True)
    cmd.add_argument("--rev", type=int, required=True)
    cmd = sub.add_parser("start", help="receive a dispatch and atomically confirm execution")
    cmd.add_argument("id")
    cmd.add_argument("--session", required=True)
    cmd.add_argument("--dispatch", type=int, required=True)


def read_json(ref, repos, evidence_refs, project_path):
    evidence_refs(repos, [ref])
    project, _, location = ref.partition("@")
    proc = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(project_path(repos, project)),
         "cat-file", "blob", location], capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def record_decision(db, args, decision, ids, dump):
    cursor = db.execute(
        """INSERT OR IGNORE INTO continuation_decision
           (host, goal, work_key, receipt, actor, action, task_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (args.host, decision["goal"], decision["work_key"], args.receipt, args.session,
         decision["action"], dump(ids)),
    )
    return cursor.rowcount == 1


def reconcile(db, args, *, Board, add_tasks, definition, dump, evidence_refs, key,
              keys, project_path, words):
    payload = read_json(args.receipt, args.repos, evidence_refs, project_path)
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int
            or payload["version"] != 1 or not isinstance(payload.get("decisions"), list)
            or not payload["decisions"]):
        raise ValueError("expected a version 1 successor receipt with decisions")
    db.execute("""CREATE TABLE IF NOT EXISTS continuation_decision (
        host TEXT NOT NULL, goal TEXT NOT NULL, work_key TEXT NOT NULL, receipt TEXT NOT NULL,
        actor TEXT NOT NULL, action TEXT NOT NULL, task_ids TEXT NOT NULL,
        time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(host, goal, work_key, receipt)
    )""")
    key(args.host, "host")
    board = Board(db)
    report = []
    seen = set()
    for decision in payload["decisions"]:
        if not isinstance(decision, dict) or decision.get("host") != args.host:
            raise ValueError("each decision must name this authoritative host")
        goal = key(decision.get("goal"), "goal")
        work_key = key(decision.get("work_key"), "work_key")
        identity = (args.host, goal, work_key)
        if identity in seen:
            raise ValueError(f"duplicate work key in receipt: {work_key}")
        seen.add(identity)
        key(decision.get("result_id"), "result_id")
        words(decision.get("finding"), "finding")
        words(decision.get("acceptance"), "acceptance")
        evidence = decision.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(ref, str) for ref in evidence):
            raise ValueError(f"{work_key} requires a list of fixed evidence references")
        evidence_refs(args.repos, evidence)
        action = decision.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError(f"unknown action for {work_key}: {action}")
        ids = keys(decision.get("task_ids", []), "task_ids")
        if bool(ids) != (action in ("load", "reuse")):
            raise ValueError(f"task IDs do not match action {action} for {work_key}")
        after = keys(decision.get("after", []), "after")
        if set(ids) & set(after):
            raise ValueError(f"{work_key} cannot wait for a task in its own decision")
        history = db.execute(
            """SELECT action, task_ids, receipt FROM continuation_decision
               WHERE host=? AND goal=? AND work_key=? ORDER BY rowid""", identity,
        ).fetchall()
        bound = next((json.loads(row["task_ids"]) for row in history
                      if row["task_ids"] != "[]"), [])
        if bound and ids and ids != bound:
            raise ValueError(f"{work_key} already maps to {bound}; reconcile the existing decision")
        pending = [tid for tid in after if board.tasks.get(tid, {}).get("state") != "done"]
        if pending:
            report.append(dict(work_key=work_key, goal=goal, action=action, added=[],
                               recorded=False, task_ids=ids, tasks=[], deferred_on=pending))
            continue
        waiting = [row["receipt"] for row in history if row["action"] == "wait_external"]
        if ids and not bound and waiting:
            recovery = words(decision.get("recovery_evidence"), "recovery_evidence")
            evidence_refs(args.repos, [recovery])
            for old_ref in waiting:
                old = read_json(old_ref, args.repos, evidence_refs, project_path)
                if any(row["goal"] == goal and row["work_key"] == work_key
                       and recovery in row["evidence"] for row in old["decisions"]):
                    raise ValueError(f"{work_key} requires newly frozen recovery evidence")
        added = []
        if action == "load":
            plan = read_json(words(decision.get("plan"), "plan"), args.repos,
                             evidence_refs, project_path)
            if (not isinstance(plan, dict) or type(plan.get("version")) is not int
                    or plan["version"] != 1 or not isinstance(plan.get("tasks"), list)):
                raise ValueError(f"invalid version 1 plan for {work_key}")
            definitions = [definition(raw) for raw in plan["tasks"]]
            selected = [task for task in definitions if task["id"] in ids and task["goal"] == goal]
            if sorted(task["id"] for task in selected) != ids:
                raise ValueError(f"{work_key} plan does not define the selected tasks in {goal}")
            added = add_tasks(db, selected, args.session)
            if added:
                board = Board(db)
        elif action == "wait_external":
            words(decision.get("resume_when"), "resume_when")
        elif action == "needs_decision":
            words(decision.get("decision_needed"), "decision_needed")
        tasks = [board.view(board.get(tid)) for tid in (ids or bound)]
        if any(t["goal"] != goal for t in tasks):
            raise ValueError(f"{work_key} points outside {goal}")
        recorded = record_decision(db, args, decision, ids, dump)
        report.append(dict(work_key=work_key, goal=goal, action=action, added=added,
                           recorded=recorded, task_ids=ids, tasks=tasks, deferred_on=[]))
    return report


def delivery(db, args, *, Board, events, key, log, take, transition):
    board = Board(db)
    task = board.view(board.get(args.id))
    selection = Namespace(id=args.id)
    history = events(db, selection)
    tickets = [event for event in history if event["action"] == "dispatch"]
    ticket = tickets[-1] if tickets else None
    if args.command == "dispatch":
        key(args.to, "to")
        identity = (key(args.host, "host"), task["goal"], key(args.work_key, "work_key"))
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='continuation_decision'").fetchone()
        bindings = db.execute(
            """SELECT task_ids FROM continuation_decision
               WHERE host=? AND goal=? AND work_key=?""", identity,
        ) if table else ()
        if not any(task["id"] in json.loads(row["task_ids"]) for row in bindings):
            raise ValueError("task is not bound to this successor work key")
        if args.rev != task["rev"]:
            raise ValueError(f"stale revision {args.rev}, current is {task['rev']}; inspect show")
        if ticket:
            after = [event for event in history if event["seq"] > ticket["seq"]]
            started = next((event for event in after if event["action"] == "start"
                            and event["data"]["dispatch"] == ticket["seq"]
                            and event["rev"] == task["rev"]), None)
            if started:
                return dict(status="started", task=task, dispatch=ticket, start=started)
            if task["state"] == "active" and task["owner"] == ticket["data"]["to"]:
                claimed = next((event for event in after if event["action"] == "claim"
                                and event["actor"] == task["owner"] and event["rev"] == task["rev"]), None)
                return dict(status="started" if claimed else "awaiting_confirmation",
                            task=task, dispatch=ticket, start=claimed)
        if task["state"] != "ready":
            return dict(status=task["state"], task=task)
        if ticket and ticket["rev"] == task["rev"]:
            return dict(status="pending_delivery", task=task, dispatch=ticket, recorded=False)
        take(board, task, args.to)
        log(db, task["id"], task["rev"], "dispatch", args.session, to=args.to,
            host=args.host, work_key=args.work_key, goal=task["goal"], needs=task["needs"])
        return dict(status="pending_delivery", task=task, dispatch=events(db, selection)[-1], recorded=True)

    if not ticket or ticket["seq"] != args.dispatch or ticket["data"]["to"] != args.session:
        raise ValueError("dispatch is obsolete or does not address this task and session")
    existing = next((event for event in history if event["action"] == "start"
                     and event["data"]["dispatch"] == args.dispatch), None)
    if existing:
        return dict(status="already_started", task=task, start=existing)
    if task["state"] == "ready" and task["rev"] == ticket["rev"]:
        take(board, task, args.session)
        transition(db, task, "active", args.session, "claim", args.session, task["note"])
        board = Board(db)
        task = board.view(board.get(args.id))
    if task["state"] != "active" or task["owner"] != args.session:
        raise ValueError("dispatch no longer matches the queue; reconcile before starting")
    activation = next((event for event in reversed(events(db, selection))
                       if event["action"] in ("claim", "handoff", "recover")), None)
    if (not activation or activation["seq"] <= ticket["seq"] or
            any(event["seq"] > ticket["seq"] and event["action"] in
                ("release", "block", "unblock", "done", "revise") for event in history)):
        raise ValueError("active assignment predates this dispatch")
    log(db, args.id, task["rev"], "start", args.session, dispatch=args.dispatch)
    return dict(status="started", task=task, start=events(db, selection)[-1])
