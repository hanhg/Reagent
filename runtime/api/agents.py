"""Agent routes owned by wf/resolver. Contract: contracts/openapi.yaml.

    GET  /v0/agents/{name}/resolved   the resolved view (H1)
    POST /v0/agents/{name}/validate   validate a draft without saving it

`router` is mounted by the runtime's app. Until it is, serve these routes alone:

    python -m runtime.api.agents --port 8000

The acting principal comes ONLY from the X-ReAgent-User header (FROZEN D2).
Visibility is filtered here, server-side; the dashboard renders what it gets.
"""
from __future__ import annotations

import argparse
import copy
import os
import re
from typing import Any

import yaml
from fastapi import APIRouter, Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from contracts.errors import Code, ReAgentError, Rejection
from runtime.resolver import load_catalog, resolve_agent
from runtime.resolver.source import Catalog
from runtime.resolver.validate import validate_draft_text

# ONE router for every wf/resolver route: the runtime app mounts `router` and
# nothing else (runtime/api/runs.py), so a second router would never be served.
router = APIRouter(prefix="/v0", tags=["agents"])


def _principal(cat: Catalog, user: str | None) -> tuple[str, bool, str | None]:
    if not user:
        raise HTTPException(403, "X-ReAgent-User header required")
    is_admin = user in cat.admins()
    team = cat.team_of(user)
    if not is_admin and team is None:
        raise HTTPException(403, f"{user} is not an enterprise admin or a member of any team")
    return user, is_admin, team


def _visible(doc: dict[str, Any], is_admin: bool, team: str | None) -> bool:
    """SPEC.md §2: members see their own team's agents and no enterprise configs."""
    if is_admin:
        return True
    md = doc["metadata"]
    return md.get("level") == "team" and md.get("team") == team


def _db_mode() -> bool:
    return os.environ.get("REAGENT_LAYERS", "files").lower() == "db"


def _measured(names: list[str]) -> dict[str, dict[str, Any]]:
    """Per-agent results from the database: latest CI pass rate, production success
    rate and 30-day cost/volume. Empty outside db mode, or if the query fails: the
    collection still lists agents, with these columns null."""
    if not names or not _db_mode():
        return {}
    import psycopg
    try:
        with psycopg.connect(os.environ.get("DATABASE_URL") or "", connect_timeout=15) as c:
            rows = c.execute("""
                SELECT a.name,
                  (SELECT e.pass_rate FROM eval_runs e JOIN agent_versions v ON v.id = e.agent_version_id
                    WHERE v.agent_id = a.id AND e.kind = 'ci' AND e.pass_rate IS NOT NULL
                    ORDER BY e.created_at DESC LIMIT 1),
                  (SELECT e.pass_rate FROM eval_runs e JOIN agent_versions v ON v.id = e.agent_version_id
                    WHERE v.agent_id = a.id AND e.kind = 'production' AND e.pass_rate IS NOT NULL
                    ORDER BY e.created_at DESC LIMIT 1),
                  r.avg_cost, coalesce(r.n, 0), r.ok, r.done
                FROM agents a
                LEFT JOIN LATERAL (
                  SELECT avg(x.cost_usd) AS avg_cost, count(*) AS n,
                         count(*) FILTER (WHERE x.status = 'succeeded') AS ok,
                         count(*) FILTER (WHERE x.status IN ('succeeded','failed','denied','cancelled')) AS done
                  FROM runs x JOIN agent_versions v ON v.id = x.agent_version_id
                  WHERE v.agent_id = a.id AND x.parent_run_id IS NULL
                    AND x.started_at >= now() - interval '30 days') r ON true
                WHERE a.name = ANY(%s)""", (names,)).fetchall()
            status: dict[str, dict[str, str]] = {}
            for name, version, st in c.execute(
                    "SELECT a.name, v.version, v.status FROM agent_versions v JOIN agents a ON a.id = v.agent_id "
                    "WHERE a.name = ANY(%s)", (names,)):
                status.setdefault(name, {})[version] = st
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for name, ci, prod, avg_cost, n, ok, done in rows:
        out[name] = {"_status": status.get(name, {}), "pass_rate": None if ci is None else float(ci),
                     "production_success_rate": float(prod) if prod is not None
                     else (ok / done if done else None),
                     "avg_cost_usd": None if avg_cost is None else round(float(avg_cost), 6),
                     "runs_30d": int(n)}
    return out


