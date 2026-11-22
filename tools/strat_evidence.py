"""Evidence layer for strat (plan v1.3 §4, §9, §11; build step 3).

The chain: source version -> segment -> OBSERVATION (what a source says, at a uniquely resolved
quote) -> CLAIM (an interpretation, typed fact|hypothesis|forecast|synthetic|vendor_claim) ->
RECORD (question, hypothesis, option, capability, strategy, message, experiment, decision,
commitment). One `edge` table holds every "X rests on Y" link, so `impact` can walk it.

Every write takes --run/--step: the record and its operation row are written in ONE transaction,
so a crash either leaves nothing or leaves a finished record that a rerun returns as `skipped`.

`validate` separates GATES (structural, must pass) from FLAGS (heuristics for human review,
never proofs) - council #3 R016/R019.
"""
import hashlib
import json
import re
import uuid

import strat as S
import strat_extract

RECORD_KINDS = ("question", "hypothesis", "option", "capability", "strategy", "message", "experiment", "decision",
                "commitment",
                # schema 8: the parts of the final document (exports/document.md) and what sits next to them
                "section", "calc", "forecast_row", "quarter", "resource", "risk", "ask", "target")


def record_table_sql(name="record"):
    kinds = ",".join(f"'{k}'" for k in RECORD_KINDS)
    return f"""CREATE TABLE IF NOT EXISTS {name} (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ({kinds})),
  title TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{{}}',
  status TEXT NOT NULL DEFAULT 'current'
    CHECK (status IN ('current','review_required','superseded','withdrawn')),
  created_at TEXT NOT NULL);
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS observation (
  id TEXT PRIMARY KEY, source_version_id TEXT NOT NULL REFERENCES source_version(id),
  extraction_version TEXT NOT NULL, segment_idx INTEGER NOT NULL, hint TEXT NOT NULL,
  exact TEXT NOT NULL, prefix TEXT NOT NULL, suffix TEXT NOT NULL,
  span_hash TEXT NOT NULL, context_hash TEXT NOT NULL, context TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('quote','measurement','count','statement')),
  value TEXT, unit TEXT, period TEXT, population TEXT,
  status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','superseded','review_required')),
  supersedes_id TEXT REFERENCES observation(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('fact','hypothesis','forecast','synthetic','vendor_claim')),
  text TEXT NOT NULL, scope TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','checked','contested','superseded','withdrawn','review_required')),
  created_at TEXT NOT NULL);
{RECORD_TABLE}CREATE TABLE IF NOT EXISTS edge (
  from_id TEXT NOT NULL, to_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('supports','contradicts','context','depends')),
  PRIMARY KEY (from_id, to_id, kind));
CREATE INDEX IF NOT EXISTS edge_to ON edge(to_id);
CREATE TABLE IF NOT EXISTS review_event (
  id TEXT PRIMARY KEY, target_id TEXT NOT NULL, reason TEXT NOT NULL, cause_id TEXT,
  created_at TEXT NOT NULL, resolved_at TEXT);
""".replace("{RECORD_TABLE}", record_table_sql())


def migrate_7_to_8(con):
    """Schema 8 widens the record.kind CHECK. SQLite can't alter a CHECK, so the table is rebuilt with the
    same columns and rows (no other table references it). Caller holds BEGIN IMMEDIATE, foreign keys off."""
    con.execute("DROP TABLE IF EXISTS record_v8")
    con.execute(record_table_sql("record_v8"))
    con.execute("INSERT INTO record_v8 (id, kind, title, fields, status, created_at) "
                "SELECT id, kind, title, fields, status, created_at FROM record")
    con.execute("DROP TABLE record")
    con.execute("ALTER TABLE record_v8 RENAME TO record")

SOURCE_KINDS = ("official", "statistics", "regulator", "research", "news", "vendor", "review", "search",
                "forum", "interview", "synthetic", "other", "unknown")
# Allowlist (rehearsal R039 #5): only these kinds can establish demand as a fact - method/sources.md «Спрос»
# (statistics incl. the operator's own sales/usage data, research with a described sample). Everything else -
# vendor pages, official offer terms, news, reviews, search, forums, interviews, synthetic, unclassified - is weak.
STRONG_FOR_DEMAND = {"statistics", "research"}
WEAK_FOR_DEMAND = set(SOURCE_KINDS) - STRONG_FOR_DEMAND   # kept for readers of the old name
SCOPE_KEYS = ("audience", "region", "period", "product", "metric")
DEMAND_RE = re.compile(r"спрос|готов\w*\s+плат|willing(ness)? to pay|\bdemand\b", re.I)
# Brief «Обязательный минимум» п.1 and п.5: the fields each record must state (a value, or 'unknown: reason').
FRAME_KEYS = ("goal", "recipient", "constraints", "data")
STRATEGY_KEYS = {
    "product": ("audience", "job", "alternatives", "direction", "refusals", "economics"),
    "comms": ("audience", "situation", "barrier", "key_message", "proof", "channels"),
}
EXPERIMENT_KEYS = ("expected_change", "baseline", "period", "side_effects",
                   # schema 8 (pilot design, method/data.md): a value or 'n/a: reason'
                   "unit", "assignment", "itt", "sample_size", "recruitment", "threshold", "stop_rule")
