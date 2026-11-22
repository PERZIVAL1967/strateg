"""Build step 5 gate: cost ledger, report.md, portable archive/restore, search by wording.

Run: python -m unittest discover -s tests -v   (from the agent folder; macOS, Linux, Windows)
"""
import json
import pathlib
import sqlite3
import sys
import zipfile

from test_step3 import Base, run
from test_step4 import COMMS_STRATEGY, EXPERIMENT, FRAME

AGENT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / "tools"))
import strat  # noqa: E402
import strat_archive  # noqa: E402


class Cost(Base):
    def cost(self, *args, token=None, step=None):
        if step is None:
            self.n += 1
            step = self.n
        return run(self.root, "cost", "--project", "demo", "--session", "s", "--token", token or self.token,
                   "--run", "r1", "--step", str(step), *args)

    def ctx_cost(self):
        return run(self.root, "context", "--project", "demo")[1]["data"]["cost"]

    def test_nothing_logged_is_unknown_not_zero(self):
        c = self.ctx_cost()
        self.assertEqual((c["totals"], c["model_cost_logged"]), ([], False))
        self.assertEqual(c["unknown"], ["model", "search", "tool", "human"])

    def test_totals_by_kind_unit_basis(self):
        for args in (("--kind", "model", "--amount", "1.25", "--unit", "usd", "--basis", "ide"),
                     ("--kind", "model", "--amount", "0.75", "--unit", "usd", "--basis", "ide"),
                     ("--kind", "human", "--amount", "30", "--unit", "minutes", "--basis", "measured",
                      "--note", "правка отчёта")):
            code, out = self.cost("--stage", "researching", *args)
            self.assertEqual(code, 0, out)
        c = self.ctx_cost()
        self.assertTrue(c["model_cost_logged"])
        self.assertEqual(c["unknown"], ["search", "tool"])
        self.assertIn({"kind": "model", "unit": "usd", "basis": "ide", "amount": 2.0, "entries": 2}, c["totals"])
        self.assertIn({"kind": "human", "unit": "minutes", "basis": "measured", "amount": 30.0, "entries": 1},
                      c["totals"])

    def test_bad_token_is_fenced(self):
        code, out = self.cost("--stage", "framing", "--kind", "model", "--amount", "1", "--unit", "usd",
                              "--basis", "ide", token=str(int(self.token) + 1))
        self.assertEqual(out["error_code"], "FENCED")
        self.assertEqual(self.ctx_cost()["totals"], [])

    def test_repeat_is_skipped_and_other_input_conflicts(self):
        def args(amount):
            return ("--stage", "framing", "--kind", "search", "--amount", amount, "--unit", "rub",
                    "--basis", "estimate")
        first = self.cost(*args("3"), step=7)[1]
        again = self.cost(*args("3"), step=7)[1]
        self.assertEqual((again["status"], again["data"]["cost_id"]), ("skipped", first["data"]["cost_id"]))
        other = self.cost(*args("4"), step=7)[1]   # same run/step, another amount
        self.assertEqual(other["error_code"], "CONFLICT")
        self.assertEqual(self.ctx_cost()["totals"][0]["entries"], 1)

    def test_bad_values_refused_in_the_envelope(self):
        base = {"--stage": "framing", "--kind": "model", "--amount": "1", "--unit": "usd", "--basis": "ide"}
        for key, bad in (("--amount", "-1"), ("--amount", "abc"), ("--amount", "nan"), ("--amount", "inf"),
                         ("--kind", "coffee"), ("--unit", "eur"), ("--basis", "guess"), ("--stage", "whenever")):
            args = [x for k, v in {**base, key: bad}.items() for x in (k, v)]
            code, out = self.cost(*args)
            self.assertEqual(out["error_code"], "VALIDATION_FAILED", (key, bad))