def _drafts() -> dict[str, dict[str, Any]]:
    from runtime.resolver.source import _DRAFTS
    return _DRAFTS


def _versions(cat: Catalog, name: str) -> list[dict[str, Any]]:
    """Version history, newest first. The database knows status and who published;
    the files know only which versions exist."""
    from runtime.resolver.source import _DRAFTS, semver_key
    if _db_mode():
        import psycopg
        try:
            with psycopg.connect(os.environ.get("DATABASE_URL") or "", connect_timeout=15) as c:
                rows = c.execute("""
                    SELECT v.version, v.status, v.published_at, u.name
                    FROM agent_versions v JOIN agents a ON a.id = v.agent_id
                    LEFT JOIN users u ON u.id = v.published_by
                    WHERE a.name = %s""", (name,)).fetchall()
            out = [{"version": v, "status": s, "published_at": at.isoformat() if at else None,
                    "published_by": by} for v, s, at, by in rows]
            return sorted(out, key=lambda r: semver_key(r["version"]), reverse=True)
        except Exception:  # noqa: BLE001
            pass
    draft = _DRAFTS.get(name)
    return [{"version": v, "published_at": None, "published_by": None,
             "status": "draft" if draft is not None and draft is doc else "published"}
            for v, doc in sorted(cat.agent_versions(name).items(), key=lambda kv: semver_key(kv[0]),
                                 reverse=True)]


def _summary(cat: Catalog, doc: dict[str, Any], status: str | None,
             measured: dict[str, Any]) -> dict[str, Any]:
    md, spec = doc["metadata"], doc.get("spec") or {}
    measured = dict(measured)
    status = measured.pop("_status", {}).get(str(md.get("version")), status)
    forked = md.get("forked_from")
    update = False
    if isinstance(forked, str) and "@" in forked:
        from runtime.resolver.source import semver_key
        src_name, src_version = forked.rsplit("@", 1)
        src = cat.latest(src_name)
        update = src is not None and semver_key(str(src["metadata"]["version"])) > semver_key(src_version)
    return {"name": md["name"], "version": str(md.get("version", "")), "status": status,
            "team": md.get("team"), "level": md.get("level", "team"),
            "description": (spec.get("use_case") or {}).get("description", ""),
            "desired_outcome": (spec.get("use_case") or {}).get("desired_outcome"),
            "tags": md.get("tags") or [], "forked_from": forked, "update_available": update,
            "pass_rate": None, "production_success_rate": None, "avg_cost_usd": None,
            "runs_30d": 0, **measured}


@router.get("/agents")
def get_agents(q: str | None = None, tag: str | None = None,
               x_reagent_user: str | None = Header(default=None)):
    """The caller's collection. Visibility is filtered here (SPEC.md §2). Ranked on
    desired outcome and name matches first, then measured pass rate."""
    cat = load_catalog()
    _, is_admin, team = _principal(cat, x_reagent_user)
    docs = [d for d in (cat.latest(n) for n in sorted(cat.agents)) if d and _visible(d, is_admin, team)]
    if tag:
        docs = [d for d in docs if tag in (d["metadata"].get("tags") or [])]
    terms = (q or "").lower().split()

    def score(d: dict[str, Any]) -> int | None:
        md, uc = d["metadata"], (d.get("spec") or {}).get("use_case") or {}
        strong = f"{md['name']} {uc.get('desired_outcome') or ''} {' '.join(md.get('tags') or [])}".lower()
        weak = f"{uc.get('description') or ''}".lower()
        if not all(t in strong or t in weak for t in terms):
            return None
        return sum(2 if t in strong else 1 for t in terms)

    scored = [(s, d) for d in docs if (s := score(d)) is not None]
    measured = _measured([d["metadata"]["name"] for _, d in scored])
    drafts = set(_drafts())
    out = [_summary(cat, d, "draft" if d["metadata"]["name"] in drafts else "published",
                    measured.get(d["metadata"]["name"], {})) for _, d in scored]
    ranks = {d["metadata"]["name"]: s for s, d in scored}
    out.sort(key=lambda a: (-ranks[a["name"]], -(a["pass_rate"] or 0), a["name"]))
    return {"agents": out}