# Final-document records (schema 8): required fields; a value may be 'unknown: reason' or 'n/a: reason'.
DOC_KEYS = {
    "section": ("part", "body"),
    "calc": ("script", "outputs", "assumptions"),
    "forecast_row": ("metric", "no_change", "ours", "driver"),
    "quarter": ("q", "known", "decision"),
    "resource": ("role", "person_months", "period", "owner", "open_question"),
    "risk": ("signal", "consequence", "owner", "action"),
    "ask": ("when", "decision", "resources", "owner"),
    "target": ("horizon", "metric", "numerator", "base", "condition"),
}
DOC_PARTS = ("summary", "1", "2", "3", "4", "5", "comms")
# one current record per key: a new one supersedes the old (same part / metric / quarter / ask moment)
UNIQUE_KEY = {"section": "part", "forecast_row": "metric", "quarter": "q", "ask": "when"}
KEY_VALUES = {"section": DOC_PARTS, "quarter": ("1", "2", "3", "4"), "ask": ("Q1", "Q2")}
# ids as the tool prints them: O-, C-, and a record's kind prefix (SEC-, CAL-, FOR-, ...) + 10 hex digits
ID_RE = re.compile(r"\b[A-Z]{1,3}-[0-9a-f]{10}\b")
REF_RE = re.compile(r"\[\s*[A-Z]{1,3}-[0-9a-f]{10}(?:\s*[,;]\s*[A-Z]{1,3}-[0-9a-f]{10})*\s*\]")
RETIRED = ("superseded", "withdrawn")   # history: kept and shown, never current work or evidence (R037)
FUTURE_RE = re.compile(r"\bбудет\b|\bбудут\b|ожида\w+|прогноз\w*|\bwill\b|\bexpected\b", re.I)


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def idempotent(con, a, tool, payload):
    """Look up (run, step). Same input -> previous result; other input -> CONFLICT; new -> None.
    Caller is inside write_tx and records the op with `finish_op` in the same transaction."""
    input_hash = hashlib.sha256(json.dumps({"tool": tool, **payload}, sort_keys=True,
                                           ensure_ascii=False).encode()).hexdigest()
    op = con.execute("SELECT * FROM operation WHERE run_id=? AND step_id=?", (a.run, a.step)).fetchone()
    if op is None:
        return None, input_hash
    if op["input_hash"] != input_hash or op["tool"] != tool:
        con.execute("ROLLBACK")
        raise S.StratError("CONFLICT", f"run {a.run!r} step {a.step!r} was already used with different input")
    con.execute("ROLLBACK")
    return op["result_ref"], input_hash


def finish_op(con, a, tool, input_hash, result_ref):
    op_id = "op-" + uuid.uuid4().hex
    con.execute("INSERT INTO operation VALUES (?,?,?,?,?,?,'succeeded',?,?,?)",
                (op_id, a.run, a.step, tool, input_hash, f"local:{tool}", result_ref, S.now(), S.now()))
    con.execute("INSERT INTO op_attempt (op_id, attempt_no, claim_token, outcome, cost_kind, started_at) "
                "VALUES (?,1,?,'ok','none',?)", (op_id, uuid.uuid4().hex, S.now()))


def node_exists(con, node_id):
    for table in ("observation", "claim", "record"):
        if con.execute(f"SELECT 1 FROM {table} WHERE id=?", (node_id,)).fetchone():
            return table
    return None


def add_edges(con, from_id, ids, kind):
    for to_id in ids:
        if not node_exists(con, to_id):
            con.execute("ROLLBACK")
            raise S.StratError("NOT_FOUND", f"{from_id} cannot rest on unknown id {to_id}")
        con.execute("INSERT OR IGNORE INTO edge VALUES (?,?,?)", (from_id, to_id, kind))


def json_arg(value, name):
    """A JSON object given inline or as @file (PowerShell drops inner quotes of inline JSON)."""
    text = value or "{}"
    if text.startswith("@"):
        try:
            text = open(text[1:], encoding="utf-8-sig").read()
        except OSError as e:
            raise S.StratError("NOT_FOUND", f"{name}: cannot read {text[1:]}: {e}")
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        raise S.StratError("VALIDATION_FAILED", f"{name} must be a JSON object (inline or @file)")
    return obj


def split_ids(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


# ---------------------------------------------------------------- commands

def cmd_classify(p, a):
    if a.source_kind not in SOURCE_KINDS:
        raise S.StratError("VALIDATION_FAILED", f"source kind must be one of {', '.join(SOURCE_KINDS)}")
    con = p.connect()
    S.write_tx(con, a.session, a.token)
    n = con.execute("UPDATE source_version SET source_kind=? WHERE id=?", (a.source_kind, a.source_version)).rowcount
    if not n:
        con.execute("ROLLBACK")
        raise S.StratError("NOT_FOUND", f"no source version {a.source_version}")
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"source_version_id": a.source_version, "source_kind": a.source_kind}