class Report(Base):
    def export_report(self):
        code, out = run(self.root, "export", "--project", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn("report.md", out["data"]["files"])
        return pathlib.Path(self.root, "projects/demo/exports/report.md").read_text(encoding="utf-8")

    def test_empty_project_says_why_each_section_is_empty(self):
        md = self.export_report()
        for n in range(1, 15):
            self.assertIn(f"## {n}. ", md)
        self.assertIn("Ещё не сделано: постановка задачи не записана.", md)
        self.assertIn("Решение не принято", md)
        self.assertIn("Расход модели не записан", md)
        self.assertNotIn("—", md)
        m = json.loads(pathlib.Path(self.root, "projects/demo/exports/manifest.json").read_text(encoding="utf-8"))
        self.assertIn("report.md", m["files"])
        self.assertEqual(run(self.root, "doctor", "--project", "demo")[1]["data"]["exports"]["state"], "current")

    def test_full_report_current_only_with_contradiction(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        ids = self.build_chain()
        contra = self.claim("hypothesis", "Подключение eSIM понятно всем клиентам.", contradicts=ids["o_rev"])
        new_price = self.claim("fact", "Цена пакета - 990 руб. за неделю (уточнено).", supports=ids["o_price"])
        self.w("resolve", "--ids", ids["c_price"], "--outcome", "supersede", "--by", new_price, "--reason", "уточнено")
        self.record("strategy", "Объяснить подключение", COMMS_STRATEGY, ids["strategy"])
        self.record("experiment", "Тест мастера", EXPERIMENT, ids["guide"])
        dec = self.record("decision", "Делаем мастер", {"actor": "human", "answer": "Да, делаем мастер",
                                                        "option": ids["guide"], "decided_at": "2026-11-11"},
                          ids["guide"])
        self.record("commitment", "Запуск мастера — к марту", {"owner": "продакт", "metric": "доля подключений",
                                                                  "due": "2027-03-01", "review_if": "доля < 50%"}, dec)
        self.n += 1
        run(self.root, "cost", "--project", "demo", "--session", "s", "--token", self.token, "--run", "r1",
            "--step", str(self.n), "--stage", "strategy", "--kind", "model", "--amount", "2.5", "--unit", "usd",
            "--basis", "ide")
        md = self.export_report()
        self.assertNotIn(ids["c_price"], md, "superseded claim is history, not in the report")
        self.assertNotIn("стоит 990 руб. за 7 дней.", md)
        self.assertIn(f"Цена пакета - 990 руб. за неделю (уточнено). [1] - `{new_price}`", md)
        self.assertIn("## 6. Противоречия", md)
        section6 = md.split("## 6. Противоречия")[1].split("## 7.")[0]
        self.assertIn(contra, section6)
        self.assertIn("«не понимаю, как подключить» [2]", section6)
        self.assertIn("(оставить как есть)", md)
        self.assertIn("(закрыть направление)", md)
        self.assertIn("Ключевое сообщение: подключение за пару шагов", md)
        self.assertIn("(готовность: планируется)", md)
        self.assertIn("слова человека: «Да, делаем мастер»", md)
        self.assertIn("economics - unknown: нет данных о доле отказов", md)
        self.assertIn("Запуск мастера - к марту", md)
        self.assertNotIn("—", md)
        self.assertNotIn("Расход модели не записан", md)
        self.assertIn("модель: 2.5 долл. США (по данным среды", md)
        self.assertIn("tariffs.html", md.split("## 14. Источники")[1])


def rezip(src, dst, mutate):
    """Copy an archive, letting `mutate(members, manifest)` change it."""
    with zipfile.ZipFile(src) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    mutate(members, manifest)
    with zipfile.ZipFile(dst, "w") as zf:
        for n, b in members.items():
            zf.writestr(n, b)
        zf.writestr("manifest.json", json.dumps(manifest))


class Archive(Base):
    def setUp(self):
        super().setUp()
        self.ids = self.build_chain()
        self.zip = str(pathlib.Path(self.root, "demo.zip"))

    def archive(self, out=None):
        return run(self.root, "archive", "--project", "demo", "--out", out or self.zip)

    def restore(self, file, project="copy"):
        return run(self.root, "restore", "--file", file, "--project", project)

    def assert_nothing_restored(self, project="copy"):
        projects = pathlib.Path(self.root, "projects")
        self.assertFalse((projects / project).exists())
        self.assertEqual([p.name for p in projects.iterdir() if p.name.startswith(".restore")], [])

    def test_round_trip_is_a_fork(self):
        code, out = self.archive()
        self.assertEqual(code, 0, out)
        with zipfile.ZipFile(self.zip) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(names[-2:], ["manifest.json", "state.sqlite"])
        self.assertEqual(len(names), 4, "two blobs + DB + manifest")
        st = run(self.root, "status", "--project", "demo")[1]["data"]["lease"]
        self.assertEqual(st["owner_session"], "s", "the original's lease is untouched")
        code, out = self.restore(self.zip)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["data"]["fencing_token"], int(self.token) + 1)
        lease = run(self.root, "status", "--project", "copy")[1]["data"]["lease"]
        self.assertIsNone(lease["owner_session"])
        old = run(self.root, "record", "--project", "copy", "--session", "s", "--token", self.token, "--run", "r9",
                  "--step", "1", "--kind", "question", "--title", "q")[1]
        self.assertEqual(old["error_code"], "FENCED", "an old chat's token can't write to the copy")
        a = run(self.root, "context", "--project", "demo")[1]["data"]
        b = run(self.root, "context", "--project", "copy")[1]["data"]
        for k in ("stage", "counts", "db_revision", "history", "cost"):
            self.assertEqual(a[k], b[k], k)
        self.assertEqual(run(self.root, "validate", "--project", "copy")[1]["data"]["gates"],
                         run(self.root, "validate", "--project", "demo")[1]["data"]["gates"])
        self.assertEqual(run(self.root, "status", "--project", "copy")[1]["data"]["source_versions_missing_blob"], [])
        tok = run(self.root, "lease", "--project", "copy", "--session", "chatB")[1]["data"]["fencing_token"]
        self.assertEqual(tok, int(self.token) + 2)

    def test_existing_out_file_is_refused(self):
        pathlib.Path(self.zip).write_bytes(b"keep me")
        self.assertEqual(self.archive()[1]["error_code"], "CONFLICT")
        self.assertEqual(pathlib.Path(self.zip).read_bytes(), b"keep me")

    def test_missing_blob_refuses_archive(self):
        con = sqlite3.connect(pathlib.Path(self.root, "projects/demo/state.sqlite"))
        digest = con.execute("SELECT sha256 FROM source_version LIMIT 1").fetchone()[0]
        con.close()
        pathlib.Path(self.root, "projects/demo/blobs/sha256", digest[:2], digest).unlink()
        code, out = self.archive()
        self.assertEqual(out["error_code"], "BLOB_MISSING")
        self.assertEqual(out["data"]["blobs"], [digest])
        self.assertEqual([p.name for p in pathlib.Path(self.root).iterdir() if p.name.startswith("demo.zip")], [])

    def test_restore_into_existing_project_is_refused(self):
        self.archive()
        before = pathlib.Path(self.root, "projects/demo/state.sqlite").read_bytes()
        self.assertEqual(self.restore(self.zip, "demo")[1]["error_code"], "CONFLICT")
        self.assertEqual(pathlib.Path(self.root, "projects/demo/state.sqlite").read_bytes(), before)

    def test_traversal_and_unlisted_members_are_refused(self):
        self.archive()
        evil = pathlib.Path(self.root, "evil.zip")

        def add(name, listed):
            def mutate(members, manifest):
                members[name] = b"x"
                if listed:
                    manifest["members"][name] = {"sha256": strat.sha256_bytes(b"x"), "size": 1}
            return mutate
        for name, listed in (("../evil.txt", True), ("/abs.txt", True), ("blobs/../../evil.txt", True),
                             ("blobs\\sha256\\x", True), ("notes.txt", False),
                             ("blobs/sha256/2d/" + strat.sha256_bytes(b"x"), False)):
            rezip(self.zip, evil, add(name, listed))
            code, out = self.restore(str(evil))
            self.assertEqual(out["error_code"], "VALIDATION_FAILED", name)
            self.assert_nothing_restored()
        self.assertFalse(pathlib.Path(self.root, "evil.txt").exists())
        self.assertFalse(pathlib.Path(self.root, "projects", "evil.txt").exists())

    def test_tampered_blob_or_schema_leaves_nothing(self):
        self.archive()
        bad = pathlib.Path(self.root, "bad.zip")

        def tamper(members, manifest):
            blob = next(n for n in members if n.startswith("blobs/"))
            members[blob] = members[blob][:-1] + b"!"
        rezip(self.zip, bad, tamper)
        self.assertEqual(self.restore(str(bad))[1]["error_code"], "VALIDATION_FAILED")
        self.assert_nothing_restored()
        rezip(self.zip, bad, lambda m, man: man.update(schema_version=6))
        self.assertEqual(self.restore(str(bad))[1]["error_code"], "UNSUPPORTED_SCHEMA")
        self.assert_nothing_restored()
        rezip(self.zip, bad, lambda m, man: m.pop(next(n for n in m if n.startswith("blobs/"))) and
              man["members"].pop(next(n for n in man["members"] if n not in m)))
        self.assertEqual(self.restore(str(bad))[1]["error_code"], "BLOB_MISSING", "a blob the DB needs is absent")
        self.assert_nothing_restored()
        bad.write_bytes(b"not a zip")
        self.assertEqual(self.restore(str(bad))[1]["error_code"], "VALIDATION_FAILED")

    def test_total_size_cap(self):
        self.archive()
        old = strat_archive.MAX_TOTAL
        strat_archive.MAX_TOTAL = 100
        try:
            with zipfile.ZipFile(self.zip) as zf, self.assertRaises(strat.StratError) as e:
                strat_archive._check_members(zf)
            self.assertEqual(e.exception.code, "TOO_LARGE")
        finally:
            strat_archive.MAX_TOTAL = old


class Search(Base):
    def setUp(self):
        super().setUp()
        self.ids = self.build_chain()

    def search(self, query, *extra, env=None):
        code, out = run(self.root, "search", "--project", "demo", "--query", query, *extra, env=env)
        self.assertEqual(code, 0, out)
        return out["data"]

    def test_words_and_substrings(self):
        doctor = run(self.root, "doctor", "--project", "demo")[1]["data"]
        d = self.search("подключить")
        self.assertIn(self.ids["o_rev"], {h["id"] for h in d["hits"]})
        if doctor["fts5_unicode61"] and doctor["fts5_trigram"]:
            self.assertEqual(d["mode"], "fts5_unicode61+fts5_trigram")
        d = self.search("подключ")   # a word stem: only substring matching finds «подключает»
        hit = next(h for h in d["hits"] if h["id"] == self.ids["c_pain"])
        self.assertEqual((hit["kind"], hit["subtype"], hit["current"]), ("claim", "hypothesis", True))
        self.assertEqual(hit["supported_by"][0]["observation"], self.ids["o_rev"])
        self.assertEqual(hit["supported_by"][0]["source_version"], self.ids["r"])
        self.assertIn("[", hit["snippet"])
        d = self.search("мастер подключения")
        kinds = {(h["kind"], h["subtype"]) for h in d["hits"]}
        self.assertIn(("record", "capability"), kinds)

    def test_fts_syntax_characters_are_plain_text(self):
        for q in ('"', '""', "AND", "OR NEAR(", "*", "цена: (990)", "-", "'", "^руб", "a:b", "{x}", "990*"):
            d = self.search(q)
            self.assertIsInstance(d["hits"], list, q)
        self.assertIn(self.ids["o_price"], {h["id"] for h in self.search('"990 руб.')["hits"]})

    def test_history_is_excluded_by_default(self):
        new = self.claim("fact", "Цена пакета 990 рублей за неделю.", supports=self.ids["o_price"])
        self.w("resolve", "--ids", self.ids["c_price"], "--outcome", "supersede", "--by", new, "--reason", "уточнено")
        ids = {h["id"] for h in self.search("Путешествие")["hits"]}
        self.assertNotIn(self.ids["c_price"], ids)
        hits = {h["id"]: h for h in self.search("Путешествие", "--include-history")["hits"]}
        self.assertEqual((hits[self.ids["c_price"]]["status"], hits[self.ids["c_price"]]["current"]),
                         ("superseded", False))

    def test_fallback_scan_without_fts(self):
        d = self.search("ПОДКЛЮЧ", env={"STRAT_TEST_NO_FTS": "1"})
        self.assertEqual(d["mode"], "like")
        self.assertIn(self.ids["c_pain"], {h["id"] for h in d["hits"]})

    def test_empty_query_and_limit(self):
        code, out = run(self.root, "search", "--project", "demo", "--query", "   ")
        self.assertEqual(out["error_code"], "VALIDATION_FAILED")
        self.assertEqual(len(self.search("руб", "--limit", "1")["hits"]), 1)

    def test_search_writes_nothing(self):
        db = pathlib.Path(self.root, "projects/demo/state.sqlite")
        before = db.read_bytes()
        self.search("подключ")
        self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    import unittest
    unittest.main()
