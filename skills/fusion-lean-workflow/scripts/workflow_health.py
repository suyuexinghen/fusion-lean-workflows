"""Read a consistent queue snapshot and benchmark artifacts from pinned local commits."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
import subprocess
from workflow_projects import PROJECTS, project_path


def git(repo, *args):
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args],
                            capture_output=True, encoding="utf-8")
    if result.returncode:
        raise ValueError(f"{repo.name}: {result.stderr.strip()}")
    return result.stdout


def history_digest(events):
    data = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(data.encode()).hexdigest()


def queue_summary(tasks, events, previous):
    cursor = events[-1]["seq"] if events else 0
    goals = {}
    bottlenecks = defaultdict(list)
    for goal in sorted({t["goal"] for t in tasks}):
        batch = [t for t in tasks if t["goal"] == goal]
        states = Counter(t["state"] for t in batch)
        reviews = [t for t in batch if t["role"] == "judge"]
        done = [t for t in batch if t["state"] == "done"]
        roles = {}
        for role in sorted({t["role"] for t in batch}):
            roles[role] = {"done": sum(t["role"] == role for t in done),
                           "planned": sum(t["role"] == role for t in batch)}
        goals[goal] = {
            "total": len(batch), "states": dict(sorted(states.items())),
            "roles": roles,
            "ready": [t["id"] for t in batch if t["state"] == "ready"],
            "active": {t["id"]: t["owner"] for t in batch if t["state"] == "active"},
            "blocked": {t["id"]: t["note"] for t in batch if t["state"] == "blocked"},
            "judge_completed_with_independence_edges": [t["id"] for t in reviews
                                                         if t["state"] == "done" and t["independent_of"]],
            "judge_tasks": [t["id"] for t in reviews],
            "deliveries_by_repo": dict(Counter(repo for t in done for repo in
                                        sorted({r.partition("@")[0] for r in t["evidence"]}))),
        }
        for t in batch:
            if t["state"] == "waiting":
                for holder in set(t["waiting_for"] + t["resource_holders"]):
                    bottlenecks[holder].append(t["id"])
    start = 0
    if previous is not None:
        if previous.get("schema") != "fusion-workflow-health/v1":
            raise ValueError("previous file must be a fusion-workflow-health/v1 report")
        baseline = previous.get("queue", {})
        if not isinstance(baseline, dict):
            raise ValueError("previous report lacks queue data")
        start = baseline.get("through_event")
        if type(start) is not int or not 0 <= start <= cursor:
            raise ValueError("previous report cursor is outside this queue")
        prefix = [e for e in events if e["seq"] <= start]
        if history_digest(prefix) != baseline.get("history_sha256"):
            raise ValueError("previous report belongs to another queue history")
    recent = [e for e in events if e["seq"] > start]
    revisions = [e for e in events if e["action"] == "revise"]
    finished = {t["id"] for t in tasks if t["state"] == "done"}
    return {
        "through_event": cursor, "history_sha256": history_digest(events),
        "after_event": start, "actions_in_window": dict(Counter(e["action"] for e in recent)),
        "goals": goals,
        "waiting_on": dict(sorted(bottlenecks.items(), key=lambda p: (-len(p[1]), p[0]))),
        "repairs": [{"event": e["seq"], "task": e["task"], "fix": e["data"]["fix"],
                     "rechecked": e["task"] in finished and e["data"]["fix"] in finished}
                    for e in revisions],
    }


REVIEW_REGISTRY = "evidence/workflow-trials/review-evidence/reviews.json"
REVIEW_REGISTRY_SCHEMA = "fusion-review-registry/v1"
REVIEW_EVIDENCE_SCHEMA = "fusion-review-evidence/v1"
CONTRACT_REF = re.compile(r"([a-z-]+)@([0-9a-f]{40}|[0-9a-f]{64}):(.+)")


def read_review_registry(repo, revision, path):
    """Read explicitly declared review evidence from the pinned wiki commit.

    A missing registry is an explicit "unregistered" observation. It is never
    treated as proof that no review happened, and Git commits alone never
    create an out-of-queue review entry.
    """
    state = {"path": path, "commit": revision, "status": "unregistered", "detail": None}
    try:
        raw = git(repo, "show", f"{revision}:{path}")
    except ValueError as exc:
        state["detail"] = f"no review registry at {path}: {exc}"
        return None, state
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        state.update(status="invalid", detail=f"invalid JSON: {exc}")
        return None, state
    if not isinstance(data, dict) or data.get("schema") != REVIEW_REGISTRY_SCHEMA:
        state.update(status="invalid", detail=f"expected schema {REVIEW_REGISTRY_SCHEMA}")
        return None, state
    if not isinstance(data.get("reviews", []), list):
        state.update(status="invalid", detail="reviews must be an array")
        return None, state
    state["status"] = "loaded"
    return data, state


def commit_fact(repo, sha):
    """Verify a commit in the wiki repository; never invent a missing one."""
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return {"commit": sha, "status": "invalid"}
    try:
        if git(repo, "cat-file", "-t", sha).strip() != "commit":
            return {"commit": sha, "status": "not-a-commit"}
        line = git(repo, "show", "-s", "--format=%H%x09%ci%x09%s", sha).rstrip("\n")
        full, committed_at, subject = line.split("\t", 2)
        return {"commit": full, "status": "verified", "committed_at": committed_at,
                "subject": subject}
    except ValueError as exc:
        return {"commit": sha, "status": "unknown", "detail": str(exc)}


def parse_contract_ref(spec, projects=PROJECTS):
    """Parse PROJECT@FULL-COMMIT:PATH without guessing an unpinned source."""
    match = CONTRACT_REF.fullmatch(spec.strip()) if isinstance(spec, str) else None
    if not match:
        raise ValueError("expected PROJECT@FULL-COMMIT:PATH")
    repo, sha, path = match.groups()
    if repo not in projects:
        raise ValueError(f"contract repo {repo!r} is not one of {list(projects)}")
    if any(part in ("", ".", "..") for part in path.split("/")) or any(ord(c) < 32 for c in path):
        raise ValueError(f"invalid contract path {path!r}")
    if not path.endswith(".json"):
        raise ValueError("contracts must point at a JSON plan file")
    return repo, sha, path


def validated_contract_tasks(data):
    """Return the task objects that define expected needs and independent_of."""
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("tasks"), list) or not data["tasks"]):
        raise ValueError("expected a version 1 plan with a nonempty tasks array")
    tasks, ids = [], set()
    for raw in data["tasks"]:
        if not isinstance(raw, dict):
            raise ValueError("each contract task must be an object")
        tid = raw.get("id")
        if not isinstance(tid, str) or not tid or tid in ids:
            raise ValueError("contract tasks need unique nonempty string ids")
        needs = raw.get("needs", [])
        independent = raw.get("independent_of", [])
        if (not isinstance(needs, list) or not all(isinstance(n, str) and n for n in needs)
                or not isinstance(independent, list)
                or not all(isinstance(n, str) and n for n in independent)):
            raise ValueError(f"{tid}: needs and independent_of must be string arrays")
        if not set(independent) <= set(needs):
            raise ValueError(f"{tid}: independent_of must be a subset of needs")
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        tasks.append({"id": tid, "needs": list(needs), "independent_of": list(independent),
                      "definition_sha256": sha256(canonical.encode()).hexdigest()})
        ids.add(tid)
    return tasks


def read_contract_source(repos_root, spec, projects):
    """Read one pinned plan file. Failures stay visible as insufficient sources."""
    source = {"source": spec, "status": "unreadable", "detail": None, "repo": None,
              "commit": None, "path": None, "file_sha256": None, "tasks": 0}
    try:
        repo, sha, path = parse_contract_ref(spec, projects)
    except ValueError as exc:
        source.update(status="invalid", detail=str(exc))
        return source, []
    source.update(repo=repo, commit=sha, path=path)
    try:
        raw = git(project_path(repos_root, repo), "show", f"{sha}:{path}")
    except ValueError as exc:
        source["detail"] = str(exc)
        return source, []
    source["file_sha256"] = sha256(raw.encode()).hexdigest()
    try:
        tasks = validated_contract_tasks(json.loads(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        source.update(status="invalid", detail=str(exc))
        return source, []
    source.update(status="loaded", tasks=len(tasks))
    return source, tasks


def read_contracts(repos_root, specs, projects):
    """Read all pinned contracts and merge expected relations by task id."""
    sources, parsed = [], []
    for spec in specs or []:
        source, tasks = read_contract_source(repos_root, spec, projects)
        sources.append(source)
        if source["status"] == "loaded":
            parsed.append((source, tasks))
    owners, conflicts = {}, set()
    for source, tasks in parsed:
        for task in tasks:
            previous = owners.get(task["id"])
            if previous and previous[1] != task["definition_sha256"]:
                conflicts.update((previous[0], id(source)))
            else:
                owners[task["id"]] = (id(source), task["definition_sha256"])
    for source, _ in parsed:
        if id(source) in conflicts:
            source.update(status="conflict",
                          detail="the same task id has different definitions across contracts")
    expected = {}
    for source, tasks in parsed:
        if id(source) in conflicts:
            continue
        for task in tasks:
            entry = expected.setdefault(task["id"], {
                "needs": set(), "independent_of": set(), "sources": [],
                "definition_sha256": task["definition_sha256"]})
            entry["needs"].update(task["needs"])
            entry["independent_of"].update(task["independent_of"])
            entry["sources"].append(source["source"])
    return sources, expected


def review_summary(repo, revision, tasks, events, path=None, contracts=None,
                   repos_root=None, projects=PROJECTS):
    """Report queued review claims, declared revision edges and disclosure scope.

    Queue facts are derived from task views and events. Facts that the queue
    cannot record (out-of-queue reviews, declared scope, reports sharing one
    session name, participant disclosures) come only from the explicit registry.
    """
    path = path or REVIEW_REGISTRY
    registry, registry_state = read_review_registry(repo, revision, path)
    errors, entries = [], {}
    for index, entry in enumerate((registry or {}).get("reviews", [])):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            errors.append(f"reviews[{index}] lacks a string id")
        elif entry["id"] in entries:
            errors.append(f"duplicate review id {entry['id']}")
        else:
            entries[entry["id"]] = entry
    identities = registry.get("identity_ambiguities", []) if registry else []
    if not isinstance(identities, list):
        errors.append("identity_ambiguities must be an array")
        identities = []
    fixes = {e["data"].get("fix") for e in events
             if e.get("action") == "revise" and isinstance(e.get("data"), dict)
             and isinstance(e["data"].get("fix"), str)}
    fix_event = {}
    for e in events:
        if (e.get("action") == "revise" and isinstance(e.get("data"), dict)
                and isinstance(e["data"].get("fix"), str)):
            fix_event.setdefault(e["data"]["fix"], e.get("seq"))
    task_ids = {t["id"] for t in tasks}
    tasks_by_id = {t["id"]: t for t in tasks}
    contract_specs = [s for s in (contracts or []) if isinstance(s, str)]
    contract_mode = bool(contract_specs)
    contract_sources, expected = [], {}
    if contract_mode:
        contract_sources, expected = read_contracts(
            repos_root or repo.parent, contract_specs, projects)
    missing_contract_tasks = sorted(tid for tid in expected if tid not in task_ids)

    def actual_edge(tid, need):
        """Describe the queue edge without inventing a missing one."""
        if tid not in tasks_by_id or need not in task_ids:
            return "dependency-missing"
        if need not in tasks_by_id[tid].get("needs", []):
            return "edge-missing"
        return need in tasks_by_id[tid].get("independent_of", [])

    def contract_pair(tid, need, required, source, source_ref, definition_sha=None):
        actual = actual_edge(tid, need)
        if actual == "dependency-missing":
            status = "missing-dependency"
        elif required and actual is not True:
            status = "missing-independence-edge"
        else:
            status = "covered" if required else "ordinary"
        return {"task": tid,
                "role": tasks_by_id[tid].get("role") if tid in tasks_by_id else None,
                "need": need, "expected": "independent" if required else "ordinary",
                "source": source, "source_ref": source_ref,
                "contract_task_sha256": definition_sha,
                "actual_independent": actual, "status": status}

    contract_checks = []
    reviews, missing_edges, ordinary = [], [], set()
    # Judge tasks whose independence is not declared at the task level used to
    # vanish silently from judge_completed_with_independence_edges.  Listing
    # them here makes the omission checkable; covered_elsewhere marks the
    # second class (same-verdict-family re-review whose independence is
    # declared in the registry or a contract instead of the task definition).
    missing_declarations = []
    # Legacy judge reviews stay visible. With an explicit contract, tasks of any
    # role are audited as soon as the pinned plan or a revise event names a
    # required relation; tasks with neither are listed as no-applicable.
    reviewed_ids = {t["id"] for t in tasks if t.get("role") == "judge"}
    if contract_mode:
        reviewed_ids |= {tid for tid in expected if tid in task_ids}
        reviewed_ids |= {t["id"] for t in tasks if set(t.get("needs", [])) & fixes}
        reviewed_ids |= set(entries) & task_ids
    for tid in sorted(reviewed_ids):
        t = tasks_by_id[tid]
        entry = entries.get(tid, {})
        needs = list(t.get("needs", []))
        independent = list(t.get("independent_of", []))
        entry_missing = []
        declared = entry.get("revision_prerequisites", [])
        if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
            entry_missing.append("revision_prerequisites")
            declared = []
        derived = sorted(set(needs) & fixes)
        declared_used = sorted(set(declared) & set(needs))
        not_needs = sorted(set(declared) - set(needs))
        prerequisites = sorted(set(derived) | set(declared_used))
        missing = sorted(set(prerequisites) - set(independent))
        contract = expected.get(tid) if contract_mode else None
        contract_independent = sorted(contract["independent_of"]) if contract else []
        contract_needs = sorted(contract["needs"]) if contract else []
        contract_ordinary = sorted(set(contract_needs) - set(contract_independent))
        task_pairs = []
        if contract:
            for need in contract_needs:
                task_pairs.append(contract_pair(
                    tid, need, need in contract["independent_of"], "plan",
                    ", ".join(contract["sources"]), contract["definition_sha256"]))
        if contract_mode:
            for need in derived:
                task_pairs.append(contract_pair(
                    tid, need, True, "revise", f"queue revise event {fix_event.get(need)}"))
            for need in declared_used:
                task_pairs.append(contract_pair(
                    tid, need, True, "declared", f"fusion-lean-wiki@{revision}:{path}"))
        contract_checks.extend(task_pairs)
        contract_missing = sorted(p["need"] for p in task_pairs
                                  if p["source"] == "plan"
                                  and p["status"] == "missing-independence-edge")
        revise_missing = sorted(p["need"] for p in task_pairs
                                if p["source"] != "plan"
                                and p["status"] == "missing-independence-edge")
        missing_dependencies = sorted({p["need"] for p in task_pairs
                                       if p["status"] == "missing-dependency"})
        union_missing = sorted(set(missing) | set(contract_missing) | set(revise_missing))
        if missing_dependencies:
            independence_status = "missing-dependency"
        elif union_missing:
            independence_status = "missing-independence-edge"
        elif contract_independent:
            independence_status = "covered"
        elif contract_needs:
            independence_status = "ordinary"
        elif prerequisites:
            independence_status = "covered"
        else:
            independence_status = "no-applicable"
        claims = [e for e in events if e.get("task") == tid and e.get("action") == "claim"]
        done = [e for e in events if e.get("task") == tid and e.get("action") == "done"]
        frozen = None
        if done:
            frozen = {"time": done[-1].get("time"),
                      "source": f"queue done event {done[-1].get('seq')}",
                      "precision": "queue event time; not the instant the answer text was finished"}
        status = "incomplete" if entry_missing or not_needs else (
            "complete" if tid in entries else "derived-only")
        review = {
            "id": tid, "kind": "queue", "state": t.get("state"), "role": t.get("role"),
            "registered": tid in entries, "registration_status": status,
            "registration_missing": entry_missing,
            "claimed_by": ({"session": claims[-1].get("actor") or claims[-1].get("owner"),
                            "event": claims[-1].get("seq"), "revision": claims[-1].get("rev")}
                           if claims else None),
            "claim_count": len(claims), "claim_events": [e.get("seq") for e in claims],
            "needs": needs, "independent_of": independent,
            "revision_prerequisites": prerequisites,
            "revision_prerequisites_source": sorted(
                (["queue-revise"] if derived else []) + (["declared"] if declared_used else [])),
            "declared_revision_prerequisites_not_in_needs": not_needs,
            "edge_status": ("missing" if union_missing or missing_dependencies else
                            "covered" if prerequisites or contract_independent else "not-applicable"),
            "missing_independence_edges": union_missing,
            "answer_frozen": frozen,
            "declared_answer_frozen_at": entry.get("answer_frozen_at"),
        }
        if contract_mode:
            review.update({
                "contracts": contract["sources"] if contract else [],
                "contract_prerequisites": contract_independent,
                "contract_ordinary_prerequisites": contract_ordinary,
                "contract_missing_independence_edges": contract_missing,
                "revise_missing_independence_edges": revise_missing,
                "missing_dependencies": missing_dependencies,
                "independence_status": independence_status,
                "contract_task_sha256": contract["definition_sha256"] if contract else None})
        if entry.get("answer_frozen_at") is not None and not isinstance(entry["answer_frozen_at"], str):
            review["registration_status"] = "incomplete"
            review["registration_missing"] = sorted(entry_missing + ["answer_frozen_at"])
        reviews.append(review)
        if t.get("role") == "judge" and not independent:
            missing_declarations.append({
                "task": tid, "state": t.get("state"), "needs": needs,
                "registry_entry": tid in entries,
                "covered_elsewhere": bool(prerequisites) or bool(contract_independent),
                "note": "role=judge without independent_of: not counted in "
                        "judge_completed_with_independence_edges and edge_status "
                        "stays not-applicable unless declared elsewhere"})
        if union_missing or missing_dependencies:
            item = {"review": tid, "state": t.get("state"),
                    "revision_prerequisites": prerequisites, "missing": missing,
                    "source": review["revision_prerequisites_source"]}
            if contract_mode:
                item.update({
                    "contract_prerequisites": contract_independent,
                    "contract_missing": contract_missing,
                    "missing_independence_edges": union_missing,
                    "missing_dependencies": missing_dependencies,
                    "status": ("missing-dependency" if missing_dependencies
                               else "missing-independence-edge")})
            missing_edges.append(item)
        if contract:
            for need in needs:
                if need not in contract_independent and need not in prerequisites:
                    ordinary.add(need)
        else:
            for need in needs:
                if need not in prerequisites and need not in independent:
                    ordinary.add(need)

    if not contract_mode:
        contract_summary = None
    else:
        independent_pairs = [p for p in contract_checks if p["expected"] == "independent"]
        missing_independent_pairs = [p for p in contract_checks
                                     if p["status"] == "missing-independence-edge"]
        missing_dependent_pairs = [p for p in contract_checks
                                   if p["status"] == "missing-dependency"]
        ordinary_pairs = [p for p in contract_checks if p["status"] == "ordinary"]
        failed_sources = [s for s in contract_sources if s["status"] != "loaded"]
        # The pinned plan decides the contract status. Revise-derived and
        # registry-declared history remains visible in audit_status and in the
        # pair-level missing lists rather than silently passing or failing.
        plan_pairs = [p for p in contract_checks if p["source"] == "plan"]
        plan_missing = [p for p in missing_independent_pairs if p["source"] == "plan"]
        plan_deps = [p for p in missing_dependent_pairs if p["source"] == "plan"]
        if failed_sources:
            contract_status = "insufficient-source"
        elif plan_deps or missing_contract_tasks:
            contract_status = "missing-dependency"
        elif plan_missing:
            contract_status = "missing-independence-edge"
        elif any(p["expected"] == "independent" for p in plan_pairs):
            contract_status = "covered"
        elif plan_pairs:
            contract_status = "ordinary"
        else:
            contract_status = "no-applicable"
        if failed_sources:
            audit_status = "insufficient-source"
        elif missing_dependent_pairs or missing_contract_tasks:
            audit_status = "missing-dependency"
        elif missing_independent_pairs:
            audit_status = "missing-independence-edge"
        elif independent_pairs:
            audit_status = "covered"
        elif ordinary_pairs:
            audit_status = "ordinary"
        else:
            audit_status = "no-applicable"
        def source_view(kind):
            pairs = [p for p in contract_checks
                     if p["source"] == kind and p["expected"] == "independent"]
            absent = [p for p in pairs if p["status"] == "missing-dependency"]
            broken = [p for p in pairs if p["status"] == "missing-independence-edge"]
            return {"independent_pairs": len(pairs),
                    "covered": sum(p["status"] == "covered" for p in pairs),
                    "missing_independence_edges": broken,
                    "missing_dependencies": absent,
                    "status": ("missing-dependency" if absent else
                               "missing-independence-edge" if broken else
                               "covered" if pairs else "not-applicable")}
        contract_summary = {
            "enabled": True,
            "complete": (not failed_sources and not plan_missing and not plan_deps
                         and not missing_contract_tasks),
            "audit_complete": (not failed_sources and not missing_independent_pairs
                               and not missing_dependent_pairs
                               and not missing_contract_tasks),
            "status": contract_status,
            "audit_status": audit_status,
            "by_source": {kind: source_view(kind)
                          for kind in ("plan", "revise", "declared")},
            "sources_loaded": len(contract_sources) - len(failed_sources),
            "sources_failed": len(failed_sources),
            "insufficient_sources": [{"source": s["source"], "status": s["status"],
                                      "detail": s["detail"]} for s in failed_sources],
            "tasks_in_contracts": len(expected),
            "missing_contract_tasks": missing_contract_tasks,
            "audited_roles": sorted({tasks_by_id[tid].get("role") for tid in reviewed_ids}),
            "independent_pairs": len(independent_pairs),
            "covered_independent_pairs": sum(p["status"] == "covered"
                                             for p in independent_pairs),
            "ordinary_pairs": len(ordinary_pairs),
            "missing_independence_edges": missing_independent_pairs,
            "missing_dependencies": missing_dependent_pairs,
            "no_applicable_tasks": sorted(task_ids - reviewed_ids)}
    out_of_queue = []
    for entry in (registry or {}).get("reviews", []):
        if not isinstance(entry, dict) or entry.get("kind") != "out-of-queue":
            continue
        rid = entry.get("id")
        missing_fields = [] if isinstance(rid, str) and rid else ["id"]
        rid = rid if isinstance(rid, str) and rid else "<unidentified>"
        session = entry.get("session")
        if not isinstance(session, str) or not session:
            missing_fields.append("session")
            session = None
        commits = entry.get("commits")
        if not isinstance(commits, list) or not commits:
            missing_fields.append("commits")
            commits = []
        facts = []
        for item in commits:
            sha = item.get("commit") if isinstance(item, dict) else item
            fact = commit_fact(repo, sha)
            if isinstance(item, dict):
                for key in ("role", "path"):
                    if isinstance(item.get(key), str):
                        fact[key] = item[key]
            facts.append(fact)
        scope = entry.get("scope")
        scope_view = None
        if not isinstance(scope, dict):
            missing_fields.append("scope")
        else:
            scope_missing = [k for k in ("countable_subquestions", "total_subquestions")
                             if type(scope.get(k)) is not int]
            excluded = scope.get("excluded_subquestions", scope.get("excluded", []))
            scope_view = {"countable_subquestions": scope.get("countable_subquestions"),
                          "total_subquestions": scope.get("total_subquestions"),
                          "excluded_subquestions": excluded if isinstance(excluded, list) else [],
                          "source": scope.get("source"),
                          "status": "incomplete" if scope_missing else "declared",
                          "missing": scope_missing}
        disclosures = entry.get("disclosures")
        if not isinstance(disclosures, list):
            missing_fields.append("disclosures")
            disclosures = []
        disclosure_view = []
        for d in disclosures:
            if not isinstance(d, dict):
                missing_fields.append("disclosures[]")
                continue
            sources = d.get("sources", d.get("source", []))
            disclosure_view.append({
                "item": d.get("item"), "kind": d.get("kind", "over-read"),
                "status": "declared", "detected": False, "detection": "not-observable",
                "sources": sources, "consequence": d.get("consequence")})
        in_queue = rid in task_ids
        claims = [e for e in events if e.get("task") == rid and e.get("action") == "claim"]
        session_events = None
        if isinstance(session, str):
            session_events = len([e for e in events if session in
                                  (e.get("actor"), e.get("owner"), e.get("previous_owner"))])
        inconsistencies = []
        if in_queue:
            inconsistencies.append("registry declares out-of-queue but a queue task exists")
        if claims:
            inconsistencies.append("registry declares no claim but queue claim events exist")
        if session_events:
            inconsistencies.append("registry session has queue events")
        answer = next((f for f in facts if f.get("role") == "blind-answer"
                       and f.get("status") == "verified"), None)
        review = next((f for f in facts if f.get("role") == "review"
                       and f.get("status") == "verified"), None)
        out_of_queue.append({
            "id": rid, "kind": "out-of-queue",
            "registration_missing": sorted(set(missing_fields)),
            "answer_frozen": ({"commit": answer["commit"], "time": answer["committed_at"],
                               "source": "Git commit time from the registry; not the instant "
                                         "the answer text was finished"} if answer else None),
            "review_frozen_at": ({"commit": review["commit"], "time": review["committed_at"],
                                  "source": "Git commit time from the registry"} if review else None),
            "registration_status": ("incomplete" if missing_fields else
                                    "conflicting" if inconsistencies else "complete"),
            "inconsistencies": inconsistencies,
            "task_in_queue": in_queue, "session": session,
            "queue_claims": [{"event": e.get("seq"), "actor": e.get("actor")} for e in claims],
            "claim_status": "present" if claims or in_queue else "none",
            "session_events": session_events,
            "commits": facts, "scope": scope_view, "disclosures": disclosure_view,
            "note": ("Git-visible only; the queue records no claim for this review"
                     if not (claims or in_queue) else "conflicts with queue state; inspect")})
    identity_view = []
    for entry in identities:
        if not isinstance(entry, dict):
            errors.append("identity_ambiguities entry is not an object")
            continue
        session, task = entry.get("session"), entry.get("task")
        reports = entry.get("reports", [])
        if not isinstance(reports, list):
            reports = []
        claims = ([e for e in events if e.get("task") == task and e.get("action") == "claim"]
                  if isinstance(task, str) else [])
        report_facts = [commit_fact(repo, r.get("commit") if isinstance(r, dict) else r)
                        for r in reports]
        missing = [k for k in ("session", "task", "reports") if not entry.get(k)]
        if len(reports) < 2:
            missing.append("multiple_reports")
        collisions = entry.get("collision_record")
        collision = commit_fact(repo, collisions) if isinstance(collisions, str) else None
        status = "identity-unverified"
        if isinstance(task, str) and task not in task_ids:
            status = "unknown-task"
        elif missing:
            status = "incomplete-registration"
        identity_view.append({
            "session": session, "task": task, "status": status,
            "registration_missing": sorted(set(missing)),
            "claim_count": len(claims), "claim_events": [e.get("seq") for e in claims],
            "report_count": len(report_facts), "reports": report_facts,
            "collision_record": collision,
            "reason": ("the queue stores session strings only; one claim and multiple reports "
                       "cannot identify the OS processes behind the name")})
    unknowns = []
    if registry_state["status"] != "loaded":
        unknowns.append("no explicit review registry: out-of-queue reviews and declared "
                        "scope are not reported")
    for item in (registry or {}).get("unknowns", []) if registry else []:
        if isinstance(item, str):
            unknowns.append(item)
    if any(review["claim_count"] > 1 for review in reviews):
        unknowns.append("a session may re-claim; the queue counts claims, not processes")
    if any(item["disclosures"] for item in out_of_queue):
        unknowns.append("file reads beyond the participant disclosure are not observable "
                        "in the queue or Git")
    return {
        "schema": REVIEW_EVIDENCE_SCHEMA, "registry": registry_state,
        "provenance": {
            "derived": ["queue task state, needs and independent_of edges",
                        "queue claim, done and revise events",
                        "Git commit existence, time and subject"],
            "declared": ["reviews[].revision_prerequisites for fixes added outside a revise event",
                         "reviews[].scope", "reviews[].disclosures",
                         "identity_ambiguities[].reports", "unknowns"],
            "not_observable": ["OS process identity behind a session string",
                               "file reads not disclosed by a participant",
                               "the instant an answer text was finished; only queue or commit "
                               "times exist"]},
        "reviews": reviews, "missing_independence_edges": missing_edges,
        "missing_declarations": missing_declarations,
        "ordinary_prerequisites_not_flagged": sorted(ordinary),
        "contracts": contract_sources, "contract_checks": contract_checks,
        "contract_summary": contract_summary,
        "out_of_queue": out_of_queue, "identity_ambiguities": identity_view,
        "registry_errors": sorted(errors), "unknowns": sorted(set(unknowns))}


def read_run(repo, revision, path, mode):
    run = json.loads(git(repo, "show", f"{revision}:{path}"))
    checks = run.get("checks") if isinstance(run, dict) else None
    if not checks or not isinstance(checks, list) or run.get("mode") != mode:
        raise ValueError(f"{path}: expected nonempty {mode} checks")
    counts = Counter()
    issues, ids = set(), set()
    for c in checks:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] or c["id"] in ids:
            raise ValueError(f"{path}: invalid or duplicate check ID")
        ids.add(c["id"])
        status = c.get("status")
        if status not in ("PASS", "FAIL", "UNRESOLVED"):
            raise ValueError(f"{path}: unrecognized check status")
        counts[status] += 1
        if status != "PASS":
            evidence = c.get("evidence")
            reason = evidence.get("reason", "") if isinstance(evidence, dict) else ""
            if c["id"] == "environment" and ".olean' of module " in str(reason) and "does not exist" in str(reason):
                issues.add("environment/missing-olean")
            else:
                issues.add(f"{status}/{c['id']}")
    target = run.get("target")
    candidate = target.get("head", "") if isinstance(target, dict) and target.get("mode") == "commit" else ""
    if not isinstance(candidate, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", candidate):
        raise ValueError(f"{path}: missing pinned Lean target")
    suite = run.get("suite_sha256")
    if not isinstance(suite, str) or not re.fullmatch(r"[0-9a-f]{64}", suite):
        raise ValueError(f"{path}: missing evaluator digest")
    if dict(counts) != run.get("summary"):
        raise ValueError(f"{path}: summary differs from checks")
    return {"file": path, "candidate": candidate, "suite_sha256": suite, "counts": dict(counts),
            "check_ids": sorted(ids), "issues": sorted(issues),
            "nonpass_ids": sorted(c["id"] for c in checks if c["status"] != "PASS")}


TRIAL_SCHEMA = "fusion-improvement-trial/v1"
ISSUE_CLASS = re.compile(r"(environment/missing-olean|(FAIL|UNRESOLVED)/[A-Za-z0-9._-]+)")


def read_trials(repo, revision, rounds):
    """Read promoted-improvement declarations. Malformed claims stay visible as rejected."""
    names = git(repo, "ls-tree", "-r", "--name-only", revision, "--", "evidence/workflow-trials").splitlines()
    trials = []
    for name in names:
        path = PurePosixPath(name)
        if len(path.parts) != 4 or path.name != "trial.json":
            continue
        folder = str(path.parent)
        declared = {"declared_in": folder}
        try:
            data = json.loads(git(repo, "show", f"{revision}:{name}"))
            if not isinstance(data, dict) or data.get("schema") != TRIAL_SCHEMA:
                raise ValueError("missing or unsupported schema")
            mechanism = data.get("mechanism")
            if not isinstance(mechanism, str) or not mechanism.strip():
                raise ValueError("missing mechanism")
            addresses = data.get("addresses")
            if (not isinstance(addresses, list) or not addresses
                    or not all(isinstance(a, str) and ISSUE_CLASS.fullmatch(a) for a in addresses)):
                raise ValueError("addresses must name observed issue classes")
            if data.get("verdict") != "promoted":
                raise ValueError(f"verdict is {data.get('verdict')!r}, not 'promoted'")
            conditions = data.get("conditions")
            if (not isinstance(conditions, dict) or not conditions
                    or not all(value is True for value in conditions.values())):
                raise ValueError("conditions are not all true")
            target = data.get("trial_folder")
            if not isinstance(target, str) or target not in rounds:
                raise ValueError("trial_folder is not a readable benchmark round")
            trial = rounds[target]
            if trial["audit"]["issues"] or trial["controls"]["issues"]:
                raise ValueError("trial audit or controls contain non-PASS checks")
            promotion = data.get("promotion_record")
            if not isinstance(promotion, str) or PurePosixPath(promotion).parts[:1] != ("evidence",):
                raise ValueError("promotion_record is missing or not under evidence/")
            git(repo, "cat-file", "-e", f"{revision}:{promotion}")
            control_path = data.get("negative_control")
            if not isinstance(control_path, str) or PurePosixPath(control_path).parts[:1] != ("evidence",):
                raise ValueError("negative_control is missing or not under evidence/")
            negative = read_run(repo, revision, control_path, "audit")
            if negative["candidate"] != trial["audit"]["candidate"]:
                raise ValueError("negative control ran on another candidate")
            if negative["suite_sha256"] != trial["audit"]["suite_sha256"]:
                raise ValueError("negative control used another evaluator")
            if not negative["nonpass_ids"]:
                raise ValueError("negative control has no failing check")
            declared.update(mechanism=mechanism, addresses=sorted(set(addresses)), trial_folder=target,
                            promotion_record=promotion,
                            candidate=trial["audit"]["candidate"], suite_sha256=trial["audit"]["suite_sha256"],
                            audit_check_ids=trial["audit"]["check_ids"],
                            controls_check_ids=trial["controls"]["check_ids"],
                            negative_control=control_path,
                            negative_nonpass_ids=negative["nonpass_ids"], status="accepted")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            declared.update(status="rejected", reason=str(exc))
        trials.append(declared)
    return trials


def benchmark_summary(repo, revision):
    names = git(repo, "ls-tree", "-r", "--name-only", revision, "--", "evidence/rounds").splitlines()
    folders = defaultdict(set)
    for name in names:
        p = PurePosixPath(name)
        if len(p.parts) == 4:
            folders[str(p.parent)].add(p.name)
    rounds, failures = [], defaultdict(set)
    for folder, files in sorted(folders.items()):
        packet = {"folder": folder}
        rounds.append(packet)
        try:
            if "round.json" not in files:
                raise ValueError("missing round.json")
            paired = bool({"round-before.json", "round-after.json"} & files)
            audit = read_run(repo, revision, f"{folder}/" + ("round-after.json" if paired else "audit.json"), "audit")
            packet["audit"] = audit
            ctrl = "controls-selftest.json" if "controls-selftest.json" in files else "controls.json"
            controls = read_run(repo, revision, f"{folder}/{ctrl}", "controls")
            packet["controls"] = controls
            if (audit["candidate"], audit["suite_sha256"]) != (controls["candidate"], controls["suite_sha256"]):
                raise ValueError("audit and controls refer to different candidates or evaluators")
            if paired:
                before = read_run(repo, revision, folder + "/round-before.json", "audit")
                packet["before"] = before
                if before["candidate"] != audit["candidate"]:
                    raise ValueError("before and after refer to different candidates")
                missing_checks = set(before["check_ids"]) - set(audit["check_ids"]) - {"environment"}
                if missing_checks:
                    raise ValueError(f"after audit omits prior checks: {sorted(missing_checks)}")
                if before["issues"] and not audit["issues"] and not controls["issues"]:
                    packet["result"] = "recovered_on_candidate"
                else:
                    packet["result"] = "paired_observation"
            else:
                packet["result"] = "observation"
            for run in (packet.get("before", audit), audit):
                for issue in run["issues"]:
                    failures[issue].add(run["candidate"])
        except (ValueError, TypeError) as exc:
            packet.update(result="insufficient_evidence", detail=str(exc))
    repeated = {issue: sorted(shas) for issue, shas in sorted(failures.items()) if len(shas) > 1}
    recovered = [r for r in rounds if r["result"] == "recovered_on_candidate"]
    # A distinct candidate already exercised with exactly the repaired evaluator and check IDs.
    for r in recovered:
        audit = r["audit"]
        verified = []
        for other in rounds:
            if other["result"] == "insufficient_evidence":
                continue
            trial = other["audit"]
            if (trial["candidate"] != audit["candidate"] and trial["suite_sha256"] == audit["suite_sha256"]
                    and trial["check_ids"] == audit["check_ids"] and not trial["issues"]
                    and other["controls"]["check_ids"] == r["controls"]["check_ids"]
                    and not other["controls"]["issues"]):
                verified.append(other["folder"])
        r["other_passing_candidates_with_same_evaluator"] = sorted(verified)
    # A promoted improvement may change the evaluator on purpose, so an identical
    # evaluator digest cannot be required. Accept a declared trial only when the
    # test set shrank in neither audit nor controls, the candidate is distinct,
    # and the declaration covers every issue the recovered round started from.
    readable = {r["folder"]: r for r in rounds if r["result"] != "insufficient_evidence"}
    declared = read_trials(repo, revision, readable)
    for r in recovered:
        covering = []
        for t in declared:
            if t["status"] != "accepted" or not r["before"]["issues"]:
                continue
            if not set(r["before"]["issues"]) <= set(t["addresses"]):
                continue
            if t["candidate"] == r["audit"]["candidate"]:
                continue
            if t["suite_sha256"] == r["audit"]["suite_sha256"]:
                continue
            if not set(r["audit"]["check_ids"]) <= set(t["audit_check_ids"]):
                continue
            if not set(r["controls"]["check_ids"]) <= set(t["controls_check_ids"]):
                continue
            if not set(t["negative_nonpass_ids"]) & set(r["before"]["nonpass_ids"]):
                continue
            covering.append(t["trial_folder"])
        if covering:
            r["superseded_by_promotion"] = sorted(set(covering))
    superseded = sum(bool(r.get("superseded_by_promotion")) for r in recovered)
    # Follow-up is decided per recovered round: either kind of evidence closes
    # that round, and two kinds on one round cannot cover another round that has
    # neither. Summing the counters first would hide that case.
    needing = sorted(r["folder"] for r in recovered
                     if not r["other_passing_candidates_with_same_evaluator"]
                     and not r.get("superseded_by_promotion"))
    for r in rounds:  # keep the public packet shape stable across snapshots
        for kind in ("audit", "controls", "before"):
            if kind in r:
                r[kind].pop("nonpass_ids", None)
    return {"rounds": rounds, "improvement_trials": declared,
            "repeated_on_distinct_candidates": repeated,
            "same_candidate_recoveries": len(recovered),
            "recoveries_with_cross_candidate_replay": sum(
                bool(r["other_passing_candidates_with_same_evaluator"]) for r in recovered),
            "recoveries_superseded_by_promotion": superseded,
            "recoveries_needing_followup": needing}


def report(tasks, events, repos, projects, previous, review_registry=None, contracts=None):
    heads = {}
    for name in projects:
        if name == "fusion-compute" and not (repos / name).exists():
            continue  # Older workspaces have only the three original checkouts.
        heads[name] = git(project_path(repos, name), "rev-parse", "--verify", "HEAD^{commit}").strip()
    queue = queue_summary(tasks, events, previous)
    bench = benchmark_summary(repos / "fusion-lean-wiki", heads["fusion-lean-wiki"])
    reviews = review_summary(repos / "fusion-lean-wiki", heads["fusion-lean-wiki"],
                             tasks, events, review_registry, contracts, repos, projects)
    attention = []
    if not bench["rounds"] or any(r["result"] == "insufficient_evidence" for r in bench["rounds"]):
        attention.append("missing_or_incomparable_benchmark_evidence")
    if bench["recoveries_needing_followup"]:
        attention.append("cross_candidate_replay_needed")
    if bench["repeated_on_distinct_candidates"]:
        attention.append("recurrent_benchmark_failure")
    for item in bench["rounds"]:
        if any(item.get(kind, {}).get("issues") for kind in ("audit", "controls")):
            attention.append("inspect_recorded_failed_or_unresolved_checks")
            break
    for goal, g in queue["goals"].items():
        if g["states"].get("done", 0) < g["total"] and not (g["ready"] or g["active"]):
            attention.append("no_runnable_work:" + goal)
    if reviews.get("missing_declarations"):
        attention.append("judge_independence_undeclared")
    comparison = None
    if previous is not None:
        prior_bench = previous.get("benchmarks")
        if not isinstance(prior_bench, dict) or not isinstance(prior_bench.get("rounds"), list):
            raise ValueError("previous report lacks benchmark observations")
        if not isinstance(previous.get("heads"), dict):
            raise ValueError("previous report lacks repository revisions")
        old = {}
        for item in prior_bench["rounds"]:
            if not isinstance(item, dict) or not isinstance(item.get("folder"), str):
                raise ValueError("invalid benchmark observation in previous report")
            old[item["folder"]] = item
        new = {r["folder"]: r for r in bench["rounds"]}
        old_goals = previous["queue"].get("goals", {})
        if not isinstance(old_goals, dict):
            raise ValueError("invalid goal counts in previous report")
        ready_before = set()
        for g in old_goals.values():
            if not isinstance(g, dict) or not isinstance(g.get("ready"), list) or not all(
                    isinstance(tid, str) for tid in g["ready"]):
                raise ValueError("invalid ready IDs in previous report")
            ready_before.update(g["ready"])
        ready_now = {tid for g in queue["goals"].values() for tid in g["ready"]}
        comparison = {"previous_heads": previous["heads"],
                      "ready_at_both_observations": sorted(ready_before & ready_now),
                      "new_rounds": sorted(new.keys() - old.keys()),
                      "changed_rounds": sorted(k for k in new.keys() & old.keys() if new[k] != old[k]),
                      "removed_rounds": sorted(old.keys() - new.keys())}
    result = {"schema": "fusion-workflow-health/v1", "observed_at": datetime.now(timezone.utc).isoformat(),
            "heads": heads, "queue": queue, "benchmarks": bench,
            "review_evidence": reviews, "comparison": comparison, "attention": attention}
    if "fusion-compute" in heads:
        revision = heads["fusion-compute"]
        paths = git(repos / "fusion-compute", "ls-tree", "-r", "--name-only", "-z", revision, "--", "runs")
        result["compute"] = {"head": revision, "run_records": [
            f"fusion-compute@{revision}:{path}" for path in paths.split("\0")
            if re.fullmatch(r"runs/[^/]+/run\.json", path)]}
    return result
