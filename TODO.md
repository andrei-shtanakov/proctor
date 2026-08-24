# TODO — proctor (план от 2026-04-16, ревизия 2026-07-26)

> Роль в экосистеме: **первый реальный Mode-2 потребитель Maestro**. В roadmap экосистемы задач не имеет, но работает как живой dogfooding-стенд.
> Стратегический контекст: `../prograph-vault/authored/notes/ecosystem-roadmap.md`
> Актуальный статус: `../prograph-vault/authored/notes/status/2026-07-08-status.md`

## Текущее состояние
- ✅ Phase 2 завершена: triggers (terminal/telegram/scheduler/webhook), NATS-транспорт, EpisodicMemory, TaskRouter (admission: 4 инварианта + TTL-очередь). Все задачи `spec/tasks.md` закрыты
- ✅ Phase 3 (часть 1) — worker registry + remote dispatch: WorkerRegistry (liveness/fencing), capability scoring, WorkerNode (worker-role runtime), remote dispatch (rollback, loss policy, reaper); distribution loop покрыт как local-transport, так и multi-node NATS интеграционными тестами
- ✅ Phase 3 (часть 2) — docker worker: core-managed container fleet (DockerWorkerManager + ContainerRuntime docker/podman CLI wrapper), fresh-id fencing, poll-loop restart (backoff/jitter/stability-reset/ceiling), Dockerfile + base worker config, integration test за `docker` pytest-маркером
- ✅ Phase 3 (часть 3) — remote docker workers через `DOCKER_HOST=ssh://` (`ssh_host` на docker-fleet, per-op timeout с kill/reap, unreachable ceiling); bare-SSH backend отложен (rule-of-three)
- ✅ Миграция asyncio → anyio завершена
- ✅ Задачи TASK-00N идут через Maestro-спеки (`maestro: add spec for ...`) — первый реальный Mode-2 run
- ✅ CI есть (GitHub Actions: unit + integration-nats с Toxiproxy reconnect-тестами)
- ✅ Governance-гейт экосистемы принят (ADR-ECO-004 D5, PR #40): `governance / gate` — обязательный чек на `master`, плюс CODEOWNERS и ruleset (1 approving review, запрет force-push/удаления ветки)
- 🔜 Phase 3 (часть 4) — модуль `mcp/` (client/server/controller/registry/proxy): не начата, следующий крупный кусок

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- **Dogfooding-обязанность**: если Maestro ломает задачу proctor, заводить issue в `../maestro/` с конкретным воспроизведением (yaml + логи)
- Пункты размечаем инлайн-тегами `@owner:` / `@blocked_by:` / `@trigger:` / `@id:` по plan-fields v2. Для `@owner:` каноничны `github:<login>`, `github-team:<org>/<team>`, `repo:<manifest-key>` и `TBD`; bare handle/role — legacy. Теги опциональны: пусто = «неизвестно», выдумывать триггер там, где его нет, не надо
  - `@id:<node-id>` — канонический идентификатор пункта (ADR-ECO-005 PF-2B): строчная грамматика `[a-z0-9][a-z0-9._-]{0,63}`, из него строится URI `todo://proctor/<id>`. Переходно `@blocked_by` принимает и legacy `<repo>#<slug>`, и канонический `todo://<repo>/<id>`

---

## Активные задачи

Задачи уровня реализации живут в `docs/plans/` и спеках; здесь — только пункты уровня команды и кросс-проектные.

### Dogfooding Maestro

- [ ] **Собрать pain-points от Mode-2 run** (ongoing) @owner:github:andrei-shtanakov @id:mode-2-pain-points
  - Журнал заведён: `notes/maestro-feedback.md` (шаблон записи готов, **записей пока 0** — с 2026-07-17 новых Mode-2 прогонов не было)
  - Фиксировать, где Maestro: падает / даёт непонятную ошибку / требует ручного вмешательства
  - Эскалировать в `../maestro/` как issues или в `../_cowork_output/`
  - Мотивация: мы единственный реальный внешний потребитель — без нашего фидбэка Maestro не узнает о багах кроме собственного dogfooding

### Arbiter routing (после Maestro R-03b)

- [ ] **Опционально включить arbiter routing** для proctor задач @owner:github:andrei-shtanakov @blocked_by:todo://maestro/r-03b @trigger:"Maestro закрыла R-03b (Mode-2 workstream-level routing)" @id:arbiter-routing-opt-in
  - Maestro R-03 (MCP-клиент arbiter) уже shipped в v0.2.0, но для нас релевантен именно Mode-2 — это R-03b, который у Maestro открыт и сам гейтится «≥1 неделя стабильного Mode-1 dogfood после v0.2.0»
  - Сравнить качество: static routing vs arbiter routing на нашем pain-data
  - Это натуральный datapoint для R-07 (eval-driven routing validation)

### Phase 3 (часть 4) — `mcp/`

- [ ] **Учесть депрекейшены mcp SDK при проектировании `mcp/`** @owner:github:andrei-shtanakov @trigger:"старт работ над модулем mcp/" @id:mcp-sdk-deprecations
  - С `mcp` 1.28.0 (у нас 1.28.1, PR #39) задепрекейчены WebSocket-транспорт (`mcp.client.websocket` / `mcp.server.websocket`) и experimental tasks API (`ClientSession.experimental`, `experimental_task_handlers=`) — удаление в v2
  - Следствие: не строить транспорт на WebSocket и не опираться на tasks API; если в pytest включим `filterwarnings = ["error"]` — понадобится scoped ignore

- [ ] **Guard результата MCP-инструмента в дизайне `mcp/`** (slug: mcp-tool-result-guard) @owner:github:andrei-shtanakov @trigger:"начат модуль mcp/" @id:mcp-tool-result-guard
  - Запрос: issue #52 (from: ai-repos-research#proposal-v3-harvest)
  - Путь «результат инструмента → контекст агента» сейчас не проверяет никто (гейты стоят на маршрутизации вызовов); тул-результат — канал prompt-injection и утечки креденшелов. Закрыть при постройке mcp/proxy, не после
  - Две детерминированные проверки результата до отдачи в контекст: (1) индикаторы prompt-injection («ignore previous instructions», «new instructions:», скрытые HTML-комментарии-инструкции); (2) regex-детектор креденшелов (AWS `AKIA…`, GitHub `ghp_`/`github_pat_`, `sk-ant-…` и др.)
  - Образец: mcptoon `src/mcptoon/router.py` (`_POISONING_INDICATORS` + `_CREDENTIAL_PATTERNS`) — переносим как идею; при заимствовании текста паттернов проверить лицензию. Evidence: `labs/ai-repos-research/details/mcptoon.md`

---

## Ждём от других проектов

На 2026-07-26 весь список, который мы ждали в апреле, **приехал**:

- ✅ **Maestro R-09 (CI)** — GitHub Actions, зелёный прогон
- ✅ **Maestro R-04 (ExecutorState)** — типизированный feedback loop
- ✅ **Maestro R-01..R-03** — shipped в v0.2.0

Открыто остаётся одно: **Maestro R-03b** (Mode-2 workstream-level routing) — от него зависит наш пункт про arbiter routing выше.

---

## Сделано / снято

- [x] **Обновить spec-ы и конфиги после Maestro R-01** (`codex` → `codex_cli`) — снято 2026-07-26 как неприменимое: `grep -rn "codex" spec/ config/ *.yaml` даёт только упоминания в `spec/.executor-logs/` (имена файлов шаблонов ревью), ни одного `agent_type: codex` в наших спеках и конфигах нет. Maestro R-01 закрыт на их стороне (commit `8fd0b51`)

---

## НЕ делаем здесь

- ❌ Прямая интеграция с arbiter (маршрутизация приходит через Maestro)
- ❌ Прямая интеграция с ATP (пока нет потребности в eval)
- ❌ Ecosystem roadmap R-NN задачи (proctor там не упомянут)
