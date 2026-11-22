"""Build step 4 gate: the IDE-facing layer - source allowlist, resume context, brief-shaped gates,
and the instructions/skills staying in sync with the CLI (plan v1.3 §5, §11 C01/C02).

Run: python -m unittest discover -s tests -v   (from the agent folder; macOS, Linux, Windows)
"""
import json
import pathlib
import re
import sys
import tempfile
import unittest

from test_step2 import LOOP, Server, HTML_PAGE
from test_step3 import Base, TARIFFS, PRODUCT_STRATEGY, run

AGENT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / "tools"))
import strat  # noqa: E402
import strat_context  # noqa: E402

COMMS_STRATEGY = {"type": "comms", "audience": "путешественники перед поездкой",
                  "situation": "покупают связь за 1-3 дня до вылета", "barrier": "не понимают, как подключить eSIM",
                  "key_message": "подключение за пару шагов", "proof": "мастер подключения (планируется)",
                  "channels": "push в приложении - напоминание, сайт - инструкция"}
EXPERIMENT = {"expected_change": "доля завершённых подключений +10 п.п.", "baseline": "текущий поток без мастера",
              "period": "4 недели", "side_effects": "рост обращений в поддержку",
              "unit": "клиент", "assignment": "случайно 50/50 по номеру клиента", "itt": "да, по назначению",
              "sample_size": "unknown: зависит от трафика", "recruitment": "все, кто открыл покупку eSIM",
              "threshold": "+5 п.п. к доле подключений", "stop_rule": "4 недели или рост обращений > 20%"}
FRAME = {"role": "frame", "goal": "решить, развивать ли eSIM-пакет для путешественников",
         "recipient": "руководитель продукта", "constraints": "только открытые данные; 2 недели",
         "data": "тарифные страницы, отзывы"}


def write_document(t, depends):
    """Schema 8: the final document's parts (summary, 1-5, comms), so the stage can move past `documenting`."""
    for part in ("summary", "1", "2", "3", "4", "5", "comms"):
        t.record("section", f"Часть {part}", {"part": part, "body": f"Текст части {part} без чисел."}, depends)


class Allowlist(Base):
    def setUp(self):
        super().setUp()
        self.srv = Server()
        self.addCleanup(self.srv.close)
        self.srv.files["/page.html"] = (HTML_PAGE.encode(), "text/html; charset=utf-8")

    def allow(self, pattern, reason="руководитель разрешил", *extra, covers="subdomains"):
        args = (["--pattern", pattern] + (["--reason", reason] if reason is not None else [])
                + (["--covers", covers] if covers else []) + list(extra))
        return self.w("allow", *args)

    def fetch(self, path):
        self.n += 1
        return run(self.root, "fetch", "--project", "demo", "--session", "s", "--token", self.token,
                   "--run", "r1", "--step", str(self.n), "--url", self.srv.url(path), env=LOOP)

    def test_empty_list_refuses_before_any_request_or_record(self):
        code, out = self.fetch("/page.html")
        self.assertEqual((out["error_code"], out["data"]["reason"]), ("BLOCKED", "not_allowed"))
        self.assertEqual(self.srv.hits, {}, "nothing was sent")
        st = run(self.root, "status", "--project", "demo")[1]["data"]
        self.assertEqual(st["operations"], [], "a refused fetch leaves no operation behind")

    def test_allowed_host_fetches(self):
        self.assertEqual(self.allow("127.0.0.1")[0], 0)
        code, out = self.fetch("/page.html")
        self.assertEqual(code, 0, out)

    def test_redirect_to_unlisted_host_is_refused(self):
        # 10.0.0.1 is private too, but the allowlist is checked first, before any DNS lookup
        self.allow("127.0.0.1")
        code, out = self.fetch("/redirect-private")
        self.assertEqual((out["error_code"], out["data"]["reason"]), ("BLOCKED", "not_allowed"))

    def test_revoke_stops_new_fetches_and_keeps_history(self):
        self.allow("127.0.0.1")
        self.assertEqual(self.fetch("/page.html")[0], 0)
        code, out = self.allow("127.0.0.1", None, "--revoke", covers=None)
        self.assertEqual(code, 0, out)
        code, out = self.fetch("/page.html")
        self.assertEqual(out["data"]["reason"], "not_allowed")
        st = run(self.root, "status", "--project", "demo")[1]["data"]
        self.assertEqual(len(st["source_versions"]), 1, "the earlier version stays")

    def test_reason_required_and_bad_patterns_refused(self):
        self.assertEqual(self.allow("example.ru", None)[1]["error_code"], "VALIDATION_FAILED")
        for bad in ("localhost", ".ru", "a b.ru", "http://example.ru", "example.ru/path"):
            self.assertEqual(self.allow(bad)[1]["error_code"], "VALIDATION_FAILED", bad)

    def test_same_step_is_skipped(self):
        self.allow("example.ru")
        self.n -= 1
        code, out = self.allow("example.ru")
        self.assertEqual(out["status"], "skipped")

    def test_host_matching(self):
        ok = strat.host_allowed
        self.assertTrue(ok("www.Example.RU.", ["example.ru"]))
        self.assertTrue(ok("example.ru", ["example.ru"]))
        self.assertFalse(ok("badexample.ru", ["example.ru"]))
        self.assertFalse(ok("example.ru.evil.com", ["example.ru"]))
        self.assertTrue(ok("www.пример.рф", [strat.norm_host("пример.рф")]), "Cyrillic domains match in IDNA form")
        self.assertFalse(ok("127.0.0.2", ["127.0.0.1"]))


