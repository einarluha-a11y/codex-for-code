# codex-for-code

Reusable шаблоны для подключения **OpenAI Codex** как AI-ревьюера в любой
GitHub-проект, с **autonomous-loop** между **Claude Code** (исполнитель)
и **Codex** (ревьюер), и **GitHub Environment approval flow** для
эскалации пользователю без email-шума.

## Что внутри

```
.github/workflows/
  codex-review.yml             — Codex авто-ревью на каждый PR
  needs-human-approval.yml     — паузится в environment до Approve/Reject
  needs-human-rejected.yml     — обработка reject/timeout/cancel
scripts/
  pr-watch.py                  — реактивный watcher PR-событий
                                 (для Claude Code Monitor tool)
templates/
  AGENTS.md.template           — заготовка правил проекта
  labels.json                  — описание GitHub меток (создаются init-агентом)
docs/
  ai-workflow.md               — операционный регламент
```

## Использование

### Через Claude Code subagent (рекомендуется)

В Claude Code:

```
@codex-for-code  init
```

Subagent:
1. Определит текущий репо (`gh repo view`).
2. Клонирует эти шаблоны.
3. Скопирует workflows + scripts.
4. Создаст метки и environment `human-approval` через `gh api`.
5. Спросит тебя: добавь `OPENAI_API_KEY` в `Settings → Secrets → Actions`.
6. (Опционально) включит branch protection на `main`.

### Вручную

```bash
# 1. Клонировать шаблоны
git clone https://github.com/einarluha-a11y/codex-for-code.git /tmp/codex-init
cd <твой-проект>

# 2. Скопировать файлы
cp /tmp/codex-init/.github/workflows/* .github/workflows/
cp /tmp/codex-init/scripts/pr-watch.py scripts/
cp /tmp/codex-init/docs/ai-workflow.md docs/
cp /tmp/codex-init/templates/AGENTS.md.template AGENTS.md
# (отредактировать AGENTS.md под свой проект — placeholders {{...}})

# 3. Создать метки
cat /tmp/codex-init/templates/labels.json | jq -c '.[]' | while read l; do
  gh label create "$(echo $l | jq -r .name)" \
    --color  "$(echo $l | jq -r .color)" \
    --description "$(echo $l | jq -r .description)" || true
done

# 4. Создать environment с required reviewer
USER_ID=$(gh api user --jq .id)
gh api -X PUT "repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/environments/human-approval" \
  -F wait_timer=0 -F prevent_self_review=false \
  -f reviewers="[{\"type\":\"User\",\"id\":${USER_ID}}]"

# 5. Добавить OPENAI_API_KEY в Settings → Secrets → Actions (только UI)
```

## Архитектура

См. `docs/ai-workflow.md` после копирования в проект — там полная схема
жизненного цикла PR, реактивной петли через `pr-watch.py` + Claude Code
`Monitor`, и approval-flow через GitHub Environment.

## Источник

Изначально разработано в проекте
[einarluha-a11y/glt-portal](https://github.com/einarluha-a11y/glt-portal).
Этот репо — переносимая версия.