@router.get("/agents/{name}")
def get_agent(name: str, x_reagent_user: str | None = Header(default=None)):
    cat = load_catalog()
    _, is_admin, team = _principal(cat, x_reagent_user)
    doc = cat.latest(name)
    if doc is None or not _visible(doc, is_admin, team):
        raise HTTPException(404, "not found")  # deliberately indistinguishable
    versions = _versions(cat, name)
    current = next((v for v in versions if v["version"] == str(doc["metadata"].get("version"))), None)
    out = _summary(cat, doc, current["status"] if current else None, _measured([name]).get(name, {}))
    out["spec_yaml"] = yaml.safe_dump(doc, sort_keys=False)
    out["versions"] = versions
    return out


@router.get("/agents/{name}/resolved")
def get_resolved(name: str, x_reagent_user: str | None = Header(default=None)):
    cat = load_catalog()                       # SPEC.md §1: re-read every layer, every time
    user, is_admin, team = _principal(cat, x_reagent_user)
    doc = cat.latest(name)
    if doc is None or not _visible(doc, is_admin, team):
        raise HTTPException(404, "not found")  # deliberately indistinguishable
    out = resolve_agent(name, caller=user, catalog=cat)
    if out["rejections"]:
        return JSONResponse(status_code=422, content={"rejections": out["rejections"]})
    return out


@router.post("/agents/{name}/validate")
def post_validate(name: str, body: dict[str, Any] = Body(...),
                  x_reagent_user: str | None = Header(default=None)):
    cat = load_catalog()
    user, is_admin, team = _principal(cat, x_reagent_user)
    text = body.get("spec")
    if not isinstance(text, str):
        raise HTTPException(422, "body.spec must be the raw YAML of the .agent file")
    try:
        draft = yaml.safe_load(text)
    except yaml.YAMLError:
        draft = None
    if isinstance(draft, dict) and not is_admin:
        md = draft.get("metadata") or {}
        if md.get("level") == "enterprise" or (md.get("team") not in (None, team)):
            raise HTTPException(403, "members validate drafts in their own team only")
    return validate_draft_text(text, cat)


@router.post("/agents/{name}/evals")
def post_evals(name: str, body: dict[str, Any] | None = Body(default=None),
               x_reagent_user: str | None = Header(default=None)):
    """Runs the suite in the mocked sandbox. The model follows REAGENT_MODEL
    (fake by default), so a dashboard click never spends credits by accident."""
    from runtime.evals import run_evals

    cat = load_catalog()
    user, is_admin, team = _principal(cat, x_reagent_user)
    doc = cat.latest(name)
    if doc is None or not _visible(doc, is_admin, team):
        raise HTTPException(404, "not found")
    body = body or {}
    # REAGENT_PIPELINE_RUNS_PER_CASE (the demo's one-run setting) also sets the
    # default here, so the dashboard's Run evals button matches the pipeline.
    runs = body.get("runs_per_case", int(os.environ.get("REAGENT_PIPELINE_RUNS_PER_CASE") or 5))
    if not isinstance(runs, int) or runs < 1:
        raise HTTPException(422, "runs_per_case must be an integer >= 1")
    report = run_evals(name, runs_per_case=runs, caller=user, catalog=cat,
                       mutants=bool(body.get("mutants")))
    if not is_admin:
        for c in report["cases"]:      # SPEC.md §11: members see inherited cases' names and results only
            if c["origin"] == "inherited":
                c["checks"] = {}
    return report