class Context(Base):
    def ctx(self):
        code, out = run(self.root, "context", "--project", "demo")
        self.assertEqual(code, 0, out)
        return out["data"]

    def test_new_project_starts_with_framing(self):
        c = self.ctx()
        self.assertEqual(c["stage"], "framing")
        self.assertEqual(c["next"]["skill"], "skills/strat-frame/SKILL.md")
        self.assertEqual(c["next"]["next_step"], {"r1": 1})

    def test_frame_needs_its_fields(self):
        self.record("question", "Развивать ли eSIM?", {"role": "frame", "goal": "x"}, "")
        self.assertIn("FRAME_FIELD_MISSING", self.codes(self.validate()))
        self.assertEqual(self.ctx()["stage"], "collecting")

    def test_unfinished_operation_comes_first_then_clears(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        f = pathlib.Path(self.root, "t.html")
        f.write_text(TARIFFS, encoding="utf-8")
        args = ["import", "--project", "demo", "--session", "s", "--token", self.token, "--run", "r1", "--step", "50",
                "--file", str(f)]
        code, _ = run(self.root, *args, env={"STRAT_CRASH_AT": "after_pending"})
        self.assertEqual(code, 137)
        c = self.ctx()
        self.assertEqual(c["stage"], "resume_operation")
        self.assertEqual([(o["run_id"], o["step_id"]) for o in c["open"]["operations"]], [("r1", "50")])
        self.assertEqual(c["next"]["next_step"]["r1"], 51)
        self.assertEqual(run(self.root, *args)[0], 0)   # the same command, same step, finishes it
        self.assertEqual(self.ctx()["stage"], "researching")

    def test_stages_follow_the_work(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        ids = self.build_chain()
        self.assertEqual(self.ctx()["stage"], "strategy", "product strategy alone is not enough")
        comms = self.record("strategy", "Объяснить подключение", COMMS_STRATEGY, ids["strategy"])
        self.record("experiment", "Тест мастера", EXPERIMENT, ids["guide"])
        v = self.validate()
        self.assertTrue(v["passed"], v["gates"])
        self.assertEqual(self.ctx()["stage"], "documenting", "the document comes before the decision")
        write_document(self, ids["strategy"])
        self.assertEqual(self.ctx()["stage"], "awaiting_decision")
        self.record("decision", "Делаем мастер", {"actor": "human", "answer": "Да, делаем мастер, срок 4 недели",
                                                     "option": ids["guide"]}, ids["guide"])
        c = self.ctx()
        self.assertEqual(c["stage"], "decided")
        self.assertEqual(c["decisions"][0]["answer"], "Да, делаем мастер, срок 4 недели")
        new = self.source("tariffs-v2.html", TARIFFS.replace("990 руб.", "1190 руб."), "vendor")
        self.w("revalidate", "--old", ids["t"], "--new", new)
        c = self.ctx()
        self.assertEqual(c["stage"], "review_required")
        self.assertIn(ids["strategy"], {x["id"] for x in c["open"]["under_review"]})
        self.assertIn(comms, {x["id"] for x in c["open"]["under_review"]}, "comms rests on the product strategy")
        # the revisit path: new evidence -> new fact supersedes the old one -> the rest re-checked and reaffirmed
        o_new = self.observe(new, "1190 руб. за 7 дней", "measurement")
        c_new = self.claim("fact", "Пакет «Путешествие» стоит 1190 руб. за 7 дней.", supports=o_new)
        self.assertEqual(self.w("resolve", "--ids", ids["c_price"], "--outcome", "supersede", "--by", c_new,
                                "--reason", "цена выросла")[0], 0)
        old_decision = c["decisions"][0]["id"]
        rest = [x["id"] for x in c["open"]["under_review"] if x["id"] not in (ids["c_price"], old_decision)]
        code, out = self.w("resolve", "--ids", ",".join(rest), "--outcome", "reaffirm",
                           "--reason", "мастер не зависит от цены пакета")
        self.assertEqual(code, 0, out)
        new_decision = self.record("decision", "Делаем мастер (подтверждено)",
                                   {"actor": "human", "answer": "Подтверждаю при новой цене",
                                    "option": ids["guide"]}, f"{ids['guide']},{c_new}")
        self.w("resolve", "--ids", old_decision, "--outcome", "supersede", "--by", new_decision,
               "--reason", "подтверждено на новом основании")
        c = self.ctx()
        self.assertEqual(c["stage"], "decided", c["open"])
        v = self.validate()
        self.assertTrue(v["passed"], v["gates"])

    def test_resolve_needs_reason_and_a_real_replacement(self):
        ids = self.build_chain()
        self.assertEqual(self.w("resolve", "--ids", ids["c_price"], "--outcome", "reaffirm", "--reason", " ")[1]
                         ["error_code"], "VALIDATION_FAILED")
        self.assertEqual(self.w("resolve", "--ids", ids["c_price"], "--outcome", "supersede", "--reason", "x")[1]
                         ["error_code"], "VALIDATION_FAILED")
        self.assertEqual(self.w("resolve", "--ids", ids["o_price"], "--outcome", "withdraw", "--reason", "x")[1]
                         ["error_code"], "NOT_FOUND", "observations keep their history")

    def test_context_is_deterministic_across_chats(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        self.build_chain()
        self.assertEqual(self.ctx(), self.ctx(), "two chats reading the same DB get the same next step")

    def test_export_includes_context_and_stays_current(self):
        self.record("question", "Развивать ли eSIM?", FRAME, "")
        code, out = run(self.root, "export", "--project", "demo")
        self.assertIn("context.md", out["data"]["files"])
        md = pathlib.Path(self.root, "projects/demo/exports/context.md").read_text(encoding="utf-8")
        self.assertIn("Этап: **collecting**", md)
        self.assertIn("решить, развивать ли eSIM-пакет", md)
        doc = run(self.root, "doctor", "--project", "demo")[1]["data"]
        self.assertEqual(doc["exports"]["state"], "current")


class BriefGates(Base):
    def setUp(self):
        super().setUp()
        self.c = self.claim("hypothesis", "Гипотеза.")
        self.opt = self.record("option", "Вариант", {"role": "change", "objection": "x", "reversal": "y"}, self.c)

    def test_strategy_needs_type_and_fields(self):
        self.record("strategy", "Без типа", {}, self.opt)
        self.record("strategy", "Неполная", {"type": "product", "audience": "a"}, self.opt)
        codes = self.codes(self.validate())
        self.assertIn("STRATEGY_TYPE_MISSING", codes)
        self.assertIn("STRATEGY_FIELD_MISSING", codes)

    def test_comms_must_rest_on_product_strategy(self):
        self.record("strategy", "Коммуникации сами по себе", COMMS_STRATEGY, self.opt)
        self.assertIn("COMMS_NOT_LINKED", self.codes(self.validate()))
        prod = self.record("strategy", "Продукт", PRODUCT_STRATEGY, self.opt)
        self.record("strategy", "Коммуникации от продукта", COMMS_STRATEGY, prod)
        bad = [g for g in self.validate()["gates"] if g["code"] == "COMMS_NOT_LINKED"]
        self.assertEqual(len(bad), 1, "only the unlinked one fails")

    def test_experiment_needs_test_plan_fields(self):
        self.record("experiment", "Тест", {"expected_change": "x"}, self.opt)
        self.assertIn("EXPERIMENT_FIELD_MISSING", self.codes(self.validate()))

    def test_decision_is_the_humans_and_chooses_an_option(self):
        self.record("decision", "Агент решил сам", {"actor": "agent", "answer": "рекомендую", "option": self.opt},
                    self.opt)
        self.record("decision", "Решение без варианта", {"actor": "human", "answer": "да"}, self.c)
        codes = self.codes(self.validate())
        self.assertIn("DECISION_WITHOUT_HUMAN", codes)
        self.assertIn("DECISION_WITHOUT_OPTION", codes)


class R035Gates(Base):
    """Council #3 R035 (Codex review of step 3): checks that were direct-only or self-declared."""

    def test_fact_resting_on_synthetic_through_a_hypothesis(self):
        syn = self.claim("synthetic", "Модельный респондент хочет eSIM.")
        hyp = self.claim("hypothesis", "Путешественники хотят eSIM.", depends=syn)
        self.claim("fact", "Путешественники хотят eSIM.", depends=hyp)
        self.assertIn("FACT_ON_SYNTHETIC", self.codes(self.validate()))

    def test_fact_on_an_observation_from_a_synthetic_source(self):
        sv = self.source("panel.html", TARIFFS, "synthetic")
        o = self.observe(sv, "990 руб. за 7 дней", "measurement")
        self.claim("fact", "Пакет стоит 990 руб.", supports=o)
        self.assertIn("FACT_ON_SYNTHETIC", self.codes(self.validate()))

    def test_contradicting_synthetic_is_not_a_basis(self):
        sv = self.source("tariffs.html", TARIFFS, "vendor")
        o = self.observe(sv, "990 руб. за 7 дней", "measurement")
        syn = self.claim("synthetic", "Модельный респондент считает, что пакет дороже.")
        self.claim("fact", "Пакет стоит 990 руб.", supports=o, contradicts=syn)
        self.assertNotIn("FACT_ON_SYNTHETIC", self.codes(self.validate()))

    def test_current_capability_needs_a_fact(self):
        hyp = self.claim("hypothesis", "Мастер, наверное, уже есть.")
        self.record("capability", "Мастер подключения", {"readiness": "current"}, hyp)
        self.assertIn("CAPABILITY_WITHOUT_EVIDENCE", self.codes(self.validate()))
        sv = self.source("app.html", TARIFFS, "official")
        fact = self.claim("fact", "Подключение eSIM занимает до 10 минут.", supports=self.observe(sv, "до 10 минут"))
        self.record("capability", "Подключение eSIM", {"readiness": "current"}, fact)
        bad = [g for g in self.validate()["gates"] if g["code"] == "CAPABILITY_WITHOUT_EVIDENCE"]
        self.assertEqual(len(bad), 1, "only the self-declared one fails")

    def test_validate_says_it_is_structural(self):
        v = self.validate()
        self.assertTrue(v["structural_passed"])
        self.assertIn("structural", v["note"])


class JsonFromFile(Base):
    def test_fields_and_scope_from_file(self):
        f = pathlib.Path(self.root, "frame.json")
        f.write_text(json.dumps(FRAME, ensure_ascii=False), encoding="utf-8-sig")   # BOM, as Windows Notepad writes
        rid = self.w("record", "--kind", "question", "--title", "Развивать ли eSIM?", "--fields", f"@{f}")[1]
        self.assertEqual(rid["status"], "ok", rid)
        self.assertNotIn("FRAME_FIELD_MISSING", self.codes(self.validate()))
        code, out = self.w("claim", "--type", "hypothesis", "--text", "x", "--scope", "@" + str(f) + ".missing")
        self.assertEqual(out["error_code"], "NOT_FOUND")
        code, out = self.w("record", "--kind", "question", "--title", "q", "--fields", "[1, 2]")
        self.assertEqual(out["error_code"], "VALIDATION_FAILED")


class InstructionsInSync(unittest.TestCase):
    """The permanent instructions must only name commands, skills and method files that exist."""

    def docs(self):
        files = [AGENT / "AGENTS.md"] + sorted((AGENT / "skills").rglob("SKILL.md"))
        return {f: f.read_text(encoding="utf-8") for f in files}

    def test_every_command_named_in_docs_exists(self):
        import argparse
        known = set()
        orig = argparse.ArgumentParser.parse_args
        try:   # collect the real subcommand names from strat's own parser
            def grab(self, *a, **k):
                for act in self._actions:
                    if isinstance(act, argparse._SubParsersAction):
                        known.update(act.choices)
                raise SystemExit(0)
            argparse.ArgumentParser.parse_args = grab
            try:
                strat.main(["status"])
            except SystemExit:
                pass
        finally:
            argparse.ArgumentParser.parse_args = orig
        self.assertIn("context", known)
        for f, text in self.docs().items():
            for cmd in re.findall(r"strat\.py\s+([a-z-]+)", text):
                self.assertIn(cmd, known, f"{f.name} names unknown command {cmd}")

    def test_every_stage_skill_exists_with_frontmatter(self):
        for stage, (skill, _) in strat_context.STAGES.items():
            f = AGENT / "skills" / skill / "SKILL.md"
            self.assertTrue(f.is_file(), f"{stage} -> {f}")
            head = f.read_text(encoding="utf-8").split("---")[1]
            self.assertIn(f"name: {skill}", head)
            self.assertIn("description:", head)

    def test_every_linked_file_exists(self):
        for f, text in self.docs().items():
            for rel in re.findall(r"\]\((?!https?:)([^)\s]+\.md)\)", text):
                self.assertTrue((f.parent / rel).resolve().is_file(), f"{f.relative_to(AGENT)} links missing {rel}")

    def test_instruction_hash_sees_an_edit(self):
        with tempfile.TemporaryDirectory() as d:
            old = strat_context.AGENT_DIR
            try:
                strat_context.AGENT_DIR = pathlib.Path(d)
                (pathlib.Path(d) / "skills" / "x").mkdir(parents=True)
                skill = pathlib.Path(d, "skills", "x", "SKILL.md")
                skill.write_text("a", encoding="utf-8")
                h1 = strat_context.instructions_hash()
                skill.write_text("b", encoding="utf-8")
                self.assertNotEqual(h1, strat_context.instructions_hash())
            finally:
                strat_context.AGENT_DIR = old


class R037History(Base):
    """Council #3 R037 (Codex review of step 4): history must never drive the resume, back a promise
    or be rewritten by resolve."""

    def ctx(self):
        return run(self.root, "context", "--project", "demo")[1]["data"]

    def decided(self):
        frame = self.record("question", "Развивать ли eSIM?", FRAME, "")
        ids = self.build_chain()
        ids["comms"] = self.record("strategy", "Объяснить подключение", COMMS_STRATEGY, ids["strategy"])
        self.record("experiment", "Тест мастера", EXPERIMENT, ids["guide"])
        write_document(self, ids["strategy"])
        ids["decision"] = self.record("decision", "Делаем мастер", {
            "actor": "human", "answer": "Да, делаем мастер", "option": ids["guide"]}, ids["guide"])
        self.assertEqual(self.ctx()["stage"], "decided")
        return dict(ids, frame=frame)

    def resolve(self, *args):
        return self.w("resolve", *args)[1]

    def test_withdrawing_the_only_decision_reopens_it(self):
        ids = self.decided()
        refused = self.resolve("--ids", ids["decision"], "--outcome", "withdraw", "--reason", "агент передумал")
        self.assertEqual(refused["error_code"], "VALIDATION_FAILED", "the agent can't withdraw the human's decision")
        out = self.resolve("--ids", ids["decision"], "--outcome", "withdraw", "--reason", "бюджет урезан",
                           "--answer", "Отменяем, бюджета нет")
        self.assertEqual(out["status"], "ok", out)
        c = self.ctx()
        self.assertEqual(c["stage"], "awaiting_decision")
        self.assertEqual(c["decisions"], [])
        old = next(h for h in c["history"] if h["id"] == ids["decision"])
        self.assertEqual((old["status"], old["answer"]), ("withdrawn", "Да, делаем мастер"), "original words kept")
        self.assertIn("Отменяем, бюджета нет", old["resolution"])

    def test_decision_is_replaced_only_by_a_new_human_decision(self):
        ids = self.decided()
        self.assertEqual(self.resolve("--ids", ids["decision"], "--outcome", "reaffirm", "--reason", "всё в силе")
                         ["error_code"], "VALIDATION_FAILED", "no reaffirming a decision by a reason")
        agent = self.record("decision", "Агент предлагает", {"actor": "agent", "answer": "оставить", "option":
                                                              ids["status_quo"]}, ids["status_quo"])
        self.assertEqual(self.resolve("--ids", ids["decision"], "--outcome", "supersede", "--by", agent,
                                      "--reason", "x")["error_code"], "VALIDATION_FAILED")
        self.resolve("--ids", agent, "--outcome", "withdraw", "--reason", "не решение человека",
                     "--answer", "Это не моё решение")
        new = self.record("decision", "Оставляем как есть", {"actor": "human", "answer": "Оставляем пакет как есть",
                                                             "option": ids["status_quo"]}, ids["status_quo"])
        self.assertIn("DECISION_CONFLICT", self.codes(self.validate()))
        self.assertEqual(self.ctx()["stage"], "fixing", "two current decisions are never silently ordered")
        self.assertEqual(self.resolve("--ids", ids["decision"], "--outcome", "supersede", "--by", new,
                                      "--reason", "человек передумал")["status"], "ok")
        c = self.ctx()
        self.assertEqual(c["stage"], "decided")
        self.assertEqual([(d["id"], d["option"]) for d in c["decisions"]], [(new, ids["status_quo"])])
        self.assertEqual(next(h for h in c["history"] if h["id"] == ids["decision"])["by"], new)

    def test_retiring_the_only_comms_strategy_goes_back_to_strategy(self):
        ids = self.decided()
        self.resolve("--ids", ids["comms"], "--outcome", "withdraw", "--reason", "каналы не подходят")
        self.assertEqual(self.ctx()["stage"], "strategy")

    def test_retiring_an_option_counts_and_retired_choice_is_flagged(self):
        ids = self.decided()
        self.resolve("--ids", ids["guide"], "--outcome", "withdraw", "--reason", "мастер не нужен")
        codes = self.codes(self.validate())
        self.assertIn("TOO_FEW_OPTIONS", codes)
        self.assertIn("DECISION_OPTION_RETIRED", codes)
        self.assertEqual(self.ctx()["stage"], "options")

    def test_superseded_frame_gives_the_new_constraints(self):
        ids = self.decided()
        new = self.record("question", "Развивать ли eSIM? (срок 4 недели)",
                          dict(FRAME, constraints="только открытые данные; 4 недели"), "")
        self.assertIn("FRAME_CONFLICT", self.codes(self.validate()))
        c = self.ctx()
        self.assertIsNone(c["frame"], "two current frames: no silent pick")
        self.assertEqual(c["stage"], "framing")
        self.resolve("--ids", ids["frame"], "--outcome", "supersede", "--by", new, "--reason", "срок продлён")
        c = self.ctx()
        self.assertEqual((c["frame"]["id"], c["frame"]["constraints"]), (new, "только открытые данные; 4 недели"))
        self.assertEqual(c["stage"], "decided")

    def test_decision_must_name_its_option(self):
        ids = self.build_chain()
        self.record("decision", "Без явного выбора", {"actor": "human", "answer": "да"},
                    f"{ids['guide']},{ids['closure']}")
        self.assertIn("DECISION_WITHOUT_OPTION", self.codes(self.validate()))

    def test_capability_on_a_withdrawn_fact_is_not_evidence(self):
        sv = self.source("app.html", TARIFFS, "official")
        fact = self.claim("fact", "Подключение eSIM занимает до 10 минут.", supports=self.observe(sv, "до 10 минут"))
        self.record("capability", "Подключение eSIM", {"readiness": "current"}, fact)
        self.assertNotIn("CAPABILITY_WITHOUT_EVIDENCE", self.codes(self.validate()))
        self.resolve("--ids", fact, "--outcome", "withdraw", "--reason", "страница устарела")
        v = self.validate()
        self.assertIn("CAPABILITY_WITHOUT_EVIDENCE", self.codes(v))
        self.assertIn("RESTS_ON_RETIRED", {f["code"] for f in v["flags"]})

    def test_resolve_replacement_must_match_and_history_is_final(self):
        ids = self.build_chain()
        bad = [("--ids", ids["c_price"], "--by", ids["guide"]),              # claim replaced by an option
               ("--ids", ids["strategy"], "--by", ids["cap"]),               # strategy by a capability
               ("--ids", ids["status_quo"], "--by", ids["status_quo"])]      # itself
        comms = self.record("strategy", "Коммуникации", COMMS_STRATEGY, ids["strategy"])
        bad.append(("--ids", ids["strategy"], "--by", comms))                # product strategy by a comms one
        for args in bad:
            out = self.resolve(*args, "--outcome", "supersede", "--reason", "x")
            self.assertEqual(out["error_code"], "VALIDATION_FAILED", args)
        self.assertEqual(self.resolve("--ids", ids["c_price"], "--outcome", "reaffirm", "--by", ids["c_pain"],
                                      "--reason", "x")["error_code"], "VALIDATION_FAILED", "--by only with supersede")
        a = ids["c_price"]
        b = self.claim("fact", "Пакет стоит 990 руб.", supports=ids["o_price"])
        self.assertEqual(self.resolve("--ids", a, "--outcome", "supersede", "--by", b, "--reason", "уточнено")
                         ["status"], "ok")
        self.assertEqual(self.resolve("--ids", b, "--outcome", "supersede", "--by", a, "--reason", "назад")
                         ["error_code"], "VALIDATION_FAILED", "a retired item can't come back as a replacement")
        self.assertEqual(self.resolve("--ids", a, "--outcome", "reaffirm", "--reason", "снова")
                         ["error_code"], "VALIDATION_FAILED", "history is final")

    def test_new_source_version_does_not_revive_history(self):
        ids = self.build_chain()
        b = self.claim("fact", "Пакет стоит 990 руб.", supports=ids["o_price"])
        self.resolve("--ids", ids["c_price"], "--outcome", "supersede", "--by", b, "--reason", "уточнено")
        new = self.source("tariffs-v2.html", TARIFFS.replace("990 руб.", "1190 руб."), "vendor")
        out = self.w("revalidate", "--old", ids["t"], "--new", new)[1]["data"]
        self.assertNotIn(ids["c_price"], out["affected"])
        self.assertIn(b, out["affected"])
        st = {h["id"]: h["status"] for h in self.ctx()["history"]}
        self.assertEqual(st[ids["c_price"]], "superseded")


class ExactHostGrant(Base):
    def allow(self, *args):
        return self.w("allow", "--reason", "руководитель разрешил", *args)

    def test_scope_is_required_and_shown(self):
        self.assertEqual(self.allow("--pattern", "example.ru")[1]["error_code"], "VALIDATION_FAILED")
        out = self.allow("--pattern", "forum.example.ru", "--covers", "host")[1]
        self.assertEqual(out["data"]["covers"], "только этот сайт")
        self.assertEqual(out["data"]["allowed"], ["=forum.example.ru"])
        c = run(self.root, "context", "--project", "demo")[1]["data"]
        self.assertEqual(c["allowed_sources"][0]["covers"], "только этот сайт")

    def test_exact_host_does_not_cover_subdomains(self):
        ok = strat.host_allowed
        self.assertTrue(ok("Forum.Example.RU.", ["=forum.example.ru"]))
        self.assertFalse(ok("user1.forum.example.ru", ["=forum.example.ru"]))
        self.assertTrue(ok("user1.forum.example.ru", ["forum.example.ru"]))


if __name__ == "__main__":
    unittest.main()
