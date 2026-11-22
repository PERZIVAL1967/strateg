"""exports/report.md - one readable document for a product manager (build step 5).

Built inside the export's read transaction, so it shows the same DB revision as the other export files.
Sections 1-13 show current work only (superseded and withdrawn items are history - see context.md);
an empty section says why it is empty instead of disappearing. Field names come from the gates in
strat_evidence (FRAME_KEYS, STRATEGY_KEYS, EXPERIMENT_KEYS) and the option/commitment skills.
"""
import json

import strat_evidence as E
import strat_ledger

TODO = "Ещё не сделано: "
FRAME_LABELS = {"goal": "Цель", "recipient": "Кому результат", "constraints": "Ограничения", "data": "Данные"}
OPTION_LABELS = {"mechanism": "Механизм", "tradeoff": "Чем жертвуем", "resources": "Ресурсы",
                 "objection": "Сильное возражение", "reversal": "Условие пересмотра"}
ROLES = {"status_quo": "оставить как есть", "closure": "закрыть направление", "change": "изменение"}
STRATEGY_LABELS = {
    "product": {"audience": "Аудитория", "job": "Задача клиента", "alternatives": "Альтернативы",
                "direction": "Направление", "refusals": "От чего отказываемся", "economics": "Экономика"},
    "comms": {"audience": "Аудитория", "situation": "Ситуация", "barrier": "Барьер",
              "key_message": "Ключевое сообщение", "proof": "Доказательство", "channels": "Каналы"},
}
EXPERIMENT_LABELS = {"expected_change": "Ожидаемое изменение", "baseline": "База сравнения", "period": "Срок",
                     "side_effects": "Побочные эффекты", "unit": "Единица отбора", "assignment": "Распределение",
                     "itt": "Анализ по назначению (ITT)", "sample_size": "Размер выборки",
                     "recruitment": "Набор участников", "threshold": "Порог успеха", "stop_rule": "Правило остановки"}
COMMITMENT_LABELS = {"owner": "ответственный", "metric": "показатель", "due": "срок", "review_if": "пересмотр, если"}
READINESS = {"current": "есть сейчас", "planned": "планируется"}
CLAIM_STATUS = {"proposed": "предложено", "checked": "проверено", "contested": "оспорено",
                "review_required": "на пересмотре"}
COST_KIND = {"model": "модель", "search": "поиск", "tool": "инструменты", "human": "работа человека"}
COST_UNIT = {"usd": "долл. США", "rub": "руб.", "tokens": "токенов", "minutes": "мин."}
COST_BASIS = {"ide": "по данным среды", "measured": "измерено", "estimate": "оценка"}


def one_line(v):
    if v is None:
        return "не указано"
    if not isinstance(v, str):
        v = json.dumps(v, ensure_ascii=False)
    v = " ".join(v.split())
    return v or "не указано"


