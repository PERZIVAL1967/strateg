"""Resume summary for a new chat (plan v1.3 §11 C01/C02; build step 4).

`strat context` is read-only. It derives, from the database alone, where the work stands and what
the next step is, so any chat - the same IDE or another one - continues from the same place:
  stage      one of STAGES, computed by fixed rules (never by the model's memory of the last chat)
  next       the actions allowed now, the skill to read, and the next free step number per run
  open       unfinished operations, failed gates, items under review, open questions to the human
  decisions  the decision in force with the human's words, and the commitments it created
  history    everything superseded or withdrawn, with how and by what - shown, never counted (R037)
  cost       the cost ledger: totals per kind/unit/basis; a kind never logged is unknown, not zero

Only current items drive the stage: a withdrawn decision, a replaced frame or a retired strategy is
history, so a new chat can't resume from it.

The same data is exported as `exports/context.md` for the human.
"""
import hashlib
import json
import pathlib
import re

import strat as S
import strat_evidence as E
import strat_ledger

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
INSTRUCTION_DIRS = ("skills", "method")

# stage -> (skill to read, what is allowed next)
STAGES = {
    "resume_operation": ("strat-resume", "довести прерванную операцию: повторить ту же команду с теми же run и step"),
    "framing": ("strat-frame", "согласовать с человеком постановку: цель, получатель, ограничения, данные"),
    "collecting": ("strat-collect", "согласовать с человеком разрешённые источники, затем import/fetch и extract"),
    "researching": ("strat-research", "записать наблюдения и типизированные утверждения по вопросам, которые могут изменить решение"),
    "options": ("strat-options", "записать не меньше 3 вариантов, включая «оставить как есть» и «закрыть направление», с возражением и условием пересмотра"),
    "strategy": ("strat-strategy", "записать продуктовую и коммуникационную стратегии, план проверки и сообщения"),
    "fixing": ("strat-research", "исправить проверки из open.gates и снова выполнить validate"),
    "documenting": ("strat-document", "собрать итоговый документ: расчёты (calc), прогноз по строкам (forecast_row), "
                                      "кварталы, люди, риски, просьба руководителю, затем части section (summary, 1-5, "
                                      "comms) из open.missing_sections; каждое число - со ссылкой [ID]; затем validate и export"),
    "awaiting_decision": ("strat-strategy", "представить рекомендацию и ждать: решает только человек"),
    "review_required": ("strat-revisit", "показать человеку, что затронули новые данные; подтвердить или пересмотреть с его словами"),
    "decided": ("strat-revisit", "следить за обязательствами; при новых данных - revalidate и impact"),
}