# -- publish pipeline (SPEC.md §11) -------------------------------------------
DASHBOARD_URL = os.environ.get("REAGENT_DASHBOARD_URL", "http://localhost:3000")


def _refusal(err: ReAgentError, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content=err.to_dict())


def _run_for(run_id: int, cat: Catalog, user: str | None):
    from runtime.publish import STORE
    _, is_admin, team = _principal(cat, user)
    try:
        run = STORE.get(run_id)
    except KeyError:
        raise HTTPException(404, "not found")
    doc = run["draft"]
    if not _visible(doc, is_admin, team):
        raise HTTPException(404, "not found")
    return run


@router.post("/agents/{name}/publish", status_code=202, tags=["publish"])
def post_publish(name: str, body: dict[str, Any] | None = Body(default=None),
                 x_reagent_user: str | None = Header(default=None)):
    """Starts a PIPELINE, not a publish. Stages 1-3 run now; stage 4 is a human."""
    from runtime.publish import start, view

    cat = load_catalog()
    user, is_admin, team = _principal(cat, x_reagent_user)
    draft = None
    if body and isinstance(body.get("spec"), str):
        draft = yaml.safe_load(body["spec"])
    doc = draft or cat.latest(name)
    if doc is None or not _visible(doc, is_admin, team):
        raise HTTPException(404, "not found")
    try:
        run = start(name, user, draft, catalog=cat)
    except ReAgentError as e:
        code = e.rejections[0].code
        return _refusal(e, 409 if code is Code.PIPELINE_ALREADY_RUNNING else 422)
    return {"publish_run_id": run["id"], "review_url": f"{DASHBOARD_URL}/publish?run={run['id']}",
            "run": view(run)}


@router.get("/publish-runs/{run_id}")
def get_publish_run(run_id: int, x_reagent_user: str | None = Header(default=None)):
    from runtime.publish import view
    return view(_run_for(run_id, load_catalog(), x_reagent_user))


@router.post("/publish-runs/{run_id}/cases/{case_id:path}/review")
def post_review(run_id: int, case_id: str, body: dict[str, Any] = Body(...),
                x_reagent_user: str | None = Header(default=None)):
    from runtime.publish import review, view
    run = _run_for(run_id, load_catalog(), x_reagent_user)
    decision = {"approve": "approve", "edit": "edit", "reject": "reject"}.get(body.get("decision"))
    if decision is None:
        raise HTTPException(422, "decision must be approve, edit or reject")
    try:
        run = review(run["id"], case_id, decision, x_reagent_user,
                     edited_case=body.get("edited_case"), reason=body.get("reason"))
    except KeyError:
        raise HTTPException(404, f"no case {case_id} in this pipeline")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except ReAgentError as e:
        return _refusal(e, 409)
    return view(run)


@router.post("/publish-runs/{run_id}/cases/{case_id:path}/dry-run")
def post_dry_run(run_id: int, case_id: str, x_reagent_user: str | None = Header(default=None)):
    """Runs one case once so the reviewer sees real (redacted) agent output."""
    from runtime.evals import run_evals
    cat = load_catalog()
    run = _run_for(run_id, cat, x_reagent_user)
    c = next((x for x in run["cases"] if x["case_id"] == case_id), None)
    if c is None or c["origin"] == "inherited":
        raise HTTPException(404, f"no reviewable case {case_id} in this pipeline")
    draft = copy.deepcopy(run["draft"])
    draft["spec"].setdefault("evaluation", {})["cases"] = [c["_case"]]
    rep = run_evals(run["agent"], 1, draft=draft, catalog=cat, include_inherited=False)
    one = rep["cases"][0] if rep["cases"] else {}
    return {"output": {"checks": one.get("checks"), "error": one.get("error"),
                       "blocked_calls": one.get("blocked_calls", [])},
            "tool_calls": one.get("tool_calls", []), "passed": bool(one.get("passes")),
            "cost_usd": rep["total_cost"]}


