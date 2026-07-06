# Аудит: console-frontend backend changes (3d08705, 930d81a, bfa07f7)  (2026-07-01)
Скоуп: RISKS (-r) + IMPROVEMENTS (-i) · Режим: агентный (7 субагентов) + живой прогон · дифф-скоуп

## Покрытие
Проверено (14 частей): runtime.py `_typed_audit_fields`; runtime.py write-sites completed(~1693)/failed(~1755)/branch(~777)/enforcement(~1856); studio_api `_auto_layout` + `_graph_to_detail`; audit/models.py clamp-validator; approvals/service.py completed_at; ui.tsx StatusBadge/tones/labels; SEAM→evidence.py; SEAM→agent-runtime producers (runner.py/tools.py); SEAM ui-labels↔backend vocab; **живой прогон run→audit→studio + branch-redaction-with-secret repro**. Adversarial verify прогнан на risk-находках; критик-проход добрал concurrency / boundary-data / test-coverage / config классы.
Не покрыто: миграции БД (тема их не трогает); фронтовые компоненты кроме ui.tsx (вне диффа); econ/Regulus working-tree правки (не мои, вне скоупа).

## Живой прогон — подтверждено
- **Branch-redaction fix РАБОТАЕТ** (repro, confirmed): реальный secret в tool-args/outcome + memory-value фан-аут-ветки → сырой маппинг утекает secret в типизированные колонки; redact-then-map (bfa07f7) даёт `[REDACTED:secret/api]` везде.
- Типизированные поля наполняются (04 tool_calls=1, 05 memory [read,write]×2), auto-layout позиции разнесены (03: 0,0 / 340,0 / 340,180) — через реальный worker→audit API→studio.

## РИСКИ

### P1 [confirmed] — runtime.py:798-805 — branch RunHistoryEntry НЕ редактится (secrets-at-rest)
bfa07f7 отредактил branch `NodeAuditRecord` (стр.777) но не соседний `RunHistoryEntry` в том же цикле: `input_snapshot=dict(branch_output)`, `output_snapshot=dict(ds_output)` — сырые. Эти entry мержатся в `run.execution_history` (`_merge_fan_in_state`) и персистятся репозиторием ранов. Прямо переоткрывает класс утечки, который bfa07f7 закрывал, и расходится с (а) audit-записью на 20 строк выше, (б) не-параллельным `_record_history` (редактит). Not-an-API-leak (execution_history не в RunStatusResponse) — но secrets-at-rest в сторе.
→ **Фикс (дёшево):** `input_snapshot=self._redact_for_audit(dict(branch_output))`, `output_snapshot=self._redact_for_audit(dict(ds_output))` — переиспользовать уже посчитанный `redacted_branch_audit`.
    impact: разрешённые secret'ы фан-аут-ветки ложатся сырыми в run.execution_history; потеря данных n; задет: любой оператор/экспорт, читающий историю рана из БД.

### P1 [confirmed] — frontend/app/components/ui.tsx:88-136 — реальные статусы рендерятся серым (tone-пробелы)
3d08705 правил именно эти карты, но покрыл happy-path, не весь словарь. Без tone → серый zinc:
- **run**: `terminated_by_loop_guard` (терминальный FAIL → должен быть red, как sibling terminated_by_policy); `queued` (стартовое состояние рана: API отдаёт `queued`, а в картах прописан `pending` — amber-tone висит на значении, которое до бэйджа не доходит); `waiting_interrupt` (админ-прерывание → amber).
- **audit**: `error` (ПРОВАЛ, а рисуется нейтрально), `forbidden`, `unauthenticated`, `unavailable`, `cancelled` — все серые.
→ **Фикс:** добавить в STATUS_TONES/DOT_TONES: error/forbidden/terminated_by_loop_guard→red; queued/waiting_interrupt/unavailable/unauthenticated→amber; cancelled→zinc. Дать `queued` (не только `pending`). Ярлыки для waiting_interrupt/terminated_by_loop_guard/queued.
    impact: провальные/терминальные/очередь-состояния неотличимы от нейтральных; цвет-триаж бэйджа сломан для оператора.