def build(con, revision):
    """Return report.md bytes. Long dashes are replaced by hyphens (the formal-writing rule); exact quotes
    stay in claims.md."""
    srcs = [dict(r) for r in con.execute(
        "SELECT sv.id, sv.retrieved_at, sv.source_kind, s.uri FROM source_version sv "
        "JOIN source s ON s.id = sv.source_id ORDER BY sv.retrieved_at, sv.id")]
    num = {s["id"]: i for i, s in enumerate(srcs, 1)}
    obs = {r["id"]: dict(r) for r in con.execute("SELECT id, source_version_id, exact, status FROM observation")}
    claims = [dict(r) for r in con.execute("SELECT * FROM claim WHERE status NOT IN (?,?) ORDER BY created_at, id",
                                           E.RETIRED)]
    records = [dict(r) for r in con.execute("SELECT * FROM record WHERE status NOT IN (?,?) ORDER BY created_at, id",
                                            E.RETIRED)]
    for r in records:
        r["fields"] = json.loads(r["fields"])
    by_id = {r["id"]: r for r in records}
    all_claims = {r["id"]: dict(r) for r in con.execute("SELECT id, text, status FROM claim")}
    edges = {}
    for e in con.execute("SELECT from_id, to_id, kind FROM edge ORDER BY to_id"):
        edges.setdefault(e["from_id"], []).append(dict(e))
    kind = {}
    for r in records:
        kind.setdefault(r["kind"], []).append(r)

    def refs(cid, edge_kind="supports"):
        ns = sorted({num[obs[e["to_id"]]["source_version_id"]] for e in edges.get(cid, [])
                     if e["kind"] == edge_kind and e["to_id"] in obs})
        return "".join(f" [{n}]" for n in ns)

    def mark(status):
        return f" ({CLAIM_STATUS[status]})" if status in ("review_required", "contested") else ""

    def claim_lines(types, empty, label=None):
        rows = [c for c in claims if c["type"] in types]
        if not rows:
            return [TODO + empty]
        out = []
        for c in rows:
            r = refs(c["id"])
            tail = r if r else " (без прямой ссылки на источник)"
            prefix = f"{label(c)}: " if label else ""
            out.append(f"- {prefix}{one_line(c['text'])}{tail}{mark(c['status'])} - `{c['id']}`")
        return out

    def fields_block(f, labels):
        return [f"  - {lab}: {one_line(f.get(k))}" for k, lab in labels.items()]

    L = ["# Отчёт по проекту", "",
         f"Ревизия базы: {revision}. Файл создан автоматически командой `strat export` - не редактировать. "
         "Показана только действующая работа; заменённое и отозванное - в `context.md`, точные цитаты - "
         "в `claims.md`. Ссылки вида [1] ведут к разделу «Источники».", ""]

    # 1. Задача
    L += ["## 1. Задача", ""]
    frames = [r for r in kind.get("question", []) if r["fields"].get("role") == "frame"]
    if not frames:
        L.append(TODO + "постановка задачи не записана.")
    else:
        if len(frames) > 1:
            L.append(f"Внимание: действующих постановок {len(frames)}, нужно оставить одну.")
        for f in frames:
            L += [f"**{one_line(f['title'])}**", ""] + [f"- {lab}: {one_line(f['fields'].get(k))}"
                                                       for k, lab in FRAME_LABELS.items()] + [""]
    if not frames:
        L.append("")

    # 2-4. Claims by type
    L += ["## 2. Что известно", ""] + claim_lines(("fact",), "факты из источников не записаны.") + [""]
    L += ["## 3. Что говорят о себе компании", ""]
    L += claim_lines(("vendor_claim",), "заявления компаний о себе не записаны.") + [""]
    L += ["## 4. Гипотезы и прогнозы", ""]
    labels = {"hypothesis": "Гипотеза", "forecast": "Прогноз",
              "synthetic": "Модельные ответы (не доказывают спрос и распространённость)"}
    L += claim_lines(("hypothesis", "forecast", "synthetic"), "гипотезы и прогнозы не записаны.",
                     label=lambda c: labels[c["type"]])
    L += [f"- Гипотеза (запись): {one_line(r['title'])} - `{r['id']}`" for r in kind.get("hypothesis", [])]
    L.append("")

    # 5. Unknowns
    L += ["## 5. Чего мы не знаем", ""]
    unknown = [f"- Открытый вопрос: {one_line(r['title'])} - `{r['id']}`" for r in kind.get("question", [])
               if r["fields"].get("role") != "frame" and r["fields"].get("status") == "open"]
    for c in claims:
        for k, v in sorted(json.loads(c["scope"]).items()):
            if str(v).strip().lower().startswith("unknown"):
                unknown.append(f"- {one_line(c['text'])}: {k} - {one_line(v)} - `{c['id']}`")
    for r in records:
        for k, v in sorted(r["fields"].items()):
            if isinstance(v, str) and v.strip().lower().startswith("unknown"):
                unknown.append(f"- {one_line(r['title'])}: {k} - {one_line(v)} - `{r['id']}`")
    L += (unknown or ["Открытых вопросов и пометок «unknown» нет."]) + [""]

    # 6. Contradictions
    L += ["## 6. Противоречия", ""]
    contra = []
    for c in claims:
        for e in edges.get(c["id"], []):
            if e["kind"] != "contradicts":
                continue
            t = e["to_id"]
            if t in obs:
                what = f"цитата «{one_line(obs[t]['exact'])}» [{num[obs[t]['source_version_id']]}]"
            elif t in all_claims:
                what = f"утверждение «{one_line(all_claims[t]['text'])}»"
            else:
                what = t
            contra.append(f"- «{one_line(c['text'])}» (`{c['id']}`) противоречит: {what} - `{t}`")
    L += (contra or ["Противоречащих наблюдений не записано."]) + [""]

    # 7. Options
    L += ["## 7. Варианты", ""]
    opts = kind.get("option", [])
    if not opts:
        L.append(TODO + "варианты не записаны.")
    for o in opts:
        role = o["fields"].get("role")
        L += [f"- **{one_line(o['title'])}** ({ROLES.get(role, one_line(role))}) - `{o['id']}`"]
        L += fields_block(o["fields"], OPTION_LABELS)
    L.append("")

    # 8-9. Strategies
    strategies = {t: [r for r in kind.get("strategy", []) if r["fields"].get("type") == t] for t in STRATEGY_LABELS}
    for n, t, title in ((8, "product", "Продуктовая стратегия"), (9, "comms", "Коммуникационная стратегия")):
        L += [f"## {n}. {title}", ""]
        if not strategies[t]:
            L.append(TODO + f"{title.lower()} не записана.")
        for s in strategies[t]:
            L += [f"- **{one_line(s['title'])}** - `{s['id']}`"] + fields_block(s["fields"], STRATEGY_LABELS[t])
        if t == "comms":
            L += ["", "Сообщения:", ""]
            msgs = kind.get("message", [])
            if not msgs:
                L.append(TODO + "сообщения не записаны.")
            for m in msgs:
                fut = " (обещание на будущее)" if m["fields"].get("future_proposition") else ""
                L.append(f"- {one_line(m['title'])}{fut} - `{m['id']}`")
                caps = [by_id[e["to_id"]] for e in edges.get(m["id"], [])
                        if e["kind"] == "depends" and by_id.get(e["to_id"], {}).get("kind") == "capability"]
                for cap in caps:
                    rd = cap["fields"].get("readiness")
                    L.append(f"  - опирается на возможность: {one_line(cap['title'])} "
                             f"(готовность: {READINESS.get(rd, one_line(rd))}) - `{cap['id']}`")
                if not caps:
                    L.append("  - опора на нашу возможность не указана")
        L.append("")

    # 10. Experiments
    L += ["## 10. План проверки", ""]
    exps = kind.get("experiment", [])
    if not exps:
        L.append(TODO + "эксперименты не записаны.")
    for x in exps:
        L += [f"- **{one_line(x['title'])}** - `{x['id']}`"] + fields_block(x["fields"], EXPERIMENT_LABELS)
    L.append("")

    # 11. Decision
    L += ["## 11. Рекомендация и решение", ""]
    decisions = kind.get("decision", [])
    if not decisions:
        L.append("Решение не принято. Рекомендацию агент представляет человеку в чате; решает только человек.")
    for d in decisions:
        f = d["fields"]
        opt = by_id.get(f.get("option"))
        who = "человека" if f.get("actor") == "human" else f"(actor: {one_line(f.get('actor'))})"
        L += [f"- **{one_line(d['title'])}** - `{d['id']}`",
              f"  - выбранный вариант: {one_line(opt['title']) if opt else one_line(f.get('option'))}",
              f"  - слова {who}: «{one_line(f.get('answer'))}»",
              f"  - дата: {one_line(f.get('decided_at'))}"]
    L.append("")

    # 12. Commitments and review
    L += ["## 12. Обязательства и условия пересмотра", ""]
    comm = kind.get("commitment", [])
    if not comm:
        L.append(TODO + "обязательства не записаны.")
    for c in comm:
        L.append(f"- **{one_line(c['title'])}** - `{c['id']}`: "
                 + "; ".join(f"{lab} {one_line(c['fields'].get(k))}" for k, lab in COMMITMENT_LABELS.items()))
    review = [f"- {one_line(c['text'])} - `{c['id']}`" for c in claims if c["status"] == "review_required"]
    review += [f"- {one_line(r['title'])} ({r['kind']}) - `{r['id']}`" for r in records
               if r["status"] == "review_required"]
    L += ["", "На пересмотре (основание изменилось):", ""] + (review or ["- нет"]) + [""]

    # 13. Cost
    L += ["## 13. Стоимость и время", ""]
    cost = strat_ledger.summary(con)
    if not cost["model_cost_logged"]:
        L.append("Расход модели не записан.")
    L += [f"- {COST_KIND[t['kind']]}: {t['amount']:g} {COST_UNIT[t['unit']]} ({COST_BASIS[t['basis']]}, "
          f"записей: {t['entries']})" for t in cost["totals"]]
    if cost["unknown"]:
        L.append("- не записано (неизвестно, не ноль): " + ", ".join(COST_KIND[k] for k in cost["unknown"]))
    L += [f"- неудачные или повторённые попытки операций: {cost['failed_or_repeated_attempts']}", ""]

    # 14. Sources
    L += ["## 14. Источники", ""]
    if not srcs:
        L.append(TODO + "источники не получены.")
    L += [f"{i}. {s['uri']} - получено {s['retrieved_at']}; тип источника: {s['source_kind']}; версия `{s['id']}`"
          for i, s in enumerate(srcs, 1)]
    text = "\n".join(L) + "\n"
    return text.replace("—", "-").replace("–", "-").encode()