def cmd_observe(p, a):
    payload = {k: getattr(a, k) for k in ("source_version", "exact", "prefix", "suffix", "hint", "kind",
                                          "value", "unit", "period", "population")}
    con = p.connect()
    ev = strat_extract.EXTRACTION_VERSION
    S.write_tx(con, a.session, a.token)
    prev, input_hash = idempotent(con, a, "observe", payload)
    if prev:
        return "skipped", {"observation_id": prev, "reason": "already recorded"}
    segs = [dict(r) for r in con.execute(
        "SELECT idx, kind, hint, text, context, span_hash, context_hash FROM segment "
        "WHERE source_version_id=? AND extraction_version=? ORDER BY idx", (a.source_version, ev))]
    if not segs:
        con.execute("ROLLBACK")
        raise S.StratError("NOT_FOUND", f"no segments for {a.source_version} / {ev}; run extract")
    r = strat_extract.locate(segs, a.exact, a.prefix or "", a.suffix or "", a.hint)
    if not r["resolved"]:
        con.execute("ROLLBACK")
        raise S.StratError("UNRESOLVED", f"quote is {r['reason']} in {a.source_version}; add prefix/suffix/hint",
                           reason=r["reason"], candidates=r["candidates"])
    s = r["segment"]
    oid = new_id("O")
    con.execute("INSERT INTO observation (id, source_version_id, extraction_version, segment_idx, hint, exact, "
                "prefix, suffix, span_hash, context_hash, context, kind, value, unit, period, population, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, a.source_version, ev, s["idx"], s["hint"], a.exact, a.prefix or "", a.suffix or "",
                 s["span_hash"], s["context_hash"], s["context"], a.kind, a.value, a.unit, a.period,
                 a.population, S.now()))
    finish_op(con, a, "observe", input_hash, oid)
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"observation_id": oid, "hint": s["hint"], "context": s["context"]}


def cmd_claim(p, a):
    scope = json_arg(a.scope, "--scope")
    tags = split_ids(a.tags)
    payload = {"type": a.type, "text": a.text, "scope": scope, "tags": tags, "supports": split_ids(a.supports),
               "contradicts": split_ids(a.contradicts), "depends": split_ids(a.depends)}
    con = p.connect()
    S.write_tx(con, a.session, a.token)
    prev, input_hash = idempotent(con, a, "claim", payload)
    if prev:
        return "skipped", {"claim_id": prev, "reason": "already recorded"}
    cid = new_id("C")
    con.execute("INSERT INTO claim (id, type, text, scope, tags, created_at) VALUES (?,?,?,?,?,?)",
                (cid, a.type, a.text, json.dumps(scope, ensure_ascii=False, sort_keys=True),
                 json.dumps(tags, ensure_ascii=False), S.now()))
    add_edges(con, cid, payload["supports"], "supports")
    add_edges(con, cid, payload["contradicts"], "contradicts")
    add_edges(con, cid, payload["depends"], "depends")
    finish_op(con, a, "claim", input_hash, cid)
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"claim_id": cid}


def unique_key(kind, fields):
    """The value that makes a document record unique among current ones (None for other kinds)."""
    k = UNIQUE_KEY.get(kind)
    if k is None:
        return None
    v = fields.get(k)
    v = " ".join(str(v if v is not None else "").split())
    return v.upper() if kind == "ask" else v.casefold()


def cmd_record(p, a):
    fields = json_arg(a.fields, "--fields")
    if a.kind not in RECORD_KINDS:
        raise S.StratError("VALIDATION_FAILED", f"--kind must be one of {', '.join(RECORD_KINDS)}")
    if a.kind in KEY_VALUES:   # the key decides which record a new one replaces, so it can't be vague
        key, allowed = UNIQUE_KEY[a.kind], KEY_VALUES[a.kind]
        if unique_key(a.kind, fields) not in {x.casefold() if a.kind != "ask" else x for x in allowed}:
            raise S.StratError("VALIDATION_FAILED", f"{a.kind} needs fields.{key} - one of {', '.join(allowed)}")
    payload = {"kind": a.kind, "title": a.title, "fields": fields, "depends": split_ids(a.depends)}
    con = p.connect()
    S.write_tx(con, a.session, a.token)
    prev, input_hash = idempotent(con, a, "record", payload)
    if prev:
        return "skipped", {"record_id": prev, "reason": "already recorded"}
    rid = new_id(a.kind[:3].upper())
    con.execute("INSERT INTO record (id, kind, title, fields, created_at) VALUES (?,?,?,?,?)",
                (rid, a.kind, a.title, json.dumps(fields, ensure_ascii=False, sort_keys=True), S.now()))
    add_edges(con, rid, payload["depends"], "depends")
    replaced = []
    key = unique_key(a.kind, fields)
    if key is not None:   # one current section per part, forecast row per metric, quarter, ask moment
        for r in con.execute("SELECT id, fields FROM record WHERE kind=? AND id<>? AND status NOT IN (?,?)",
                             (a.kind, rid) + RETIRED).fetchall():
            if unique_key(a.kind, json.loads(r["fields"])) == key:
                con.execute("UPDATE record SET status='superseded' WHERE id=?", (r["id"],))
                con.execute("UPDATE review_event SET resolved_at=? WHERE target_id=? AND resolved_at IS NULL",
                            (S.now(), r["id"]))
                con.execute("INSERT INTO review_event VALUES (?,?,?,?,?,?)",
                            (new_id("RV"), r["id"], f"supersede: new {a.kind} with the same "
                             f"{UNIQUE_KEY[a.kind]} ({fields.get(UNIQUE_KEY[a.kind])})", rid, S.now(), S.now()))
                replaced.append(r["id"])
    finish_op(con, a, "record", input_hash, rid)
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"record_id": rid, **({"superseded": replaced} if replaced else {})}