### P1 [likely, ПРЕД-СУЩЕСТВУЮЩЕЕ] — runtime.py:777 → audit/repository.py — гонка audit-цепочки в фан-ауте
Branch-audit-write живёт в `branch_coro_factory`, который parallel-executor запускает через `create_task`+`gather`. `audit_repository.write` читает голову цепочки (для `previous_record_digest`) и вставляет — read-head→insert не атомарно. Конкурентные ветки читают одну голову → форк tamper-evident хэш-цепочки; sqlite `busy_timeout` (~5s) превращает громкий SQLITE_BUSY в тихий форк со stale `previous_record_digest`. Моими коммитами НЕ внесено (я лишь добавил поля в уже-конкурентную запись) — но bfa07f7 расширил запись здесь. `likely`, не воспроизведено.
→ **Фикс:** сериализовать audit-write на run (asyncio.Lock по run_id) или BEGIN IMMEDIATE на голову. **Сначала — repro** (2 ветки, spy-repo, проверить непрерывность digest-цепочки), потом чинить.
    impact: непрерывность/детерминизм tamper-evident audit-цепочки под параллелизмом; потеря данных n (запись есть), но верификация цепочки может падать/форкаться.

### P2 [verified] — service/auth.py:205-226 — denial-запись без completed_at ('Completed —' остаётся)
`_record_service_denial` пишет NodeAuditRecord со `started_at`, но без `completed_at`. 3d08705 ставил цель наполнить completed_at на всех терминальных записях — этот сайт пропущен, `forbidden`/`unauthenticated` в Audit-view всё ещё '—'.
→ **Фикс:** `completed_at=datetime.now(UTC)` (denial терминален; == started_at корректно, clamp держит инвариант).

### P2 [verified] — runtime.py:2077-2088 — `_typed_audit_fields` глушит исключения без лога
Оба цикла `except Exception: continue` без записи. В audit/governance-системе мальформленная/reduction-битая запись молча выпадает из типизированных queryable/evidence колонок, при том что данные лежат в execution_metadata.extra — оператор видит меньше tool-calls/memory, без следа.
→ **Фикс:** `logger.warning(...)` перед continue (audit_id + индекс/tool_ref + exc), continue оставить.

+3 minor (P2/P3): studio_api.py:196-207 — не-Mapping `metadata["studio"]`/`node_positions` уронит `.get()` (boundary-data, coerce defensively); studio partial `node_positions` → недо-позиционированные ноды липнут в (0,0) [confirmed, all-or-nothing guard]; memory-цикл `model_validate(extra=forbid)` строгий против coerce-tolerant tool-цикла (асимметрия).

## УЛУЧШЕНИЯ

### P2 [fits] — runtime.py:1704/1766/774 + audit/models.py:143 — completed_at == started_at, нулевая длительность
completed_at=now() (арг) вычисляется ДО started_at default_factory (позже, при конструировании) → completed_at<started_at всегда → clamp-валидатор всегда срабатывает → каждая запись показывает нулевую длительность, а started_at = момент конструирования, не реальный старт ноды. Реальный тайминг есть в OTEL span (не потеряно), но поля вводят в заблуждение.
→ Захватить реальный старт ноды до dispatch и передавать `started_at` на write-сайтах; тогда clamp станет настоящей страховкой, а не всегда-no-op.

### P2 [fits] — studio_api.py:173,185 — _auto_layout схлопывает целые классы графов в колонку x=0
`if graph.entry_step:` пропускает BFS целиком при falsy entry_step (все ноды depth 0); `depth.get(id,0)` кладёт всех недостижимых в depth 0. Граф без entry_step или с недостижимыми нодами → вертикальный стек в одной колонке (частично тот же оверлап, что фича лечит).
→ Сидить BFS с `graph.nodes[0]` при falsy entry_step; недостижимым — свои колонки.