# ---------------------------------------------------------------- exports/document.md (schema 8)

DOC_HEADINGS = (
    ("summary", "Главное"),
    ("1", "1. Клиент и его проблема"),
    ("2", "2. Предложение и путь к клиенту"),
    ("3", "3. Польза для клиента и прибыль оператора"),
    ("4", "4. Стратегический выбор и план по кварталам"),
    ("5", "5. Ресурсы, риски и решение руководителя"),
    ("comms", "Коммуникационная стратегия"),
)
NOT_WRITTEN = "(не написано)"


def cell(v):
    """One Markdown table cell: a number range as low / base / high, no line breaks, no bare pipes."""
    if isinstance(v, dict) and {"low", "base", "high"} <= set(v):
        rest = {k: x for k, x in v.items() if k not in ("low", "base", "high")}
        v = f"{cell(v['low'])} / {cell(v['base'])} / {cell(v['high'])}" + (f" ({one_line(rest)})" if rest else "")
    elif isinstance(v, float) and v.is_integer():
        v = int(v)
    return one_line(v if isinstance(v, str) else (str(v) if isinstance(v, (int, float)) else v)).replace("|", "/")


def table(head, rows):
    if not rows:
        return []
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + \
        ["| " + " | ".join(cell(x) for x in r) + " |" for r in rows] + [""]