def impacted(con, ids):
    """Everything that rests, directly or transitively, on any of `ids` (excluding the ids themselves)."""
    out = set()
    for start in ids:
        rows = con.execute("""
            WITH RECURSIVE up(id) AS (
              SELECT ? UNION SELECT e.from_id FROM edge e JOIN up ON e.to_id = up.id)
            SELECT id FROM up""", (start,)).fetchall()
        out.update(r[0] for r in rows)
    return sorted(out - set(ids))


def cmd_impact(p, a):
    con = p.connect()
    ids = split_ids(a.ids)
    for i in ids:
        if not node_exists(con, i):
            raise S.StratError("NOT_FOUND", f"unknown id {i}")
    hit = impacted(con, ids)
    grouped = {}
    for i in hit:
        grouped.setdefault(node_exists(con, i), []).append(i)
    return "ok", {"changed": ids, "affected": grouped, "count": len(hit)}


def cmd_revalidate(p, a):
    """New version of a source: carry forward observations whose quote resolves uniquely in the new
    version with the same span AND context hash; everything else -> review_required, plus all it supports.
    Conservative by design (R016/R019): a matching paragraph is not proof nothing else changed."""
    con = p.connect()
    ev = strat_extract.EXTRACTION_VERSION
    S.write_tx(con, a.session, a.token)
    prev, input_hash = idempotent(con, a, "revalidate", {"old": a.old, "new": a.new})
    if prev:
        return "skipped", {"result": json.loads(prev), "reason": "already revalidated"}
    olds = [dict(r) for r in con.execute(
        "SELECT * FROM observation WHERE source_version_id=? AND status='current'", (a.old,))]
    segs = [dict(r) for r in con.execute(
        "SELECT idx, kind, hint, text, context, span_hash, context_hash FROM segment "
        "WHERE source_version_id=? AND extraction_version=? ORDER BY idx", (a.new, ev))]
    if not segs:
        con.execute("ROLLBACK")
        raise S.StratError("NOT_FOUND", f"no segments for new version {a.new}; run extract first")
    carried, review = [], []
    for o in olds:
        # the recorded structural hint first; if the layout moved and it's gone, the quote alone -
        # either way the match must be unique
        r = strat_extract.locate(segs, o["exact"], o["prefix"], o["suffix"], o["hint"])
        if not r["resolved"] and r["reason"] == "not_found":
            r = strat_extract.locate(segs, o["exact"], o["prefix"], o["suffix"])
        s = r.get("segment")
        same = r["resolved"] and s["span_hash"] == o["span_hash"] and s["context_hash"] == o["context_hash"]
        if same:
            nid = new_id("O")
            con.execute("INSERT INTO observation (id, source_version_id, extraction_version, segment_idx, hint, "
                        "exact, prefix, suffix, span_hash, context_hash, context, kind, value, unit, period, "
                        "population, supersedes_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (nid, a.new, ev, s["idx"], s["hint"], o["exact"], o["prefix"], o["suffix"], s["span_hash"],
                         s["context_hash"], s["context"], o["kind"], o["value"], o["unit"], o["period"],
                         o["population"], o["id"], S.now()))
            con.execute("UPDATE observation SET status='superseded' WHERE id=?", (o["id"],))
            # whoever rested on the old observation now rests on the carried-forward one too
            con.execute("INSERT OR IGNORE INTO edge SELECT from_id, ?, kind FROM edge WHERE to_id=?", (nid, o["id"]))
            carried.append({"old": o["id"], "new": nid})
        else:
            why = r.get("reason") or ("context_changed" if s and s["context_hash"] != o["context_hash"] else "span_changed")
            con.execute("UPDATE observation SET status='review_required' WHERE id=?", (o["id"],))
            review.append({"observation": o["id"], "reason": why})
    flagged = []
    for target in [x["observation"] for x in review] + impacted(con, [x["observation"] for x in review]):
        table = node_exists(con, target)
        if table in ("claim", "record"):
            if con.execute(f"SELECT status FROM {table} WHERE id=?", (target,)).fetchone()[0] in RETIRED:
                continue   # history stays history; a new source version doesn't bring it back (R037)
            con.execute(f"UPDATE {table} SET status='review_required' WHERE id=?", (target,))
        if table != "observation":
            flagged.append(target)
        con.execute("INSERT INTO review_event VALUES (?,?,?,?,?,NULL)",
                    (new_id("RV"), target, f"source {a.old} -> {a.new}", a.new, S.now()))
    result = {"carried_forward": carried, "review_required": review, "affected": flagged}
    finish_op(con, a, "revalidate", input_hash, json.dumps(result, sort_keys=True))
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", result


RESOLVE_STATUS = {   # outcome -> new status for a claim / a record
    "reaffirm": ("checked", "current"),
    "supersede": ("superseded", "superseded"),
    "withdraw": ("withdrawn", "withdrawn"),
}


def is_human_decision(fields):
    return fields.get("actor") == "human" and bool(str(fields.get("answer", "")).strip())