### P2 [fits] — studio_api.py:202,207 — сделать fill-in per-node вместо all-or-nothing
`layout=_auto_layout(graph); pos = node_positions.get(id) or layout.get(id,{x:0,y:0})` — частичные карты перестанут ронять ноды в origin. Плюс расширить `test_stored_positions_win_over_auto_layout` (сейчас не ассертит b/c → фиксирует плохое поведение).

### P3 [moderate] — ui.tsx:96 vs 132 — approval_api: BLUE tone + label "Approval resolved"
Blue = тот же in-flight цвет что `running`, а label терминально-позитивный; при этом sibling `resolved`/`approved` — emerald. Два «resolved»-смысла в разных цветах.
→ Либо emerald под ярлык, либо нейтральный ярлык ('Approval decided') + нейтральный тон.

+2 minor (P3): добавить E2E-тест, что фан-аут-ветка с агентом наполняет типизированные tool_calls/memory (сейчас branch-применение непокрыто тестом); таргетный тест на clamp-валидатор (completed_at<started_at → клампит; None не трогает; равные не трогают).

## Применено (2026-07-06, 4 атомарных коммита)
- **8ebe3af** — P1 branch RunHistoryEntry редакция (переиспользует redacted снапшоты) + P2 логирование дропнутых typed-audit записей; +invariant-тест redact-then-map.
- **08b9d8d** — P2 completed_at на auth-denial записи (дозакрывает 3d08705).
- **552c324** — P2/P3 studio: per-node fallback (частичные позиции), seed BFS без entry_step, defensive coercion metadata; +2 теста.
- **247ecd6** — P1 ui.tsx tone/label для полного словаря статусов (error/forbidden/terminated_by_loop_guard→red; queued/waiting_interrupt/unavailable/unauthenticated→amber; cancelled→zinc).
Все 4 прошли pre-commit (ruff + полный сьют 1440). Версия pyproject НЕ бампалась — в рабочем дереве незакоммиченные чужие Regulus-правки pyproject; бамп бы их затянул (как и в 3 прошлых fix-коммитах сессии).

## Вторая волна (2026-07-06)
- **P1 гонка audit-цепочки** — CONFIRMED репро (8 конкурентных write для одного run → 5 genesis-записей, форк tamper-evident цепочки). Фикс: process-wide `_chain_lock` в `AuditRepository.write` сериализует read-head+insert; +concurrent-write regression-тест. (Фикс появился в рабочем дереве от параллельной сессии; подтверждён репро и закоммичен вместе с тестом.)
- **P2 нулевая длительность** — `started_at` теперь = момент dispatch ноды, протянут через `_drive` в completed/failed audit-write сайты, поэтому `completed_at - started_at` — реальная длительность; approval-resolution оставлен на default (человеческое ожидание ≠ длительность ноды); +тест длительности. branch-path длительность осталась ~0 (follow-up).

**Осталось отложено (minor):** memory-loop строгий validate; approval_api blue-tone vs resolved-label; E2E-тест typed-полей в фан-ауте; branch-path длительность.

## Гигиена истории (2026-07-06)
`Co-Authored-By: Claude` вычищен из всех 20 атрибутированных коммитов ветки через
scoped `git filter-branch --msg-filter` (ветка локальная, не запушена; деревья
идентичны — менялись только сообщения; параллельная работа застэшена и восстановлена).
0 упоминаний Claude в истории ветки. Впредь коммиты без Claude-атрибуции.

## TL;DR
1. **Топ-дефект (P1, confirmed, чинить первым):** branch `RunHistoryEntry` не редактится (runtime.py:798-805) — secret'ы фан-аут-веток ложатся сырыми в run.execution_history; дёшево, переиспользовать `redacted_branch_audit`.
2. **Топ-инконсистентность (P1, confirmed):** ui.tsx tone-пробелы — `error`/`terminated_by_loop_guard`/`queued`/`waiting_interrupt` рендерятся серым; 3d08705 покрыл happy-path, не весь словарь статусов.
3. **Топ-возможность (P2, fits):** логировать в `_typed_audit_fields` перед `continue` — иначе audit-система молча недо-отчитывается о tool/memory без следа. (+ P1-риск для последующего repro: гонка audit-цепочки в параллельных ветках.)