@router.post("/publish-runs/{run_id}/continue", status_code=202)
def post_continue(run_id: int, x_reagent_user: str | None = Header(default=None)):
    """Continues to evals once every case is reviewed. Continuing is the author's
    acknowledgement of stage 2's advisory findings."""
    from runtime.publish import acknowledge, proceed, view
    run = _run_for(run_id, load_catalog(), x_reagent_user)
    if x_reagent_user == run["started_by"]:
        acknowledge(run["id"], x_reagent_user)
    try:
        return view(proceed(run["id"]))
    except ReAgentError as e:
        return _refusal(e, 409)


@router.post("/admin/calibration", tags=["admin"])
def post_calibration(body: dict[str, Any] = Body(...),
                     x_reagent_user: str | None = Header(default=None)):
    """Add human labels, then re-grade every label with the current judge (FROZEN D19).
    Enterprise admins only: calibration decides whether the judge may gate a publish."""
    from runtime.evals.judge import recalibrate

    cat = load_catalog()
    user, is_admin, _ = _principal(cat, x_reagent_user)
    if not is_admin:
        raise HTTPException(403, "enterprise admins only")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise HTTPException(503, "calibration labels live in the database, and none is configured")
    labels = (body or {}).get("labels")
    if not isinstance(labels, list) or not all(
            isinstance(l, dict) and {"case_name", "output_redacted", "human_verdict"} <= set(l)
            for l in labels):
        raise HTTPException(422, "labels must be a list of {case_name, output_redacted, human_verdict}")
    import psycopg
    from runtime.guardrails.redaction import Redactor
    redact = Redactor()
    with psycopg.connect(url, connect_timeout=15) as c:
        uid = c.execute("SELECT id FROM users WHERE name = %s", (user,)).fetchone()
        if uid is None:
            raise HTTPException(403, f"{user} is not a known user")
        for l in labels:
            # The column is named _redacted: redact BEFORE the write, never on read.
            text = redact.redact_text(str(l["output_redacted"]))[0]
            c.execute("INSERT INTO calibration_labels (agent_version_id, case_name, output_redacted, "
                      "human_verdict, labeled_by) VALUES (%s, %s, %s, %s, %s)",
                      (l.get("agent_version_id"), l["case_name"], text, bool(l["human_verdict"]), uid[0]))
    out = recalibrate(cat, url)
    return {k: out[k] for k in ("agreement", "kappa", "n_labels", "calibrated", "judge_model")}


NAME = re.compile(r"^[a-z0-9-]+$")


@router.post("/publish-runs/{run_id}/abandon")
def post_abandon(run_id: int, x_reagent_user: str | None = Header(default=None)):
    """Abandon a running pipeline, freeing the agent for a new one (FROZEN D17).
    Not in contracts/openapi.yaml yet: the 409 for an already-running pipeline names
    the run, and this is how the dashboard clears it. Reported for a contract update."""
    from runtime.publish import abandon, view
    run = _run_for(run_id, load_catalog(), x_reagent_user)
    return view(abandon(run["id"]))