def replacement_kind(row, table):
    """What a replacement must match (R037): a claim by a claim, a record by a record of the same kind;
    a strategy also keeps its type and a frame stays a frame."""
    if table == "claim":
        return ("claim",)
    f = json.loads(row["fields"])
    return ("record", row["kind"], f.get("type") if row["kind"] == "strategy" else None,
            f.get("role") == "frame" if row["kind"] == "question" else None)


def cmd_resolve(p, a):
    """Close a review: reaffirm (the basis was re-checked and still holds), supersede (replaced by --by)
    or withdraw (no longer held). Nothing is deleted; review events are closed with the reason.
    History is final: only current items can be resolved, and the replacement must be current, of the same
    kind and not among the replaced - so a supersession chain can never loop back (R037).
    A human decision is never changed on the agent's word: it is superseded only by a new human decision,
    withdrawn only with the human's words (--answer), and never "reaffirmed" by a reason alone."""
    if a.outcome not in RESOLVE_STATUS:
        raise S.StratError("VALIDATION_FAILED", f"--outcome must be one of {', '.join(RESOLVE_STATUS)}")
    if not (a.reason or "").strip():
        raise S.StratError("VALIDATION_FAILED", "--reason is required")
    if (a.outcome == "supersede") != bool(a.by):
        raise S.StratError("VALIDATION_FAILED", "--by <the replacing id> goes with supersede, and only with it")
    ids = split_ids(a.ids)
    if a.by in ids:
        raise S.StratError("VALIDATION_FAILED", f"{a.by} can't replace itself")
    con = p.connect()
    S.write_tx(con, a.session, a.token)
    prev, input_hash = idempotent(con, a, "resolve", {"ids": ids, "outcome": a.outcome, "by": a.by or "",
                                                      "reason": a.reason, "answer": a.answer or ""})
    if prev:
        return "skipped", {"resolved": json.loads(prev), "reason": "already resolved"}

    def fail(code, msg):
        con.execute("ROLLBACK")
        raise S.StratError(code, msg)

    def row(i):
        table = node_exists(con, i)
        if table not in ("claim", "record"):
            fail("NOT_FOUND", f"{i} is not a claim or record (observations keep their history)")
        return table, con.execute(f"SELECT * FROM {table} WHERE id=?", (i,)).fetchone()

    if a.by:
        by_table, by = row(a.by)
        if by["status"] in RETIRED:
            fail("VALIDATION_FAILED", f"replacement {a.by} is itself history ({by['status']})")
    done = []
    for i in ids:
        table, r = row(i)
        if r["status"] in RETIRED:
            fail("VALIDATION_FAILED", f"{i} is already history ({r['status']}); resolve its replacement instead")
        if a.by and replacement_kind(by, by_table) != replacement_kind(r, table):
            fail("VALIDATION_FAILED", f"{a.by} can't replace {i}: a replacement must be the same kind of item")
        if table == "record" and r["kind"] == "decision":
            if a.outcome == "reaffirm":
                fail("VALIDATION_FAILED", "a decision is not reaffirmed by a reason: record the human's new decision "
                                          "and supersede the old one with it")
            if a.outcome == "supersede" and not is_human_decision(json.loads(by["fields"])):
                fail("VALIDATION_FAILED", f"{a.by} has no human answer; only the human's decision replaces a decision")
            if a.outcome == "withdraw" and not (a.answer or "").strip():
                fail("VALIDATION_FAILED", "withdrawing a decision needs the human's own words (--answer)")
        status = RESOLVE_STATUS[a.outcome][0 if table == "claim" else 1]
        con.execute(f"UPDATE {table} SET status=? WHERE id=?", (status, i))
        con.execute("UPDATE review_event SET resolved_at=? WHERE target_id=? AND resolved_at IS NULL", (S.now(), i))
        note = f"{a.outcome}: {a.reason}" + (f" | человек: «{a.answer.strip()}»" if (a.answer or "").strip() else "")
        con.execute("INSERT INTO review_event VALUES (?,?,?,?,?,?)",
                    (new_id("RV"), i, note, a.by, S.now(), S.now()))
        done.append({"id": i, "status": status})
    finish_op(con, a, "resolve", input_hash, json.dumps(done, sort_keys=True))
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"resolved": done}


def cmd_validate(p, a):
    return "ok", check(p.connect(), p.dir)


def strip_refs(text):
    return REF_RE.sub(" ", text)


