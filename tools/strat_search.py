"""Find claims, observations and records by wording (build step 5). Read-only.

The index is built at query time in the connection's temp schema and dropped with the connection, so it
can never be stale and nothing is persisted. Two FTS5 indexes: `unicode61` (whole words) and `trigram`
(substrings - helps with Russian word forms, but it is not morphology). Every user term is quoted as an
FTS5 string, so syntax characters (" * : ( ) - AND OR NEAR ^) are plain text. Without FTS5 the search
falls back to a case-insensitive substring scan in Python (SQLite's own LIKE folds ASCII letters only);
`mode` in the output says which was used. STRAT_TEST_NO_FTS=1 forces the fallback (tests).
"""
import json
import os
import sqlite3

import strat as S
import strat_evidence as E

SNIPPET_TOKENS = 12


def documents(con, include_history):
    """(id, kind, subtype, status, current, body) for every searchable item."""
    docs = []
    for r in con.execute("SELECT id, type, status, text FROM claim ORDER BY id"):
        docs.append((r["id"], "claim", r["type"], r["status"], r["status"] not in E.RETIRED, r["text"]))
    for r in con.execute("SELECT id, kind, status, exact, value, unit FROM observation ORDER BY id"):
        body = " ".join(x for x in (r["exact"], r["value"], r["unit"]) if x)
        docs.append((r["id"], "observation", r["kind"], r["status"], r["status"] != "superseded", body))
    for r in con.execute("SELECT id, kind, status, title, fields FROM record ORDER BY id"):
        f = json.loads(r["fields"])
        parts = [r["title"]] + [v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in f.values()]
        docs.append((r["id"], "record", r["kind"], r["status"], r["status"] not in E.RETIRED, " ".join(parts)))
    return [d for d in docs if include_history or d[4]]


def fts_query(terms):
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def fts_search(con, tok, docs, terms):
    """Return {id: (rank, snippet)} or None if this tokenizer is unavailable."""
    if os.environ.get("STRAT_TEST_NO_FTS") == "1":
        return None
    name = f"temp.search_{tok}"
    try:
        con.execute(f"CREATE VIRTUAL TABLE {name} USING fts5(id UNINDEXED, body, tokenize='{tok}')")
    except sqlite3.OperationalError:
        return None
    con.executemany(f"INSERT INTO {name} (id, body) VALUES (?, ?)", [(d[0], d[5]) for d in docs])
    rows = con.execute(f"SELECT id, bm25(search_{tok}) r, snippet(search_{tok}, 1, '[', ']', '...', "
                       f"{SNIPPET_TOKENS}) s FROM {name} WHERE {name.split('.')[1]} MATCH ? ORDER BY r",
                       (fts_query(terms),)).fetchall()
    return {r["id"]: (r["r"], r["s"]) for r in rows}


def scan(docs, terms):
    """Fallback: every term must occur as a case-insensitive substring."""
    out = {}
    for d in docs:
        body = d[5].casefold()
        if all(t.casefold() in body for t in terms):
            i = body.find(terms[0].casefold())
            start = max(0, i - 40)
            text = d[5][start:i] + "[" + d[5][i:i + len(terms[0])] + "]" + d[5][i + len(terms[0]):i + 80]
            out[d[0]] = (0, ("..." if start else "") + text + ("..." if i + 80 < len(d[5]) else ""))
    return out


def cmd_search(p, a):
    terms = (a.query or "").split()
    if not terms:
        raise S.StratError("VALIDATION_FAILED", "--query needs at least one word")
    if not 1 <= a.limit <= 500:
        raise S.StratError("VALIDATION_FAILED", "--limit must be between 1 and 500")
    con = p.connect()
    con.execute("BEGIN")   # one snapshot
    try:
        docs = documents(con, a.include_history)
        words = fts_search(con, "unicode61", docs, terms)
        subs = fts_search(con, "trigram", docs, terms)
        mode = "+".join((["fts5_unicode61"] if words is not None else [])
                        + (["fts5_trigram"] if subs is not None else ["like"]))
        if subs is None:
            subs = scan(docs, terms)
        hits = list((words or {}).items()) + sorted(((i, v) for i, v in subs.items() if i not in (words or {})),
                                                    key=lambda kv: kv[1][0])
        meta = {d[0]: d for d in docs}
        out = []
        for i, (_, snip) in hits[:a.limit]:
            d = meta[i]
            hit = {"id": i, "kind": d[1], "subtype": d[2], "status": d[3], "current": d[4], "snippet": snip,
                   "matched_by": "words" if words and i in words else "substring"}
            if d[1] == "claim":
                hit["supported_by"] = [dict(r) for r in con.execute(
                    "SELECT o.id AS observation, o.source_version_id AS source_version, o.status FROM edge e "
                    "JOIN observation o ON o.id = e.to_id WHERE e.from_id=? AND e.kind='supports' ORDER BY o.id",
                    (i,))]
            out.append(hit)
    finally:
        con.execute("COMMIT")
    return "ok", {"query": a.query, "mode": mode, "include_history": bool(a.include_history),
                  "count": len(out), "total_matches": len(hits), "hits": out,
                  "note": "trigram matching is substring matching, not morphology; history is excluded unless "
                          "--include-history"}


def register(sub):
    sp = sub.add_parser("search")
    sp.add_argument("--project", required=True)
    sp.add_argument("--query", required=True)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--include-history", action="store_true")


COMMANDS = {"search": cmd_search}