def team_base(cat: Catalog, team: str, owner: str, name: str) -> dict[str, Any]:
    """The team's base config as a blank agent: it inherits the enterprise and team
    layers and adds nothing. No tools (least privilege, SPEC.md §1), the team's
    first allowed model, an http trigger, and placeholders the author replaces."""
    spec = cat.team(team) or {}
    allowed = (spec.get("models") or {}).get("allow") or (cat.ent.get("models") or {}).get("allow") or ["*"]
    model = next((p for p in allowed if p in cat.models), next(iter(sorted(cat.models)), ""))
    return {"apiVersion": "reagent/v0", "kind": "Agent",
            "metadata": {"name": name, "version": "0.1.0", "level": "team", "team": team},
            "spec": {"use_case": {"description": "What this agent does.",
                                  "desired_outcome": "What success looks like."},
                     "model": {"id": model},
                     "prompt": {"task": "Describe the task step by step."},
                     "triggers": [{"type": "http"}],
                     "ownership": {"owners": [owner]}}}


@router.post("/agents/{name}/fork", status_code=201)
def post_fork(name: str, body: dict[str, Any] = Body(...),
              x_reagent_user: str | None = Header(default=None)):
    """Fork into the caller's team as a new draft. SPEC.md §12, FROZEN D24.

    `name` is a team agent, and `forked_from` records its EXACT version, never a
    range. Or `name` is `team:<team>`: the team's base config, which is how a new
    agent starts from scratch. Agent names cannot contain ':', so it never collides."""
    from runtime.resolver import resolve_document
    from runtime.resolver.source import register_draft

    cat = load_catalog()
    user, is_admin, team = _principal(cat, x_reagent_user)
    new_name = (body or {}).get("new_name", "")
    base_team = name[len("team:"):] if name.startswith("team:") else None
    src = None
    if base_team is not None:
        if cat.team(base_team) is None or not (is_admin or base_team == team):
            raise HTTPException(404, "not found")
        target = base_team
    else:
        src = cat.latest(name)
        if src is None or not _visible(src, is_admin, team):
            raise HTTPException(404, "not found")
        if src["metadata"].get("level") != "team":
            raise HTTPException(422, "only team agents can be forked")
        target = team or src["metadata"].get("team")
        if not is_admin and src["metadata"].get("team") != team:
            return JSONResponse(status_code=403, content={"rejections": [Rejection(
                Code.FORK_OUTSIDE_TEAM, "metadata.forked_from", f"team:{team}", attempted=name).to_dict()]})
    if not NAME.match(new_name or ""):
        raise HTTPException(422, "new_name must match ^[a-z0-9-]+$")
    if cat.agent_versions(new_name):
        raise HTTPException(409, f"an agent called {new_name} already exists")

    if src is None:
        doc = team_base(cat, target, user, new_name)
    else:
        doc = copy.deepcopy(src)
        doc["metadata"].update(name=new_name, version="0.1.0", team=target,
                               forked_from=f"{name}@{src['metadata']['version']}")
        doc["spec"].setdefault("ownership", {})["owners"] = [user]
    md = doc["metadata"]
    resolved = resolve_document(doc, catalog=cat, caller=None)

    if os.environ.get("REAGENT_LAYERS", "files").lower() == "db":
        from runtime.publish.pgstore import PgStore
        PgStore().create_draft(doc, forked_from=(name, str(src["metadata"]["version"])) if src else None)
    else:
        register_draft(doc)
    spec = doc["spec"]
    return {"name": new_name, "version": md["version"], "status": "draft", "team": target,
            "level": "team", "description": spec.get("use_case", {}).get("description", ""),
            "desired_outcome": spec.get("use_case", {}).get("desired_outcome"),
            "tags": md.get("tags", []), "forked_from": md.get("forked_from"),
            "update_available": False,
            "spec_yaml": yaml.safe_dump(doc, sort_keys=False),
            "versions": [{"version": md["version"], "status": "draft", "published_at": None,
                          "published_by": None}],
            "rejections": resolved["rejections"]}


def create_app() -> FastAPI:
    app = FastAPI(title="ReAgent agents (wf/resolver)")
    app.include_router(router)
    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    uvicorn.run(create_app(), host=a.host, port=a.port)