def instructions_hash():
    """One hash over the permanent instructions and method, recorded with every context read, so a run
    shows which instruction version it used and a silent edit is visible."""
    h = hashlib.sha256()
    files = [AGENT_DIR / f for f in INSTRUCTION_FILES]
    for d in INSTRUCTION_DIRS:
        files += sorted((AGENT_DIR / d).rglob("*.md")) if (AGENT_DIR / d).is_dir() else []
    for f in files:
        if f.is_file():
            h.update(f.relative_to(AGENT_DIR).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def next_steps(con):
    """Next free numeric step per run, so a resumed chat never reuses a (run, step) by accident."""
    out = {}
    for r in con.execute("SELECT run_id, step_id FROM operation"):
        n = int(r["step_id"]) if re.fullmatch(r"\d{1,9}", r["step_id"]) else 0
        out[r["run_id"]] = max(out.get(r["run_id"], 0), n)
    return {run: n + 1 for run, n in sorted(out.items())}


def build(p, con, revision=None):
    if revision is None:
        revision = int(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
    records = [dict(r) for r in con.execute("SELECT * FROM record ORDER BY created_at, id")]
    for r in records:
        r["fields"] = json.loads(r["fields"])
    by_kind = {}   # current work only; retired records are history
    for r in records:
        if r["status"] not in E.RETIRED:
            by_kind.setdefault(r["kind"], []).append(r)
    frames = [r for r in by_kind.get("question", []) if r["fields"].get("role") == "frame"]
    frame = frames[0] if len(frames) == 1 else None   # two current frames: FRAME_CONFLICT, never a silent pick
    strategies = {r["fields"].get("type") for r in by_kind.get("strategy", [])}
    open_ops = [dict(r) for r in con.execute(
        "SELECT op_id, run_id, step_id, tool, input_uri, status FROM operation WHERE status IN ('pending','unknown') "
        "ORDER BY created_at")]
    sources = con.execute("SELECT COUNT(*) FROM source_version").fetchone()[0]
    claims = con.execute("SELECT COUNT(*) FROM claim WHERE status NOT IN (?,?)", E.RETIRED).fetchone()[0]
    under_review = [dict(r) for r in con.execute(
        "SELECT id, 'claim' AS kind, text AS title FROM claim WHERE status='review_required' UNION ALL "
        "SELECT id, kind, title FROM record WHERE status='review_required' ORDER BY 1")]
    v = E.check(con, p.dir)
    failed_ops = [dict(r) for r in con.execute(
        "SELECT op_id, run_id, step_id, tool, input_uri, result_ref AS note FROM operation WHERE status='failed' "
        "ORDER BY created_at")]
    parts = {E.unique_key("section", r["fields"]) for r in by_kind.get("section", [])}
    missing_sections = [x for x in E.DOC_PARTS if x not in parts]
    decisions = by_kind.get("decision", [])

    if open_ops:
        stage = "resume_operation"
    elif frame is None:
        stage = "framing"
    elif not sources:
        stage = "collecting"
    elif not claims:
        stage = "researching"
    elif len(by_kind.get("option", [])) < 3:
        stage = "options"
    elif not {"product", "comms"} <= strategies or not by_kind.get("experiment"):
        stage = "strategy"
    elif under_review and decisions:   # new data after a decision: revisit first, gates are fixed as part of it
        stage = "review_required"
    elif not v["passed"]:
        stage = "fixing"
    elif missing_sections and not decisions:   # the document is what the human decides on
        stage = "documenting"
    elif not decisions:
        stage = "awaiting_decision"
    else:
        stage = "decided"
    skill, action = STAGES[stage]

    lease = dict(con.execute("SELECT owner_session, fencing_token, expires_at FROM lease WHERE id=1").fetchone())
    return {
        "project": p.dir.name, "db_revision": revision, "instructions_sha256": instructions_hash(),
        "stage": stage,
        "next": {"action": action, "skill": f"skills/{skill}/SKILL.md", "next_step": next_steps(con) or {"r1": 1}},
        "lease": {**lease, "note": "last committed lease; a new chat takes it with `lease` (and --takeover "
                                   "only once the human confirms the old chat is finished)"},
        "frame": {"id": frame["id"], "title": frame["title"], **frame["fields"]} if frame else None,
        "allowed_sources": [{**dict(r), "covers": S.COVERS[r["covers"]]} for r in con.execute(
            "SELECT pattern, covers, reason, added_at FROM source_policy WHERE revoked_at IS NULL ORDER BY pattern")],
        "counts": {"source_versions": sources, "claims": claims,
                   **{k: len(rs) for k, rs in sorted(by_kind.items())}},
        "open": {
            "operations": open_ops,
            "failed_operations": failed_ops,   # listed, never waited on; rerun the same run/step to retry
            "missing_sections": missing_sections,
            "gates": v["gates"],
            "flags": len(v["flags"]),
            "under_review": under_review,
            "questions_to_human": [{"id": r["id"], "title": r["title"]} for r in by_kind.get("question", [])
                                   if r["fields"].get("status") == "open"],
        },
        "decisions": [{"id": r["id"], "title": r["title"], "status": r["status"], "option": r["fields"].get("option"),
                       "answer": r["fields"].get("answer"), "decided_at": r["fields"].get("decided_at")}
                      for r in decisions],
        "history": history(con, records),
        "commitments": [{"id": r["id"], "title": r["title"], **{k: r["fields"].get(k) for k in
                                                                ("owner", "metric", "due", "review_if")}}
                        for r in by_kind.get("commitment", [])],
        "cost": strat_ledger.summary(con),
    }


def history(con, records):
    """Retired claims and records, oldest first, with the resolution that retired them. A decision keeps
    the human's original words."""
    rows = [{"id": r["id"], "kind": r["kind"], "title": r["title"], "status": r["status"],
             **({"answer": r["fields"].get("answer")} if r["kind"] == "decision" else {}), "at": r["created_at"]}
            for r in records if r["status"] in E.RETIRED]
    rows += [{"id": r["id"], "kind": "claim", "title": r["text"], "status": r["status"], "at": r["created_at"]}
             for r in con.execute("SELECT id, text, status, created_at FROM claim WHERE status IN (?,?)", E.RETIRED)]
    for h in rows:
        ev = con.execute("SELECT reason, cause_id FROM review_event WHERE target_id=? AND (reason LIKE 'supersede:%' "
                         "OR reason LIKE 'withdraw:%') ORDER BY created_at DESC, id LIMIT 1", (h["id"],)).fetchone()
        h["resolution"], h["by"] = (ev["reason"], ev["cause_id"]) if ev else (None, None)
    return sorted(rows, key=lambda h: (h.pop("at"), h["id"]))


def cmd_context(p, a):
    con = p.connect()
    con.execute("BEGIN")   # one consistent snapshot
    try:
        return "ok", build(p, con)
    finally:
        con.execute("COMMIT")


def readable_host(pattern):
    try:
        return pattern.encode("ascii").decode("idna")
    except UnicodeError:
        return pattern


def markdown(c):
    L = ["# Где мы сейчас", "", "Файл создан автоматически из базы - не редактировать. Новый чат начинает с "
         "`python tools/strat.py context --project " + c["project"] + "`.", "",
         f"- Этап: **{c['stage']}**",
         f"- Следующее действие: {c['next']['action']}",
         f"- Навык: `{c['next']['skill']}`",
         f"- Следующий свободный шаг: " + ", ".join(f"{r}: {n}" for r, n in c["next"]["next_step"].items()),
         f"- Ревизия базы: {c['db_revision']}; инструкции: `{c['instructions_sha256'][:12]}`", ""]
    if c["frame"]:
        f = c["frame"]
        L += ["## Постановка", "", f"**{f['title']}**", ""]
        L += [f"- {k}: {f.get(k, '-')}" for k in E.FRAME_KEYS] + [""]
    L += ["## Разрешённые источники", ""]
    L += [f"- {readable_host(s['pattern'])} ({s['covers']}) - {s['reason']} ({s['added_at']})"
          for s in c["allowed_sources"]] or ["- пока нет"]
    L += ["", "## Открытые вопросы", ""]
    o = c["open"]
    items = [f"- незавершённая операция {x['run_id']}/{x['step_id']} ({x['tool']} {x['input_uri']})" for x in o["operations"]]
    items += [f"- неудавшаяся операция {x['run_id']}/{x['step_id']} ({x['tool']} {x['input_uri']})"
              + (f": {x['note']}" if x.get("note") else "") for x in o.get("failed_operations", [])]
    if c["stage"] in ("documenting", "awaiting_decision") and o.get("missing_sections"):
        items.append("- в итоговом документе нет частей: " + ", ".join(o["missing_sections"]))
    items += [f"- проверка {g['code']}: {g['id'] or ''} {g['message']}" for g in o["gates"]]
    items += [f"- на пересмотре: {x['id']} ({x['kind']}) {x['title']}" for x in o["under_review"]]
    items += [f"- вопрос человеку: {x['id']} {x['title']}" for x in o["questions_to_human"]]
    L += items or ["- нет"]
    L += ["", "## Решения", ""]
    L += [f"- {d['id']} {d['title']} - «{d['answer']}» (вариант {d['option'] or '-'})" for d in c["decisions"]] \
        or ["- действующего решения нет"]
    if c["commitments"]:
        L += ["", "## Обязательства", ""]
        L += [f"- {x['id']} {x['title']}: ответственный {x.get('owner') or '-'}, показатель {x.get('metric') or '-'}, "
              f"срок {x.get('due') or '-'}, пересмотр если {x.get('review_if') or '-'}" for x in c["commitments"]]
    cost = c["cost"]
    L += ["", "## Стоимость", ""]
    L += [f"- {t['kind']}: {t['amount']:g} {t['unit']} ({t['basis']}, записей: {t['entries']})" for t in cost["totals"]]
    L += [] if cost["model_cost_logged"] else ["- расход модели не записан"]
    L += [f"- не записано (неизвестно, не ноль): {', '.join(cost['unknown'])}"] if cost["unknown"] else []
    if c["history"]:
        L += ["", "## История (не действует)", ""]
        L += [f"- {h['id']} ({h['kind']}, {h['status']}) {h['title']}"
              + (f" - «{h['answer']}»" if h.get("answer") else "")
              + (f"; {h['resolution']}" if h["resolution"] else "") + (f"; заменено на {h['by']}" if h["by"] else "")
              for h in c["history"]]
    return ("\n".join(L) + "\n").encode()
