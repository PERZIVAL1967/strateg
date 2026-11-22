"""Cost and time ledger for strat (build step 5).

The brief asks that the user can see what the work cost: models, search, tools, retries, human rework.
The tool cannot see model spend, so the agent logs it with `cost`, saying where the number came from:
  basis  ide       the number the IDE showed
         measured  counted by the agent (e.g. minutes from timestamps)
         estimate  a guess, labelled as one
A kind with no entries is reported as unknown, never as zero.
"""
import math

import strat as S
import strat_evidence as E

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_entry (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL, stage TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('model','search','tool','human')),
  amount REAL NOT NULL CHECK (amount >= 0),
  unit TEXT NOT NULL CHECK (unit IN ('usd','rub','tokens','minutes')),
  basis TEXT NOT NULL CHECK (basis IN ('ide','measured','estimate')),
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
"""

KINDS = ("model", "search", "tool", "human")
UNITS = ("usd", "rub", "tokens", "minutes")
BASES = ("ide", "measured", "estimate")


def cmd_cost(p, a):
    import strat_context
    stages = tuple(strat_context.STAGES) + ("other",)
    for name, value, allowed in (("--kind", a.kind, KINDS), ("--unit", a.unit, UNITS), ("--basis", a.basis, BASES),
                                 ("--stage", a.stage, stages)):
        if value not in allowed:
            raise S.StratError("VALIDATION_FAILED", f"{name} must be one of {', '.join(allowed)}")
    try:
        amount = float(a.amount)
    except ValueError:
        amount = float("nan")
    if not math.isfinite(amount) or amount < 0:
        raise S.StratError("VALIDATION_FAILED", f"--amount must be a number >= 0, got {a.amount!r}")
    payload = {"stage": a.stage, "kind": a.kind, "amount": amount, "unit": a.unit, "basis": a.basis,
               "note": a.note or ""}
    con = p.connect()
    S.write_tx(con, a.session, a.token)
    prev, input_hash = E.idempotent(con, a, "cost", payload)
    if prev:
        return "skipped", {"cost_id": prev, "reason": "already recorded"}
    cid = E.new_id("COST")
    con.execute("INSERT INTO cost_entry VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, a.run, a.step, a.stage, a.kind, amount, a.unit, a.basis, a.note or "", S.now()))
    E.finish_op(con, a, "cost", input_hash, cid)
    S.bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"cost_id": cid, **payload}


def summary(con):
    """Totals per kind + unit + basis. Kinds without entries are listed as unknown - never as zero."""
    totals = [{"kind": r["kind"], "unit": r["unit"], "basis": r["basis"], "amount": round(r["s"], 6),
               "entries": r["n"]}
              for r in con.execute("SELECT kind, unit, basis, SUM(amount) s, COUNT(*) n FROM cost_entry "
                                   "GROUP BY kind, unit, basis ORDER BY kind, unit, basis")]
    logged = {t["kind"] for t in totals}
    retries = con.execute("SELECT COUNT(*) FROM op_attempt WHERE outcome IN ('failed','unknown','superseded')"
                          ).fetchone()[0]
    return {"totals": totals, "model_cost_logged": "model" in logged,
            "unknown": [k for k in KINDS if k not in logged],
            "failed_or_repeated_attempts": retries,
            "note": "logged by the agent; a kind in `unknown` was not logged - its cost is unknown, not zero"}


def register(sub):
    sp = sub.add_parser("cost")
    for f in ("--project", "--session", "--run", "--step", "--stage", "--kind", "--amount", "--unit", "--basis"):
        sp.add_argument(f, required=True)
    sp.add_argument("--token", type=int, required=True)
    sp.add_argument("--note")


COMMANDS = {"cost": cmd_cost}
