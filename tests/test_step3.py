"""Build step 3 gate: observations, claims, records, edges, validators, impact, revalidation.

A small fictional-but-realistic chain: an operator tariff page + an app-review page ->
observations -> claims -> 3 options (incl. status quo and closure) -> capability -> strategy ->
message. Then each gate is broken on purpose, and a new source version tests carry-forward.
Run: python -m unittest discover -s tests -v
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

STRAT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "strat.py"

TARIFFS = """<!doctype html><html><body><h2>eSIM для поездок</h2>
<p>Пакет «Путешествие» стоит 990 руб. за 7 дней и включает 5 ГБ в 40 странах.</p>
<p>Подключение eSIM занимает до 10 минут в приложении.</p>
<table><tr><th>Пакет</th><th>Цена за 7 дней, руб.</th></tr><tr><td>Путешествие</td><td>990</td></tr></table>
<p>Условия обслуживания приведены на сайте оператора. Подробнее в разделе помощи.</p></body></html>"""
REVIEWS = """<!doctype html><html><body><h2>Отзывы о приложении</h2>
<p>Отзыв 1: хочу eSIM в поездки, но не понимаю, как подключить.</p>
<p>Отзыв 2: роуминг слишком дорогой, купил местную сим-карту.</p>
<p>Всего отзывов на странице: 2. Отзывы не модерируются и не являются выборкой.</p>
<p>{pad}</p></body></html>""".replace("{pad}", "Служебный текст страницы. " * 10)


def run(root, *args, env=None):
    e = {**os.environ, **(env or {})}
    p = subprocess.run([sys.executable, str(STRAT), "--root", root, *args], capture_output=True, env=e)
    text = p.stdout.decode("utf-8")
    return p.returncode, (json.loads(text) if text.strip() else None)


SCOPE = json.dumps({"audience": "клиенты оператора, выезжающие за рубеж", "region": "Россия",
                    "period": "ноябрь 2026", "product": "eSIM-пакет «Путешествие»",
                    "metric": "цена пакета, руб. за 7 дней"}, ensure_ascii=False)


PRODUCT_STRATEGY = {"type": "product", "audience": "путешественники, которые не подключают eSIM",
                    "job": "быть на связи сразу после прилёта", "alternatives": "местная сим-карта, роуминг",
                    "direction": "мастер подключения", "refusals": "не снижаем цену пакета",
                    "economics": "unknown: нет данных о доле отказов"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        run(self.root, "init", "--project", "demo")
        self.token = str(run(self.root, "lease", "--project", "demo", "--session", "s")[1]["data"]["fencing_token"])
        self.n = 0

    def w(self, cmd, *args):
        """A write command with a fresh (run, step)."""
        self.n += 1
        code, out = run(self.root, cmd, "--project", "demo", "--session", "s", "--token", self.token,
                        "--run", "r1", "--step", str(self.n), *args)
        return code, out

    def source(self, name, html, kind):
        f = pathlib.Path(self.root, name)
        f.write_text(html, encoding="utf-8")
        code, out = self.w("import", "--file", str(f))
        self.assertEqual(code, 0, out)
        sv = out["data"]["source_version_id"]
        run(self.root, "extract", "--project", "demo", "--session", "s", "--token", self.token, "--source-version", sv)
        code, out = run(self.root, "classify", "--project", "demo", "--session", "s", "--token", self.token,
                        "--source-version", sv, "--source-kind", kind)
        self.assertEqual(code, 0, out)
        return sv

    def observe(self, sv, exact, kind="quote", **kw):
        args = ["--source-version", sv, "--exact", exact, "--kind", kind]
        for k, v in kw.items():
            args += [f"--{k}", v]
        code, out = self.w("observe", *args)
        self.assertEqual(code, 0, out)
        return out["data"]["observation_id"]

    def claim(self, type_, text, scope=SCOPE, **kw):
        args = ["--type", type_, "--text", text, "--scope", scope]
        for k, v in kw.items():
            args += [f"--{k}", v]
        code, out = self.w("claim", *args)
        self.assertEqual(code, 0, out)
        return out["data"]["claim_id"]

    def record(self, kind, title, fields, depends):
        code, out = self.w("record", "--kind", kind, "--title", title,
                           "--fields", json.dumps(fields, ensure_ascii=False), "--depends", depends)
        self.assertEqual(code, 0, out)
        return out["data"]["record_id"]

    def validate(self):
        return run(self.root, "validate", "--project", "demo")[1]["data"]

    def codes(self, v):
        return sorted({g["code"] for g in v["gates"]})

    def build_chain(self):
        """A valid chain; returns ids for the tests to break or change."""
        t = self.source("tariffs.html", TARIFFS, "vendor")
        r = self.source("reviews.html", REVIEWS, "review")
        o_price = self.observe(t, "990 руб. за 7 дней", "measurement", value="990", unit="руб./7 дней",
                               period="ноябрь 2026", population="n/a: тариф")
        o_setup = self.observe(t, "до 10 минут")
        o_rev = self.observe(r, "не понимаю, как подключить", "statement", population="2 отзыва, не выборка")
        c_price = self.claim("fact", "Пакет «Путешествие» стоит 990 руб. за 7 дней.", supports=o_price)
        c_setup = self.claim("vendor_claim", "Оператор заявляет подключение eSIM до 10 минут.", supports=o_setup)
        c_pain = self.claim("hypothesis", "Часть путешественников не подключает eSIM, потому что не понимает процесс.",
                            supports=o_rev)
        base = f"{c_price},{c_pain}"
        opts = {
            "status_quo": self.record("option", "Оставить пакет как есть", {
                "role": "status_quo", "objection": "Барьер подключения сохраняется",
                "reversal": "доля незавершённых подключений > 30% к 01.03.2027"}, base),
            "guide": self.record("option", "Пошаговый мастер подключения в приложении", {
                "role": "change", "objection": "Затраты на разработку без доказанного спроса",
                "reversal": "тест мастера не снижает отказы к 01.02.2027"}, base),
            "closure": self.record("option", "Закрыть направление eSIM-пакетов", {
                "role": "closure", "objection": "Клиенты уйдут к местным сим-картам",
                "reversal": "регулятор запрещает продажу пакетов"}, base),
        }
        cap = self.record("capability", "Мастер подключения eSIM", {"readiness": "planned"}, c_pain)
        strat_ = self.record("strategy", "Снять барьер подключения", PRODUCT_STRATEGY, f"{opts['guide']},{cap}")
        msg = self.record("message", "«Подключите eSIM за пару шагов - мастер подскажет»",
                          {"future_proposition": True}, f"{cap},{strat_}")
        return dict(t=t, r=r, o_price=o_price, o_setup=o_setup, o_rev=o_rev, c_price=c_price, c_setup=c_setup,
                    c_pain=c_pain, cap=cap, strategy=strat_, msg=msg, **opts)


class Chain(Base):
    def test_valid_chain_passes_all_gates(self):
        self.build_chain()
        v = self.validate()
        self.assertTrue(v["passed"], v["gates"])
        self.assertEqual(v["counts"]["claims"], 3)

    def test_writes_are_idempotent_per_step(self):
        t = self.source("tariffs.html", TARIFFS, "vendor")
        args = ["--project", "demo", "--session", "s", "--token", self.token, "--run", "r9", "--step", "1",
                "--source-version", t, "--exact", "до 10 минут", "--kind", "quote"]
        first = run(self.root, "observe", *args)[1]
        again = run(self.root, "observe", *args)[1]
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(again["data"]["observation_id"], first["data"]["observation_id"])
        other = args[:-4] + ["--exact", "990 руб.", "--kind", "quote"]
        self.assertEqual(run(self.root, "observe", *other)[1]["error_code"], "CONFLICT")

    def test_ambiguous_quote_cannot_become_an_observation(self):
        t = self.source("tariffs.html", TARIFFS, "vendor")
        code, out = self.w("observe", "--source-version", t, "--exact", "990", "--kind", "quote")
        self.assertEqual(out["error_code"], "UNRESOLVED")
        self.assertEqual(out["data"]["reason"], "ambiguous")

    def test_claim_cannot_rest_on_unknown_id(self):
        code, out = self.w("claim", "--type", "fact", "--text", "x", "--scope", SCOPE, "--supports", "O-nope")
        self.assertEqual(out["error_code"], "NOT_FOUND")


class Gates(Base):
    def test_fact_without_evidence(self):
        self.claim("fact", "Рынок eSIM в России вырос вдвое.")
        self.assertIn("FACT_WITHOUT_EVIDENCE", self.codes(self.validate()))

    def test_scope_missing(self):
        self.claim("hypothesis", "Что-то про аудиторию.", scope=json.dumps({"region": "Россия"}))
        self.assertIn("SCOPE_MISSING", self.codes(self.validate()))

    def test_unknown_scope_without_reason_is_a_flag_not_a_gate(self):
        s = json.dumps({"audience": "unknown", "region": "Россия", "period": "2026", "product": "eSIM",
                        "metric": "n/a: качественное наблюдение"})
        self.claim("hypothesis", "Гипотеза.", scope=s)
        v = self.validate()
        self.assertTrue(v["passed"], v["gates"])
        self.assertIn("SCOPE_NO_REASON", {f["code"] for f in v["flags"]})

    def test_demand_fact_from_reviews_only_is_rejected(self):
        r = self.source("reviews.html", REVIEWS, "review")
        o = self.observe(r, "хочу eSIM в поездки", "statement")
        self.claim("fact", "Есть спрос на eSIM для поездок.", supports=o)
        self.assertIn("DEMAND_FROM_WEAK_SOURCES", self.codes(self.validate()))

    def test_same_demand_as_hypothesis_is_fine(self):
        r = self.source("reviews.html", REVIEWS, "review")
        o = self.observe(r, "хочу eSIM в поездки", "statement")
        self.claim("hypothesis", "Возможно, есть спрос на eSIM для поездок.", supports=o)
        self.assertTrue(self.validate()["passed"])

    def test_unclassified_source_counts_as_weak_for_demand(self):
        f = pathlib.Path(self.root, "x.html")
        f.write_text(REVIEWS, encoding="utf-8")
        sv = self.w("import", "--file", str(f))[1]["data"]["source_version_id"]
        run(self.root, "extract", "--project", "demo", "--session", "s", "--token", self.token, "--source-version", sv)
        o = self.observe(sv, "хочу eSIM в поездки", "statement")
        self.claim("fact", "Клиенты готовы платить за eSIM.", supports=o)
        self.assertIn("DEMAND_FROM_WEAK_SOURCES", self.codes(self.validate()))

    def test_fact_resting_on_synthetic(self):
        syn = self.claim("synthetic", "Синтетический респондент: «я бы купил».")
        self.claim("fact", "Пакет стоит 990 руб.", depends=syn)
        self.assertIn("FACT_ON_SYNTHETIC", self.codes(self.validate()))

    def test_future_wording_in_fact_is_flagged(self):
        t = self.source("tariffs.html", TARIFFS, "vendor")
        o = self.observe(t, "990 руб. за 7 дней", "measurement")
        self.claim("fact", "Пакет будет стоить 990 руб.", supports=o)
        v = self.validate()
        self.assertTrue(v["passed"])
        self.assertIn("FACT_LOOKS_LIKE_FORECAST", {f["code"] for f in v["flags"]})

    def test_record_without_basis(self):
        self.record("strategy", "Стратегия ни на чём", {}, "")
        self.assertIn("RECORD_WITHOUT_BASIS", self.codes(self.validate()))

    def test_too_few_options_and_missing_roles(self):
        c = self.claim("hypothesis", "Гипотеза.")
        self.record("option", "Только один вариант", {"role": "change", "objection": "x", "reversal": "y"}, c)
        codes = self.codes(self.validate())
        self.assertIn("TOO_FEW_OPTIONS", codes)
        self.assertIn("OPTION_ROLE_MISSING", codes)

    def test_option_needs_objection_and_reversal(self):
        c = self.claim("hypothesis", "Гипотеза.")
        for role in ("status_quo", "change", "closure"):
            self.record("option", role, {"role": role}, c)
        self.assertIn("OPTION_FIELD_MISSING", self.codes(self.validate()))

    def test_message_backed_only_by_vendor_claim(self):
        t = self.source("tariffs.html", TARIFFS, "vendor")
        o = self.observe(t, "до 10 минут")
        vc = self.claim("vendor_claim", "Оператор заявляет подключение за 10 минут.", supports=o)
        self.record("message", "«Подключение за 10 минут»", {}, vc)
        v = self.validate()
        self.assertIn("MESSAGE_WITHOUT_CAPABILITY", self.codes(v))
        self.assertIn("vendor", [g for g in v["gates"] if g["code"] == "MESSAGE_WITHOUT_CAPABILITY"][0]["message"])

    def test_planned_capability_promised_as_current(self):
        c = self.claim("hypothesis", "Гипотеза.")
        cap = self.record("capability", "Мастер подключения", {"readiness": "planned"}, c)
        self.record("message", "«Мастер уже в приложении»", {}, cap)
        self.assertIn("PROMISE_NOT_READY", self.codes(self.validate()))

    def test_cycle_is_rejected(self):
        # The CLI can't create a cycle (a record can only depend on ids that already exist), so a cycle
        # means the DB was edited by hand - validate must still catch it.
        c = self.claim("hypothesis", "A")
        r1 = self.record("question", "Q", {}, c)
        r2 = self.record("hypothesis", "H", {}, r1)
        import sqlite3
        db = sqlite3.connect(pathlib.Path(self.root, "projects/demo/state.sqlite"))
        db.execute("INSERT INTO edge VALUES (?,?,'depends')", (r1, r2))   # r1 -> r2 -> r1
        db.commit()
        db.close()
        self.assertIn("CYCLE", self.codes(self.validate()))


class ImpactAndRevision(Base):
    def test_impact_walks_the_whole_chain_and_nothing_else(self):
        ids = self.build_chain()
        out = run(self.root, "impact", "--project", "demo", "--ids", ids["o_rev"])[1]["data"]
        hit = {i for group in out["affected"].values() for i in group}
        for k in ("c_pain", "status_quo", "guide", "closure", "cap", "strategy", "msg"):
            self.assertIn(ids[k], hit, k)
        for k in ("c_price", "c_setup", "o_price"):
            self.assertNotIn(ids[k], hit, f"{k} doesn't rest on the review")

    def test_footer_only_edit_carries_everything_forward(self):
        ids = self.build_chain()
        new = self.source("tariffs-v2.html", TARIFFS.replace("Подробнее в разделе помощи.", "Обновлено 08.11."), "vendor")
        code, out = self.w("revalidate", "--old", ids["t"], "--new", new)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out["data"]["carried_forward"]), 2)
        self.assertEqual(out["data"]["review_required"], [])
        v = self.validate()
        self.assertTrue(v["passed"], v["gates"])

    def test_same_number_new_denominator_requires_review(self):
        ids = self.build_chain()
        changed = TARIFFS.replace("990 руб. за 7 дней и включает 5 ГБ", "990 руб. за 7 дней и включает 3 ГБ") \
                         .replace("<h2>eSIM для поездок</h2>", "<h2>eSIM для поездок (новые условия)</h2>")
        new = self.source("tariffs-v3.html", changed, "vendor")
        out = self.w("revalidate", "--old", ids["t"], "--new", new)[1]["data"]
        flagged = {x["observation"] for x in out["review_required"]}
        self.assertIn(ids["o_price"], flagged, "same quote, but the context (heading) changed")
        self.assertIn(ids["c_price"], out["affected"])
        v = self.validate()
        self.assertIn("FACT_BASIS_UNDER_REVIEW", self.codes(v))
        self.assertIn("UNDER_REVIEW", {f["code"] for f in v["flags"]})

    def test_unchanged_cell_under_changed_header_requires_review(self):
        # R016/R019 case: the number and its cell are byte-identical, only the column header
        # (the denominator) changed. Only the context hash can catch this.
        t = self.source("tariffs.html", TARIFFS, "vendor")
        o = self.observe(t, "990", "measurement", hint="table0!r1c1")
        c = self.claim("fact", "Пакет стоит 990 руб. за 7 дней.", supports=o)
        new = self.source("tariffs-v5.html", TARIFFS.replace("Цена за 7 дней, руб.", "Цена за 30 дней, руб."), "vendor")
        out = self.w("revalidate", "--old", t, "--new", new)[1]["data"]
        self.assertEqual(out["review_required"], [{"observation": o, "reason": "context_changed"}])
        self.assertIn(c, out["affected"])

    def test_quote_gone_from_new_version_requires_review(self):
        ids = self.build_chain()
        new = self.source("tariffs-v4.html", TARIFFS.replace("до 10 минут", "до 30 минут"), "vendor")
        out = self.w("revalidate", "--old", ids["t"], "--new", new)[1]["data"]
        reasons = {x["observation"]: x["reason"] for x in out["review_required"]}
        self.assertEqual(reasons.get(ids["o_setup"]), "not_found")
        self.assertIn(ids["c_setup"], out["affected"])
        self.assertNotIn(ids["c_pain"], out["affected"], "the review-based hypothesis is untouched")


class ClaimsExport(Base):
    def test_claims_md_cites_every_source(self):
        ids = self.build_chain()
        code, out = run(self.root, "export", "--project", "demo")
        self.assertIn("claims.md", out["data"]["files"])
        md = pathlib.Path(self.root, "projects/demo/exports/claims.md").read_text(encoding="utf-8")
        self.assertIn(ids["c_price"], md)
        self.assertIn("«990 руб. за 7 дней»", md)
        self.assertIn("tariffs.html", md)
        self.assertIn("тип источника: vendor", md)
        dump = pathlib.Path(self.root, "projects/demo/exports/dump/edge.jsonl").read_text(encoding="utf-8")
        self.assertIn(ids["o_price"], dump)


if __name__ == "__main__":
    unittest.main()