def outputs_text(o):
    if isinstance(o, dict):
        return "; ".join(f"{k}: {cell(v)}" for k, v in o.items())
    return cell(o)


def document(con, revision):
    """Return document.md bytes: the one document for the executive - the opening summary, parts 1-5 and the
    communication strategy, with forecast rows, quarters, the target, people, risks and asks as tables, and
    each calculation under the part whose section rests on it. report.md is its appendix."""
    records = [dict(r) for r in con.execute("SELECT * FROM record WHERE status NOT IN (?,?) ORDER BY created_at, id",
                                            E.RETIRED)]
    for r in records:
        r["fields"] = json.loads(r["fields"])
    kind = {}
    for r in records:
        kind.setdefault(r["kind"], []).append(r)
    deps = {}
    for e in con.execute("SELECT from_id, to_id FROM edge WHERE kind='depends' ORDER BY to_id"):
        deps.setdefault(e["from_id"], []).append(e["to_id"])
    sections = {}
    for r in kind.get("section", []):
        sections.setdefault(E.unique_key("section", r["fields"]), []).append(r)

    calcs = kind.get("calc", [])
    placed = {}   # calc id -> part: the first part (1-5, comms, summary) whose section depends on it
    for part in ("1", "2", "3", "4", "5", "comms", "summary"):
        for sec in sections.get(part, []):
            for d in deps.get(sec["id"], []):
                placed.setdefault(d, part)

    def calc_lines(items):
        out = []
        for c in items:
            f = c["fields"]
            out.append(f"- **{one_line(c['title'])}** [{c['id']}]: скрипт `{one_line(f.get('script'))}`; "
                       f"результат: {outputs_text(f.get('outputs'))}")
            out.append(f"  - допущения: {one_line(f.get('assumptions'))}")
        return out + ([""] if out else [])

    frames = [r for r in kind.get("question", []) if r["fields"].get("role") == "frame"]
    title = one_line(frames[0]["title"]) if len(frames) == 1 else "Продуктовая и коммуникационная стратегия"
    L = [f"# {title}", "",
         f"Ревизия базы: {revision}. Документ собран командой `strat export` из записей проекта - не редактировать. "
         "В квадратных скобках - номер записи (расчёт, утверждение, прогноз) в приложении `report.md`.", ""]
    for part, heading in DOC_HEADINGS:
        L += [f"## {heading}", ""]
        secs = sections.get(part, [])
        if not secs:
            L += [NOT_WRITTEN, ""]
        for sec in secs:
            body = sec["fields"].get("body")
            L += [(body if isinstance(body, str) else one_line(body)).strip() or NOT_WRITTEN, ""]
        mine = [c for c in calcs if placed.get(c["id"]) == part]
        if part == "3":
            mine += [c for c in calcs if c["id"] not in placed]
        if mine:
            L += ["### Расчёты", ""] + calc_lines(mine)
        if part == "4":
            rows = kind.get("forecast_row", [])
            if rows:
                L += ["### Прогноз по строкам", ""]
                L += table(("Показатель", "Без изменений", "Наш прогноз (низкий / базовый / высокий)", "Почему",
                            "Запись"),
                           [(r["fields"].get("metric"), r["fields"].get("no_change"), r["fields"].get("ours"),
                             r["fields"].get("driver"), r["id"]) for r in rows])
            qs = sorted(kind.get("quarter", []), key=lambda r: (str(r["fields"].get("q")), r["created_at"]))
            if qs:
                L += ["### План по кварталам", ""]
                L += table(("Квартал", "Что станет известно", "Какое решение это позволит", "Запись"),
                           [(f"Q{r['fields'].get('q')}", r["fields"].get("known"), r["fields"].get("decision"),
                             r["id"]) for r in qs])
            ts = kind.get("target", [])
            if ts:
                L += ["### Ориентир", ""]
                L += table(("Горизонт", "Показатель", "Числитель", "База", "Условие", "Запись"),
                           [tuple(r["fields"].get(k) for k in E.DOC_KEYS["target"]) + (r["id"],) for r in ts])
        if part == "5":
            res = kind.get("resource", [])
            if res:
                L += ["### Люди и сроки", ""]
                L += table(("Роль", "Человеко-месяцы", "Период", "Ответственный", "Открытый вопрос", "Запись"),
                           [tuple(r["fields"].get(k) for k in E.DOC_KEYS["resource"]) + (r["id"],) for r in res])
            risks = kind.get("risk", [])
            if risks:
                L += ["### Риски", ""]
                L += table(("Признак", "Последствие", "Ответственный", "Действие", "Запись"),
                           [tuple(r["fields"].get(k) for k in E.DOC_KEYS["risk"]) + (r["id"],) for r in risks])
            asks = sorted(kind.get("ask", []), key=lambda r: str(r["fields"].get("when")).upper())
            if asks:
                L += ["### Просьба к руководителю", ""]
                L += table(("Когда", "Решение", "Ресурсы", "Ответственный", "Запись"),
                           [tuple(r["fields"].get(k) for k in E.DOC_KEYS["ask"]) + (r["id"],) for r in asks])
    L += ["---", "", "Приложение: доказательная база, источники, варианты и история - в `report.md`."]
    text = "\n".join(L) + "\n"
    return text.replace("—", "-").replace("–", "-").encode()
