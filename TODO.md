# TODO — proctor-a (план от 2026-04-16)

> Роль в экосистеме: **первый реальный Mode-2 потребитель Maestro**. В roadmap экосистемы задач не имеет, но работает как живой dogfooding-стенд.
> Стратегический контекст: `../_cowork_output/roadmap/ecosystem-roadmap.md`
> Актуальный статус: `../_cowork_output/status/2026-04-10-status.md`

## Текущее состояние
- 🔥 Phase 2 активна: EpisodicMemory, SchedulerTrigger (croniter), TelegramTrigger
- ✅ Миграция asyncio → anyio завершена
- ✅ Задачи TASK-00N идут через Maestro-спеки (`maestro: add spec for ...`) — первый реальный Mode-2 run
- ⚠️ Нет CI (нужен для собственной стабильности, не часть ecosystem roadmap)

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- **Dogfooding-обязанность**: если Maestro ломает задачу proctor-a, заводить issue в `../Maestro/` с конкретным воспроизведением (yaml + логи)

---

## Активные задачи (собственный Phase 2 — детали в коде/спеках)

Полный список задач Phase 2 — в `spec/tasks.md` этого проекта. Здесь только **кросс-проектные** пункты:

### Dogfooding Maestro

- [ ] **Собрать pain-points от Mode-2 run** (ongoing)
  - Вести `notes/maestro-feedback.md` с конкретными примерами где Maestro:
    - падает
    - дает непонятное сообщение об ошибке
    - требует ручного вмешательства
  - Эскалировать в `../Maestro/` как issues или в `_cowork_output/`
  - Мотивация: мы единственный реальный внешний потребитель — без нашего фидбэка Maestro не узнает о багах кроме собственного dogfooding

### После R-01 (Maestro rename `codex` → `codex_cli`)

- [ ] **Обновить spec-ы и конфиги** если в наших yaml/спеках фигурирует `agent_type: codex`
  - Grep: `grep -rn "codex" spec/ config/ *.yaml`
  - Синхронизация с Maestro — перед её релизом v0.1.0

### После R-03 (Maestro arbiter routing)

- [ ] **Опционально включить arbiter routing** для proctor-a задач
  - Сравнить качество: static routing vs arbiter routing на нашем pain-data
  - Это натуральный datapoint для R-07 (eval-driven routing validation)

---

## Ждём от других проектов

- **Maestro → R-09 (CI)**: уменьшит шанс, что обновление Maestro сломает наш Mode-2 run
- **Maestro → R-04 (ExecutorState)**: даст типизированный feedback loop между нами и Maestro через spec-runner
- **Maestro → R-01..R-03**: после R-03 мы сможем осмысленно тестировать arbiter routing

---

## НЕ делаем здесь

- ❌ Прямая интеграция с arbiter (маршрутизация приходит через Maestro)
- ❌ Прямая интеграция с ATP (пока нет потребности в eval)
- ❌ Ecosystem roadmap R-NN задачи (proctor-a там не упомянут)
