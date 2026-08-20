# AI Workflow — петля «Claude Code ↔ Codex»

Операционный регламент: как агенты ведут разработку GLT Portal **без
участия пользователя**, и как останавливаются для эскалации.

> **Базовые правила** — `AGENTS.md` (раздел 8): роли, лимит итераций,
> правила мержа Claude (§8.4), что НЕ трогать (§7). Этот файл —
> только то, что не дублирует AGENTS.md.

## 1. Жизненный цикл PR

```
Claude: ветка → код → локальный CI (lint/tsc/test) → gh pr create
                                  │
                                  ▼ pull_request: opened
                ┌─────────────────────────────────┐
                │  CI (lint-test-build) — required │
                │  Codex Review — оставляет коммент│
                └────────────────┬────────────────┘
                                 ▼
       Claude (autonomous loop через pr-watch.py + Monitor):
         • CI красный      → коммит `review: fix CI`
         • Codex коммент   → правка `review:` или обоснованный отказ в треде
         • CI зелёный + всё обработано + AGENTS.md §8.4 OK
                                 ▼
                    gh pr merge --squash --delete-branch
```

## 2. Реактивная петля без поллинга

`scripts/pr-watch.py` запускается через Claude Code Monitor (persistent,
session-length). Эмитит события в чат:

| Событие | Когда |
|---|---|
| `PR_OPENED #N` | Появился новый open-PR |
| `PR_CLOSED #N` | PR закрыт/мерджен |
| `PR_COMMENT #N by <user>` | Новый комментарий (особенно от github-actions[bot] = Codex) |
| `PR_CI #N` | Изменился состав CI checks |
| `PR_LABELS #N` | Изменился набор меток (used by approve-flow §3) |

Интервал: 30 секунд. При сбое `gh` (rate-limit / network) — `WATCH_ERROR`
с сохранением состояния (без ложных PR_CLOSED).

## 3. Approve-петля через GitHub Environment

Чтобы пользователь не получал email на каждый Codex-коммент, а только
когда нужно решение, используется **GitHub Deployment Environment +
Required Reviewers**:

```
Claude ставит `needs-human` на PR
        │
        ▼
needs-human-approval.yml ─── паузится в environment human-approval
        │                    (required reviewer = einarluha-a11y)
        │
        ▼
GitHub шлёт email с кнопкой Review deployments
        │
   ┌────┴────┐
 Approve   Reject / timeout
   │         │
   ▼         ▼
Снимает    needs-human-rejected.yml (workflow_run trigger):
needs-     • failure / timed_out → human-rejected
human,     • cancelled / прочее  → approval-flow-error
ставит
human-
approved
   │
   ▼
Watcher → PR_LABELS → Claude мержит (если human-approved + CI clean)
```

**Email-шум**: пользователь снял в `Settings → Notifications → Customize
email updates` галочки «Pull Request reviews» и «Comments on Issues and
Pull Requests». Email теперь приходит только на Actions deployment review.

## 4. Источник задач

`gh issue list --state open --label ai-ready` — бэклог, готовый для
автономного взятия. Метка `ai-ready` означает: задача описана, не
требует уточнений, есть acceptance criteria.

## 5. Известные ограничения

### 5.1 Codex видит секреты в head-ветке (документированный риск)

Workflow `codex-review.yml` запускается на `pull_request` из того же
репо и имеет доступ к `OPENAI_API_KEY`. Митигации:
- форки исключены (`head.repo.full_name == github.repository`),
- внешних коллабораторов в репо нет,
- `AGENTS.md §8.4` запрещает автомерж изменений `auth.ts`/`.env*`.

**TODO**: при появлении внешних коллабораторов — перевести
`OPENAI_API_KEY` в protected environment с required reviewer.

### 5.2 Лимит autonomous loop

Watcher живёт пока активна сессия Claude Code. Перезапуск сессии —
перезапустить Monitor с `python3 scripts/pr-watch.py`.

## 6. Связанные

- `AGENTS.md` — общие правила, правила мержа, что НЕ трогать.
- `.github/workflows/codex-review.yml` — Codex action.
- `.github/workflows/needs-human-approval.yml` + `needs-human-rejected.yml` — approve flow.
- `scripts/pr-watch.py` — реактивный watcher.