def unsourced_paragraphs(body):
    """Paragraphs of a section body that state a number but cite no record/claim id in [brackets].
    List markers, headings, quarter names (Q1-Q4) and the ids themselves don't count as numbers."""
    out = []
    for para in re.split(r"\n\s*\n", body or ""):
        if REF_RE.search(para):
            continue
        lines = [ln for ln in para.splitlines() if not ln.lstrip().startswith("#")]
        text = "\n".join(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", ln) for ln in lines)
        text = re.sub(r"\b[QК][1-4]\b", " ", ID_RE.sub(" ", text))
        if re.search(r"\d", text):
            out.append(" ".join(para.split())[:80])
    return out


def doc_value_missing(kind, key, v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return True
    if isinstance(v, (dict, list)) and not v:
        return True
    return False


def calc_script_problem(project_dir, script):
    """None if `script` is a file under <project>/work/, else why not."""
    if project_dir is None or not isinstance(script, str) or script.lower().startswith(("unknown", "n/a")):
        return None
    rel = script.replace("\\", "/")
    base = (project_dir / "work").resolve()
    target = (project_dir / rel).resolve()
    if base != target and base not in target.parents:
        return f"script {script!r} is not under work/ of the project"
    if not target.is_file():
        return f"script {script!r} does not exist under the project (expected projects/<id>/{rel})"
    return None


def check(con, project_dir=None):
    """Gates + flags over one connection (callers wanting one snapshot hold a read transaction).
    `project_dir` lets the calc check look for scripts on disk; without it that check is skipped."""
    gates, flags = [], []

    def gate(code, node, msg):
        gates.append({"code": code, "id": node, "message": msg})

    def flag(code, node, msg):
        flags.append({"code": code, "id": node, "message": msg})

    obs = {r["id"]: dict(r) for r in con.execute(
        "SELECT o.*, sv.source_kind FROM observation o JOIN source_version sv ON sv.id = o.source_version_id")}
    claims = {r["id"]: dict(r) for r in con.execute("SELECT * FROM claim")}
    records = {r["id"]: dict(r) for r in con.execute("SELECT * FROM record")}
    edges = [dict(r) for r in con.execute("SELECT * FROM edge")]
    out_edges = {}
    for e in edges:
        out_edges.setdefault(e["from_id"], []).append(e)
        if e["to_id"] not in obs and e["to_id"] not in claims and e["to_id"] not in records:
            gate("DANGLING_EDGE", e["from_id"], f"rests on missing {e['to_id']}")

    def synthetic_ancestor(start):
        """First synthetic claim or synthetic-source observation reachable from `start` through any edge
        except `contradicts` (council #3 R035: the check was direct-only)."""
        seen, stack = {start}, [start]
        while stack:
            n = stack.pop()
            for e in out_edges.get(n, []):
                m = e["to_id"]
                if e["kind"] == "contradicts" or m in seen:
                    continue
                seen.add(m)
                if claims.get(m, {}).get("type") == "synthetic" or obs.get(m, {}).get("source_kind") == "synthetic":
                    return m
                stack.append(m)
        return None

    for cid, c in claims.items():
        scope = json.loads(c["scope"])
        missing = [k for k in SCOPE_KEYS if not str(scope.get(k, "")).strip()]
        if missing:
            gate("SCOPE_MISSING", cid, f"scope needs {', '.join(missing)} (a value, or 'unknown: reason' / 'n/a: reason')")
        for k in SCOPE_KEYS:
            if str(scope.get(k, "")).strip().lower() in ("unknown", "n/a"):
                flag("SCOPE_NO_REASON", cid, f"scope.{k} is '{scope[k]}' without a reason")
        sup = [e["to_id"] for e in out_edges.get(cid, []) if e["kind"] == "supports" and e["to_id"] in obs]
        if c["type"] == "fact" and c["status"] not in RETIRED:   # history is not re-judged
            live = [o for o in sup if obs[o]["status"] == "current"]
            if not sup:
                gate("FACT_WITHOUT_EVIDENCE", cid, "a fact needs at least one supporting observation")
            elif not live:
                gate("FACT_BASIS_UNDER_REVIEW", cid, "every supporting observation is superseded or under review")
            tags = json.loads(c["tags"])
            if "demand" in tags or DEMAND_RE.search(c["text"]):
                kinds = {obs[o]["source_kind"] for o in live}
                if kinds and not kinds & STRONG_FOR_DEMAND:
                    gate("DEMAND_FROM_WEAK_SOURCES", cid,
                         f"demand stated as fact but evidence is only {sorted(kinds)} - only "
                         f"{'/'.join(sorted(STRONG_FOR_DEMAND))} sources establish demand (vendor pages, reviews, "
                         "search, interviews and synthetic answers don't); make it a hypothesis or add stronger evidence")
            syn = synthetic_ancestor(cid)
            if syn:
                gate("FACT_ON_SYNTHETIC", cid, f"a fact can't rest on synthetic material, even indirectly: {syn}")
            if FUTURE_RE.search(c["text"]):
                flag("FACT_LOOKS_LIKE_FORECAST", cid, "future/expectation wording in a fact - check the type")
        if c["status"] == "review_required":
            flag("UNDER_REVIEW", cid, "basis changed - see review events")

    def live(i):
        """A claim or record that is still current work (not history)."""
        n = claims.get(i) or records.get(i)
        return n is not None and n["status"] not in RETIRED

    # retired items are history: kept for the record, not re-judged and not counted (R037)
    active = {k: v for k, v in records.items() if v["status"] not in RETIRED}
    for nid in [c for c in claims if live(c)] + list(active):
        old = sorted({e["to_id"] for e in out_edges.get(nid, []) if e["kind"] != "contradicts"
                      and (e["to_id"] in claims or e["to_id"] in records) and not live(e["to_id"])})
        if old:
            flag("RESTS_ON_RETIRED", nid, f"still rests on history {', '.join(old)} - re-point it or re-check it")

    for rid, r in active.items():
        deps = [e["to_id"] for e in out_edges.get(rid, []) if e["kind"] == "depends"]
        if r["kind"] != "question" and not deps:
            gate("RECORD_WITHOUT_BASIS", rid, f"{r['kind']} must depend on claims or records")
        if r["status"] == "review_required":
            flag("UNDER_REVIEW", rid, "basis changed - see review events")

    # dependency graph must be acyclic (DAG)
    graph = {}
    for e in edges:
        graph.setdefault(e["from_id"], []).append(e["to_id"])
    state = {}

    def visit(n, path):
        state[n] = 1
        for m in graph.get(n, []):
            if state.get(m) == 1:
                gate("CYCLE", n, " -> ".join(path + [n, m]))
            elif state.get(m) is None:
                visit(m, path + [n])
        state[n] = 2
    for n in list(graph):
        if state.get(n) is None:
            visit(n, [])

    options = {k: json.loads(v["fields"]) for k, v in active.items() if v["kind"] == "option"}
    if options:
        if len(options) < 3:
            gate("TOO_FEW_OPTIONS", None, f"{len(options)} options; the brief requires at least 3")
        roles = {f.get("role") for f in options.values()}
        for role, label in (("status_quo", "«оставить как есть»"), ("closure", "«закрыть направление»")):
            if role not in roles:
                gate("OPTION_ROLE_MISSING", None, f"no option with role={role} ({label})")
        for oid, f in options.items():
            for key in ("objection", "reversal"):
                if not str(f.get(key, "")).strip():
                    gate("OPTION_FIELD_MISSING", oid, f"option needs a non-empty '{key}'")

    def need(rid, fields, keys, code):
        missing = [k for k in keys if not str(fields.get(k, "")).strip()]
        if missing:
            gate(code, rid, f"needs {', '.join(missing)} (a value, or 'unknown: reason')")

    frames = [k for k, v in active.items() if v["kind"] == "question" and json.loads(v["fields"]).get("role") == "frame"]
    if len(frames) > 1:
        gate("FRAME_CONFLICT", None, f"{len(frames)} current frames ({', '.join(sorted(frames))}); supersede the old "
                                     "one so a new chat can't read stale constraints")
    decisions = sorted(k for k, v in active.items() if v["kind"] == "decision")
    if len(decisions) > 1:
        gate("DECISION_CONFLICT", None, f"{len(decisions)} current decisions ({', '.join(decisions)}); a new decision "
                                        "supersedes the old one (resolve --outcome supersede)")

    for rid, r in active.items():
        f = json.loads(r["fields"])
        deps = [e["to_id"] for e in out_edges.get(rid, []) if e["kind"] == "depends"]
        if r["kind"] in DOC_KEYS:
            missing = [k for k in DOC_KEYS[r["kind"]] if doc_value_missing(r["kind"], k, f.get(k))]
            if missing:
                gate(f"{r['kind'].upper()}_FIELD_MISSING", rid,
                     f"{r['kind']} needs {', '.join(missing)} (a value, or 'unknown: reason' / 'n/a: reason')")
            if r["kind"] == "forecast_row" and isinstance(f.get("ours"), dict) \
                    and not {"low", "base", "high"} <= set(f["ours"]):
                gate("FORECAST_RANGE_INCOMPLETE", rid, "ours as an object needs low, base and high")
            if r["kind"] == "calc":
                problem = calc_script_problem(project_dir, f.get("script"))
                if problem:
                    flag("CALC_SCRIPT_MISSING", rid, problem + " - a calculation must be reproducible")
            if r["kind"] == "section" and isinstance(f.get("body"), str):
                for para in unsourced_paragraphs(f["body"]):
                    flag("SECTION_NUMBER_WITHOUT_SOURCE", rid,
                         f"number without a source or calculation (part {f.get('part')}): «{para}» - cite the "
                         "record or claim id in square brackets, e.g. [CAL-0123456789]")
                for ref in sorted(set(ID_RE.findall(" ".join(REF_RE.findall(f["body"]))))):
                    if ref not in obs and ref not in claims and ref not in records:
                        flag("SECTION_REF_UNKNOWN", rid, f"part {f.get('part')} cites {ref}, which does not exist")
        elif r["kind"] == "question" and f.get("role") == "frame":
            need(rid, f, FRAME_KEYS, "FRAME_FIELD_MISSING")
        elif r["kind"] == "strategy":
            if f.get("type") not in STRATEGY_KEYS:
                gate("STRATEGY_TYPE_MISSING", rid, "strategy needs type 'product' or 'comms'")
            else:
                need(rid, f, STRATEGY_KEYS[f["type"]], "STRATEGY_FIELD_MISSING")
            if f.get("type") == "comms" and not any(
                    json.loads(active.get(d, {}).get("fields", "{}")).get("type") == "product" for d in deps):
                gate("COMMS_NOT_LINKED", rid, "a communication strategy must rest on the product strategy it serves")
        elif r["kind"] == "capability" and f.get("readiness") == "current":
            # a current fact: its own gates then require current observations, so the whole path is live (R037)
            if not any(claims.get(d, {}).get("type") == "fact" and live(d) for d in deps):
                gate("CAPABILITY_WITHOUT_EVIDENCE", rid, "a capability marked 'current' must rest on a current fact "
                                                       "claim showing it exists; self-declared readiness or a retired "
                                                       "fact can't back a promise")
        elif r["kind"] == "experiment":
            need(rid, f, EXPERIMENT_KEYS, "EXPERIMENT_FIELD_MISSING")
        elif r["kind"] == "decision":
            if f.get("actor") != "human" or not str(f.get("answer", "")).strip():
                gate("DECISION_WITHOUT_HUMAN", rid, "a decision records the human's own words (actor=human, answer); "
                                                    "the agent recommends, it never decides")
            chosen = f.get("option")
            if records.get(chosen, {}).get("kind") != "option" or chosen not in deps:
                gate("DECISION_WITHOUT_OPTION", rid, "a decision names the option it chooses (fields.option) and "
                                                     "depends on it")
            elif not live(chosen):
                gate("DECISION_OPTION_RETIRED", rid, f"the chosen option {chosen} is history; the human decides again")

    parts = {unique_key("section", json.loads(r["fields"])) for r in active.values() if r["kind"] == "section"}
    if parts:   # only once the document has been started, so older projects are not affected
        for part in DOC_PARTS:
            if part not in parts:
                flag("DOC_PART_MISSING", None, f"the final document has no section part {part}")

    for rid, r in active.items():
        if r["kind"] != "message":
            continue
        f = json.loads(r["fields"])
        deps = [e["to_id"] for e in out_edges.get(rid, []) if e["kind"] == "depends"]
        caps = [active[d] for d in deps if active.get(d, {}).get("kind") == "capability"]
        if not caps:
            vendor_only = deps and all(claims.get(d, {}).get("type") == "vendor_claim" for d in deps)
            gate("MESSAGE_WITHOUT_CAPABILITY", rid,
                 "a message must point to our own capability" + (" - vendor claims can't back our promise" if vendor_only else ""))
        for cap in caps:
            readiness = json.loads(cap["fields"]).get("readiness")
            if readiness != "current" and not f.get("future_proposition"):
                gate("PROMISE_NOT_READY", rid, f"capability {cap['id']} is '{readiness}', but the message is not "
                                              "marked as a future proposition")

    return {"passed": not gates, "structural_passed": not gates,
            "note": "gates are structural checks: they don't certify that a source says what a claim says, or that "
                    "demand is real - that stays human review",
            "gates": gates, "flags": flags,
                  "counts": {"observations": len(obs), "claims": len(claims), "records": len(records),
                             "edges": len(edges)}}


def claims_markdown(con):
    """Readable claims with full citations (the jury-facing view of the evidence chain)."""
    lines = ["# Утверждения и основания", "",
             "Файл создан автоматически из базы - не редактировать.", ""]
    obs = {r["id"]: dict(r) for r in con.execute(
        "SELECT o.*, s.uri, sv.retrieved_at, sv.source_kind FROM observation o "
        "JOIN source_version sv ON sv.id = o.source_version_id JOIN source s ON s.id = sv.source_id")}
    for c in con.execute("SELECT * FROM claim ORDER BY created_at, id"):
        scope = json.loads(c["scope"])
        lines.append(f"## {c['id']} · {c['type']} · {c['status']}")
        lines.append("")
        lines.append(c["text"])
        lines.append("")
        lines.append("Область: " + "; ".join(f"{k}: {scope.get(k, '-')}" for k in SCOPE_KEYS))
        ev = con.execute("SELECT to_id, kind FROM edge WHERE from_id=? ORDER BY kind, to_id", (c["id"],)).fetchall()
        for e in ev:
            o = obs.get(e["to_id"])
            if o:
                lines.append(f"- {e['kind']}: «{o['exact']}» - {o['uri']} ({o['hint']}; {o['context'] or 'без контекста'}; "
                             f"получено {o['retrieved_at']}; тип источника: {o['source_kind']}; {o['status']})")
            else:
                lines.append(f"- {e['kind']}: {e['to_id']}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def register(sub):
    def w(name, *req):
        sp = sub.add_parser(name)
        for f in ("--project", "--session") + req:
            sp.add_argument(f, required=True)
        sp.add_argument("--token", type=int, required=True)
        return sp
    sp = w("classify", "--source-version", "--source-kind")
    sp = w("observe", "--run", "--step", "--source-version", "--exact", "--kind")
    for f in ("--prefix", "--suffix", "--hint", "--value", "--unit", "--period", "--population"):
        sp.add_argument(f)
    sp = w("claim", "--run", "--step", "--type", "--text", "--scope")
    for f in ("--tags", "--supports", "--contradicts", "--depends"):
        sp.add_argument(f)
    sp = w("record", "--run", "--step", "--kind", "--title")
    for f in ("--fields", "--depends"):
        sp.add_argument(f)
    w("revalidate", "--run", "--step", "--old", "--new")
    sp = w("resolve", "--run", "--step", "--ids", "--outcome", "--reason")
    sp.add_argument("--by")
    sp.add_argument("--answer")
    for name in ("validate",):
        sub.add_parser(name).add_argument("--project", required=True)
    ip = sub.add_parser("impact")
    ip.add_argument("--project", required=True)
    ip.add_argument("--ids", required=True)


COMMANDS = {"classify": cmd_classify, "observe": cmd_observe, "claim": cmd_claim, "record": cmd_record,
            "impact": cmd_impact, "revalidate": cmd_revalidate, "resolve": cmd_resolve, "validate": cmd_validate}
