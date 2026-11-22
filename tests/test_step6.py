"""Build step 6 gate: the final document (schema 8 record kinds, document.md, completeness flags, the
`documenting` stage) and the rehearsal fixes (TLS fallback, empty bodies, abandon, read, demand allowlist).

Run: python -m unittest discover -s tests -v   (from the agent folder; macOS, Linux, Windows)
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from test_step2 import LOOP, Server
from test_step3 import Base, REVIEWS, TARIFFS, run
from test_step4 import COMMS_STRATEGY, EXPERIMENT, FRAME, write_document

AGENT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / "tools"))
import strat  # noqa: E402
import strat_evidence as E  # noqa: E402

FULL = {
    "section": {"part": "1", "body": "Клиент замечает перебой первым."},
    "calc": {"script": "work/calc-gap.py", "outputs": {"gap_mln": {"low": 4, "base": 6, "high": 8}},
             "assumptions": "конверсия 2-5% (бриф, учебные данные)"},
    "forecast_row": {"metric": "Подключения, тыс.", "no_change": 120, "ours": {"low": 130, "base": 150, "high": 170},
                     "driver": "приглашение в момент перебоя"},
    "quarter": {"q": 1, "known": "доля откликов на приглашение", "decision": "расширять ли пилот"},
    "resource": {"role": "аналитик", "person_months": 3, "period": "Q1-Q2", "owner": "руководитель продукта",
                 "open_question": "unknown: доступ к данным биллинга"},
    "risk": {"signal": "рост обращений", "consequence": "нагрузка на поддержку", "owner": "поддержка",
             "action": "пауза рассылки"},
    "ask": {"when": "Q1", "decision": "запуск пилота", "resources": "2 разработчика", "owner": "директор"},
    "target": {"horizon": "конец 3-го года", "metric": "доля семей с услугой", "numerator": "семьи с услугой",
               "base": "все семейные тарифы", "condition": "пилот подтвердил порог"},
}


class Doc(Base):
    def setUp(self):
        super().setUp()
        self.c = self.claim("hypothesis", "Семьи узнают о перебое поздно.")

    def rec(self, kind, fields, title=None, depends=None):
        return self.record(kind, title or kind, fields, depends or self.c)

    def ctx(self):
        return run(self.root, "context", "--project", "demo")[1]["data"]

    def flags(self, code):
        return [f for f in self.validate()["flags"] if f["code"] == code]

    def export(self):
        code, out = run(self.root, "export", "--project", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn("document.md", out["data"]["files"])
        self.assertIn("report.md", out["data"]["files"])
        return pathlib.Path(self.root, "projects/demo/exports/document.md").read_text(encoding="utf-8")

    def work_script(self, name="calc-gap.py"):
        f = pathlib.Path(self.root, "projects/demo/work", name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("print(6)\n", encoding="utf-8")

    # 1. required fields per kind
    def test_each_new_kind_needs_its_fields(self):
        self.work_script()
        for kind, full in FULL.items():
            key = E.UNIQUE_KEY.get(kind)
            stub = {key: full[key]} if key else {}
            bad = self.rec(kind, stub)
            gates = [g for g in self.validate()["gates"] if g["id"] == bad]
            self.assertEqual([g["code"] for g in gates], [f"{kind.upper()}_FIELD_MISSING"], kind)
            for k in E.DOC_KEYS[kind]:
                if k != key:
                    self.assertIn(k, gates[0]["message"], (kind, k))
            good = self.rec(kind, full)   # supersedes the stub where the kind is unique per key
            self.assertFalse([g for g in self.validate()["gates"] if g["id"] == good], kind)
            if not key:
                self.w("resolve", "--ids", bad, "--outcome", "withdraw", "--reason", "заглушка")
        self.assertTrue(self.validate()["passed"], self.validate()["gates"])

    def test_unknown_with_reason_is_a_value_and_keys_are_checked(self):
        r = self.rec("risk", {"signal": "unknown: нет данных", "consequence": "n/a: пилот", "owner": "x",
                              "action": "y"})
        self.assertFalse([g for g in self.validate()["gates"] if g["id"] == r])
        for kind, fields in (("section", {"part": "6", "body": "x"}), ("quarter", {"q": 5}),
                             ("ask", {"when": "Q3"}), ("section", {"body": "без части"})):
            code, out = self.w("record", "--kind", kind, "--title", "x", "--fields", json.dumps(fields),
                               "--depends", self.c)
            self.assertEqual(out["error_code"], "VALIDATION_FAILED", (kind, fields))
        code, out = self.w("record", "--kind", "essay", "--title", "x", "--depends", self.c)
        self.assertEqual(out["error_code"], "VALIDATION_FAILED")

    def test_forecast_range_needs_low_base_high(self):
        r = self.rec("forecast_row", dict(FULL["forecast_row"], ours={"low": 1, "base": 2}))
        self.assertIn(("FORECAST_RANGE_INCOMPLETE", r), [(g["code"], g["id"]) for g in self.validate()["gates"]])
        self.rec("forecast_row", dict(FULL["forecast_row"], ours=150))   # a plain number is fine (and supersedes)
        self.assertTrue(self.validate()["passed"])

    def test_experiment_needs_pilot_design_keys(self):
        old = {k: EXPERIMENT[k] for k in ("expected_change", "baseline", "period", "side_effects")}
        r = self.rec("experiment", old)
        g = [g for g in self.validate()["gates"] if g["id"] == r]
        self.assertEqual(g[0]["code"], "EXPERIMENT_FIELD_MISSING")
        for k in ("unit", "assignment", "itt", "sample_size", "recruitment", "threshold", "stop_rule"):
            self.assertIn(k, g[0]["message"])
        r2 = self.rec("experiment", dict(EXPERIMENT, itt="n/a: нет случайного распределения"))
        self.assertFalse([g for g in self.validate()["gates"] if g["id"] == r2])

    # 2. supersede by key
    def test_new_section_of_same_part_supersedes_the_old(self):
        first = self.rec("section", {"part": "2", "body": "Черновик."})
        code, out = self.w("record", "--kind", "section", "--title", "v2", "--fields",
                           json.dumps({"part": "2", "body": "Чистовик."}, ensure_ascii=False), "--depends", self.c)
        self.assertEqual(out["data"]["superseded"], [first])
        second = out["data"]["record_id"]
        other = self.rec("section", {"part": "3", "body": "Другая часть."})
        c = self.ctx()
        self.assertEqual(c["counts"]["section"], 2)
        h = next(x for x in c["history"] if x["id"] == first)
        self.assertEqual((h["status"], h["by"]), ("superseded", second))
        self.assertIn("supersede:", h["resolution"])
        md = self.export()
        self.assertIn("Чистовик.", md)
        self.assertNotIn("Черновик.", md)
        self.assertNotIn(other, [x["id"] for x in c["history"]])
        for kind, a, b in (("quarter", {"q": "1"}, {"q": 1}), ("ask", {"when": "q1"}, {"when": "Q1"}),
                           ("forecast_row", {"metric": "ARPU,  руб."}, {"metric": "arpu, руб."})):
            x = self.rec(kind, dict(FULL[kind], **a))
            self.rec(kind, dict(FULL[kind], **b))
            st = {r["id"]: r["status"] for r in self.ctx_records(kind)}
            self.assertEqual(st[x], "superseded", kind)

    def ctx_records(self, kind):
        con = sqlite3.connect(pathlib.Path(self.root, "projects/demo/state.sqlite"))
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute("SELECT id, status FROM record WHERE kind=?", (kind,))]
        finally:
            con.close()

    # 3. document.md
    def test_document_has_all_headings_tables_and_calcs(self):
        md = self.export()
        for h in ("## Главное", "## 1. Клиент и его проблема", "## 2. Предложение и путь к клиенту",
                  "## 3. Польза для клиента и прибыль оператора", "## 4. Стратегический выбор и план по кварталам",
                  "## 5. Ресурсы, риски и решение руководителя", "## Коммуникационная стратегия"):
            self.assertIn(h, md)
        self.assertEqual(md.count("(не написано)"), 7)
        self.assertIn("report.md", md.strip().splitlines()[-1])
        self.work_script()
        self.work_script("calc-reach.py")
        gap = self.rec("calc", FULL["calc"], "Разрыв в прибыли")
        reach = self.rec("calc", dict(FULL["calc"], script="work/calc-reach.py", outputs={"reach": 1200}),
                         "Охват по каналам")
        fr = self.rec("forecast_row", FULL["forecast_row"], depends=gap)
        q2 = self.rec("quarter", dict(FULL["quarter"], q=2, known="итог пилота"))
        self.rec("quarter", FULL["quarter"])
        for kind in ("target", "resource", "risk", "ask"):
            self.rec(kind, FULL[kind])
        self.rec("section", {"part": "2", "body": f"Охват по каналам - [{reach}]."}, depends=reach)
        self.rec("section", {"part": "summary", "body": "Главное в одном абзаце."})
        md = self.export()
        p2, p3 = md.split("## 2. ")[1].split("## 3. ")[0], md.split("## 3. ")[1].split("## 4. ")[0]
        p4, p5 = md.split("## 4. ")[1].split("## 5. ")[0], md.split("## 5. ")[1].split("## Коммуникационная")[0]
        self.assertIn(f"[{reach}]", p2.split("### Расчёты")[1], "a calc sits under the part that rests on it")
        self.assertIn("`work/calc-reach.py`", p2)
        self.assertIn("### Расчёты", p3, "an unplaced calc falls back to part 3")
        self.assertIn("gap_mln: 4 / 6 / 8", p3)
        self.assertIn("| Показатель | Без изменений | Наш прогноз (низкий / базовый / высокий) | Почему | Запись |", p4)
        self.assertIn(f"| Подключения, тыс. | 120 | 130 / 150 / 170 | приглашение в момент перебоя | {fr} |", p4)
        self.assertLess(p4.index("| Q1 |"), p4.index(f"| Q2 | итог пилота"), "quarters in order")
        self.assertIn(q2, p4)
        self.assertIn("### Ориентир", p4)
        for h in ("### Люди и сроки", "### Риски", "### Просьба к руководителю"):
            self.assertIn(h, p5)
        self.assertIn("| Q1 | запуск пилота | 2 разработчика | директор |", p5)
        self.assertIn("Главное в одном абзаце.", md.split("## 1. ")[0])
        self.assertEqual(md.count("(не написано)"), 5)
        self.assertNotIn("—", md)
        self.assertNotIn("–", md)
        m = json.loads(pathlib.Path(self.root, "projects/demo/exports/manifest.json").read_text(encoding="utf-8"))
        self.assertIn("document.md", m["files"])

    # 4. completeness flag + documenting stage
    def test_missing_parts_flag_only_once_a_section_exists(self):
        self.assertEqual(self.flags("DOC_PART_MISSING"), [], "old projects without sections are not flagged")
        self.rec("section", {"part": "1", "body": "Текст."})
        missing = sorted(f["message"].rsplit(" ", 1)[1] for f in self.flags("DOC_PART_MISSING"))
        self.assertEqual(missing, sorted(["summary", "2", "3", "4", "5", "comms"]))

    def test_documenting_stage_between_strategy_and_decision(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        ids = self.build_chain()
        self.record("strategy", "Объяснить подключение", COMMS_STRATEGY, ids["strategy"])
        self.record("experiment", "Тест мастера", EXPERIMENT, ids["guide"])
        c = self.ctx()
        self.assertEqual(c["stage"], "documenting")
        self.assertEqual(c["next"]["skill"], "skills/strat-document/SKILL.md")
        self.assertEqual(c["open"]["missing_sections"], list(E.DOC_PARTS))
        self.rec("section", {"part": "summary", "body": "Коротко."}, depends=ids["strategy"])
        self.assertEqual(self.ctx()["open"]["missing_sections"], ["1", "2", "3", "4", "5", "comms"])
        write_document(self, ids["strategy"])
        self.assertEqual(self.ctx()["stage"], "awaiting_decision")

    # 5. number without a reference
    def test_number_without_reference_is_flagged(self):
        body = ("Охват - 1200 семей.\n\nОхват - 1200 семей [%s].\n\n## Часть 2\n\n1. Пункт без чисел\n"
                "- Q1: решение\n\nСсылка на несуществующее [CAL-0123456789].") % self.c
        r = self.rec("section", {"part": "2", "body": body})
        fl = [f for f in self.flags("SECTION_NUMBER_WITHOUT_SOURCE") if f["id"] == r]
        self.assertEqual(len(fl), 1, fl)
        self.assertIn("1200 семей", fl[0]["message"])
        self.assertEqual([f["id"] for f in self.flags("SECTION_REF_UNKNOWN")], [r])
        self.assertTrue(self.validate()["passed"], "flags are for review, not gates")

    # 6. calc script must exist under work/
    def test_calc_script_missing_flag(self):
        r = self.rec("calc", FULL["calc"])
        self.assertEqual([f["id"] for f in self.flags("CALC_SCRIPT_MISSING")], [r])
        self.work_script()
        self.assertEqual(self.flags("CALC_SCRIPT_MISSING"), [])
        pathlib.Path(self.root, "projects/demo/outside.py").write_text("x", encoding="utf-8")
        r2 = self.rec("calc", dict(FULL["calc"], script="outside.py"))
        self.rec("calc", dict(FULL["calc"], script="work/../../demo/outside.py"))
        fl = self.flags("CALC_SCRIPT_MISSING")
        self.assertIn(r2, [f["id"] for f in fl])
        self.assertTrue(all("not under work/" in f["message"] for f in fl))

    # 9. demand allowlist
    def test_vendor_alone_no_longer_counts_as_demand(self):
        t = self.source("tariffs.html", TARIFFS, "vendor")
        o = self.observe(t, "990 руб. за 7 дней")
        self.claim("fact", "Есть спрос на eSIM для поездок.", supports=o)
        self.assertIn("DEMAND_FROM_WEAK_SOURCES", self.codes(self.validate()))
        stat = self.source("stats.html", REVIEWS, "statistics")
        o2 = self.observe(stat, "хочу eSIM в поездки", "statement")
        self.claim("fact", "Спрос на eSIM для поездок подтверждён статистикой.", supports=f"{o},{o2}")
        weak = [g for g in self.validate()["gates"] if g["code"] == "DEMAND_FROM_WEAK_SOURCES"]
        self.assertEqual(len(weak), 1, "a strong source among the supports passes")
        for kind in ("official", "interview", "news"):
            self.assertNotIn(kind, E.STRONG_FOR_DEMAND)


class Sources(Base):
    def ctx(self):
        return run(self.root, "context", "--project", "demo")[1]["data"]

    def status(self):
        return run(self.root, "status", "--project", "demo")[1]["data"]

    def allow_loop(self):
        self.assertEqual(self.w("allow", "--pattern", "127.0.0.1", "--covers", "host", "--reason", "тест")[0], 0)

    # 7. empty body is not a source
    def test_empty_or_near_empty_body_is_blocked(self):
        srv = Server()
        self.addCleanup(srv.close)
        srv.files["/empty"] = (b"", "text/html")
        srv.files["/tiny.html"] = ("<html><body><p>Загрузка...</p></body></html>".encode(), "text/html")
        self.allow_loop()
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        for path in ("/empty", "/tiny.html"):
            self.n += 1
            code, out = run(self.root, "fetch", "--project", "demo", "--session", "s", "--token", self.token,
                            "--run", "r1", "--step", str(self.n), "--url", srv.url(path), env=LOOP)
            self.assertEqual((out["error_code"], out["data"]["reason"], out["retryable"]),
                             ("BLOCKED", "empty_body", False), path)
        st = self.status()
        self.assertEqual(st["source_versions"], [])
        self.assertEqual({o["status"] for o in st["operations"] if o["tool"] == "fetch"}, {"failed"})
        c = self.ctx()
        self.assertEqual(c["stage"], "collecting", "a failed fetch does not hold the stage")
        self.assertEqual(len(c["open"]["failed_operations"]), 2)

    # 8. abandon
    def test_abandon_unblocks_the_stage(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        f = pathlib.Path(self.root, "t.html")
        f.write_text(TARIFFS, encoding="utf-8")
        self.n += 1
        args = ["import", "--project", "demo", "--session", "s", "--token", self.token, "--run", "r1",
                "--step", str(self.n), "--file", str(f)]
        self.assertEqual(run(self.root, *args, env={"STRAT_CRASH_AT": "after_pending"})[0], 137)
        c = self.ctx()
        self.assertEqual(c["stage"], "resume_operation")
        op = c["open"]["operations"][0]["op_id"]
        base = ["abandon", "--project", "demo", "--session", "s", "--token", self.token, "--op", op]
        self.assertEqual(run(self.root, *base, "--reason", " ")[1]["error_code"], "VALIDATION_FAILED")
        self.assertEqual(run(self.root, *base[:-1], "op-0", "--reason", "x")[1]["error_code"], "NOT_FOUND")
        code, out = run(self.root, *base[:6], str(int(self.token) + 1), *base[7:], "--reason", "x")
        self.assertEqual(out["error_code"], "FENCED")
        code, out = run(self.root, *base, "--reason", "сайт недоступен, человек сохранит страницу")
        self.assertEqual((code, out["data"]["status"]), (0, "failed"), out)
        self.assertEqual(run(self.root, *base, "--reason", "сайт недоступен, человек сохранит страницу")[1]["status"],
                         "skipped")
        c = self.ctx()
        self.assertEqual(c["stage"], "collecting")
        self.assertEqual(c["open"]["operations"], [])
        self.assertIn("abandoned: сайт недоступен", c["open"]["failed_operations"][0]["note"])
        st = self.status()
        self.assertIn(("failed", "ABANDONED"), [(a["outcome"], a["error_code"]) for a in st["attempts"]])
        code, out = run(self.root, *args)   # rerunning the same step still retries it
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ctx()["stage"], "researching")
        self.assertEqual(run(self.root, *base, "--reason", "x")[1]["error_code"], "CONFLICT", "succeeded stays")

    # 10. read
    def test_read_whole_capped_and_around_a_quote(self):
        sv = self.source("tariffs.html", TARIFFS, "vendor")
        d = run(self.root, "read", "--project", "demo", "--source-version", sv)[1]["data"]
        self.assertFalse(d["truncated"])
        self.assertIn("990 руб. за 7 дней", d["text"])
        self.assertTrue(d["text"].startswith("[#0 "), "segments carry idx and hint")
        d = run(self.root, "read", "--project", "demo", "--source-version", sv, "--chars", "40")[1]["data"]
        self.assertEqual((len(d["text"]), d["truncated"]), (40, True))
        d = run(self.root, "read", "--project", "demo", "--source-version", sv, "--around", "до 10 минут",
                "--chars", "60")[1]["data"]
        self.assertTrue(d["found"])
        self.assertIn("до 10 минут", d["text"])
        self.assertLessEqual(len(d["text"]), 60)
        self.assertTrue(d["truncated"])
        d = run(self.root, "read", "--project", "demo", "--source-version", sv, "--around", "ДО 10 МИНУТ")[1]["data"]
        self.assertTrue(d["found"], "case-insensitive fallback")
        d = run(self.root, "read", "--project", "demo", "--source-version", sv, "--around", "нет такого")[1]["data"]
        self.assertEqual((d["found"], d["text"]), (False, ""))
        out = run(self.root, "read", "--project", "demo", "--source-version", sv, "--chars", "0")[1]
        self.assertEqual(out["error_code"], "VALIDATION_FAILED")
        self.assertEqual(run(self.root, "read", "--project", "demo", "--source-version", "SV-x")[1]["error_code"],
                         "NOT_FOUND")


class Schema8(Base):
    def test_v7_project_is_migrated_forward(self):
        sv = self.source("tariffs.html", TARIFFS, "vendor")
        c = self.claim("hypothesis", "Гипотеза.")
        opt = self.record("option", "Оставить", {"role": "status_quo", "objection": "x", "reversal": "y"}, c)
        db = pathlib.Path(self.root, "projects/demo/state.sqlite")
        con = sqlite3.connect(db)   # rebuild the record table as schema 7 had it
        old = E.record_table_sql("record_old").replace(
            ",'section','calc','forecast_row','quarter','resource','risk','ask','target'", "")
        con.executescript(old + "INSERT INTO record_old SELECT * FROM record; DROP TABLE record; "
                                "ALTER TABLE record_old RENAME TO record; "
                                "UPDATE meta SET value='7' WHERE key='schema_version';")
        con.close()
        code, out = run(self.root, "doctor", "--project", "demo")
        self.assertEqual((code, out["data"]["schema_version"], out["data"]["integrity_check"]), (0, "8", "ok"), out)
        self.assertEqual(self.validate()["counts"]["records"], 1)
        self.record("section", "Часть 1", {"part": "1", "body": "Текст."}, opt)
        st = run(self.root, "status", "--project", "demo")[1]["data"]
        self.assertEqual(st["source_versions"][0]["id"], sv)


class Tls(unittest.TestCase):
    """11. SSL fallback, unit-level: no network."""

    class FakeCtx:
        def __init__(self):
            self.loaded = []

        def load_verify_locations(self, cafile=None):
            self.loaded.append(cafile)

    def ctx(self, env, ca_file):
        fake = self.FakeCtx()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(strat.ssl, "create_default_context", return_value=fake), \
                mock.patch.object(strat, "MACOS_CA_FILE", ca_file), \
                mock.patch.dict(sys.modules, {"certifi": None}):   # import certifi -> ImportError
            if "SSL_CERT_FILE" not in env:
                os.environ.pop("SSL_CERT_FILE", None)
            ctx, source = strat.ssl_context()
        return fake, ctx, source

    def test_falls_back_to_system_bundle_when_unset(self):
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            pem = f.name
        self.addCleanup(os.unlink, pem)
        fake, ctx, source = self.ctx({}, pem)
        self.assertIs(ctx, fake)
        self.assertEqual((source, fake.loaded), (pem, [pem]))

    def test_explicit_ssl_cert_file_wins_and_missing_bundle_is_default(self):
        fake, _, source = self.ctx({"SSL_CERT_FILE": "/some/ca.pem"}, "/etc/ssl/cert.pem")
        self.assertEqual((source, fake.loaded), ("SSL_CERT_FILE", []))
        fake, _, source = self.ctx({}, "/nonexistent/cert.pem")
        self.assertEqual((source, fake.loaded), ("system default", []))

    def test_real_context_keeps_verification(self):
        import ssl
        ctx, _ = strat.ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
