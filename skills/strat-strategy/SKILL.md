---
name: strat-strategy
description: Разработать согласованные продуктовую и коммуникационную стратегии и план проверки, подготовить рекомендацию и записать решение человека. Использовать на этапах strategy и awaiting_decision; между ними - итоговый документ (strat-document).
---
# Стратегия и рекомендация

Метод: [стратегия и план проверки](../../method/strategy.md).

1. Возможности продукта: каждая, на которую будут опираться обещания, - `--kind capability` с
   `"readiness": "current" | "planned"`. `current` - только с опорой на утверждение-факт, что
   возможность уже есть (`CAPABILITY_WITHOUT_EVIDENCE`). Без такого факта - `planned`.
2. Продуктовая стратегия: `--kind strategy`, `"type": "product"`, поля `audience`, `job`,
   `alternatives`, `direction`, `refusals`, `economics`. Опора - выбранный для рекомендации вариант
   и возможности.
3. Коммуникационная стратегия: `"type": "comms"`, поля `audience`, `situation`, `barrier`,
   `key_message`, `proof`, `channels`. Опора - **продуктовая стратегия** (`COMMS_NOT_LINKED`).
4. Сообщения: `--kind message`, опора - своя `capability`. Если она `planned`, то
   `"future_proposition": true`. Чужие заявления (`vendor_claim`) не подтверждают наше обещание.
5. План проверки: `--kind experiment`, поля `expected_change`, `baseline`, `period`, `side_effects`,
   `unit`, `assignment`, `itt`, `sample_size`, `recruitment`, `threshold`, `stop_rule`
   (как считать размер и темп пилота - [данные](../../method/data.md)). Размер пилота - со своим `calc`.
6. `python tools/strat.py validate --project <id>` - без gates. Затем `python tools/strat.py export --project <id>`.
   Если `context` показывает этап `documenting`, собери итоговый документ по навыку
   [strat-document](../strat-document/SKILL.md) и только потом переходи к шагу 7.
7. Рекомендация человеку: какой вариант и почему, на каких фактах держится, какие гипотезы не
   проверены, что заставит пересмотреть. Затем **спроси решение и жди**.
8. Решение записывай только с его словами:
   `--kind decision --fields '{"actor": "human", "answer": "<дословно>", "option": "<id варианта>", "decided_at": "<дата>"}' --depends <id варианта>`.
   Действующее решение одно: новое решение заменяет старое через `resolve --outcome supersede`.
   Обязательства из решения: `--kind commitment` с `owner`, `metric`, `due`, `review_if`.

Готово, когда записано решение человека или он попросил доработать.
