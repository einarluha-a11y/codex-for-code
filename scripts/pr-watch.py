#!/usr/bin/env python3
"""Реактивный watcher open-PR. Каждая stdout-строка = событие для Claude.

События:
  WATCH_STARTED ...                      — однократно при старте
  PR_OPENED #N | <title>                 — появился новый open-PR
  PR_CLOSED #N                           — PR закрыт/мерджен
  PR_COMMENT #N by <user> [id=<id>]: <body>
  PR_CI #N | <name1=STATE,name2=STATE,...>
  PR_LABELS #N | <label1,label2,...>     — изменился набор меток
                                           (используется для needs-human /
                                           human-approved / human-rejected
                                           approve-петли, см. docs/ai-workflow.md §6.1)
  WATCH_STOPPED repo=<repo> signal=<signal>
"""
import json
import os
import signal
import subprocess
import sys
import time


def _detect_repo():
    """Auto-detect <owner>/<repo> from current git remote via gh CLI."""
    import subprocess
    try:
        out = subprocess.run(
            ['gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    raise RuntimeError("Cannot detect repo: set REPO env var or run inside a gh-authorized git repo.")


REPO = os.environ.get("REPO") or _detect_repo()
INTERVAL = int(os.environ.get("INTERVAL", "30"))


class GhError(Exception):
    """Ошибка вызова gh CLI (network/rate-limit/auth/timeout)."""


def gh(args):
    """Вызов gh. Возвращает stdout. Кидает GhError при сбое."""
    try:
        out = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        raise GhError(f"subprocess: {e}")
    if out.returncode != 0:
        raise GhError(f"exit={out.returncode}: {out.stderr.strip()[:160]}")
    return out.stdout.strip()


def gh_json(args):
    """Вызов gh + parse JSON. Кидает GhError при сбое."""
    raw = gh(args)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception as e:
        raise GhError(f"json: {e}")


def list_open_prs():
    """ВАЖНО: при ошибке кидает GhError. Пустой [] — это легитимно
    (PR'ов нет), не путать с ошибкой. Иначе watcher ложно эмитил бы
    PR_CLOSED для всех known PR при rate-limit / network glitch."""
    return gh_json(
        ["pr", "list", "--repo", REPO, "--state", "open",
         "--json", "number,title,labels"]
    )


def labels_str(pr_obj):
    """Стабильное строковое представление набора меток для diff."""
    names = sorted(l.get("name", "") for l in (pr_obj.get("labels") or []))
    return ",".join(names)


def list_comments(pr):
    """Issue-comments + pull-review-comments одной лентой.

    СТРОГО: сбой любого из двух эндпоинтов = GhError всей функции —
    частичная лента сдвинула бы watermark по неполным данным.
    """
    issues = gh_json(["api", f"repos/{REPO}/issues/{pr}/comments"])
    reviews = gh_json(["api", f"repos/{REPO}/pulls/{pr}/comments"])
    combined = {c["id"]: c for c in issues + reviews}
    return [combined[k] for k in sorted(combined)]


def ci_status(pr):
    raw = gh_json(
        ["pr", "checks", str(pr), "--repo", REPO,
         "--json", "name,state"]
    )
    pairs = sorted(f"{c['name']}={c['state']}" for c in raw)
    return ",".join(pairs)


def emit(line):
    # Один print = одно событие. flush обязателен — без него Monitor не видит.
    print(line, flush=True)


def snapshot(prs):
    """Снимок текущего состояния PR без эмита истории."""
    known = {}
    for pr in prs:
        n = pr["number"]
        cs = list_comments(n)
        max_id = max((c["id"] for c in cs), default=0)
        known[n] = {
            "last_comment_id": max_id,
            "ci": ci_status(n),
            "labels": labels_str(pr),
        }
    return known


def handle_signal(signum, _frame):
    emit(f"WATCH_STOPPED repo={REPO} signal={signal.Signals(signum).name}")
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    known = {}            # pr_number -> {"last_comment_id": int, "ci": str, "labels": str}
    bootstrapped = False

    # Init: snapshot текущего состояния, без эмита истории.
    try:
        initial = list_open_prs()
        known = snapshot(initial)
        bootstrapped = True
        emit(
            f"WATCH_STARTED repo={REPO} interval={INTERVAL}s "
            f"tracked_prs={len(known)}"
        )
    except GhError as e:
        emit(f"WATCH_ERROR init: {e}")
        # Стартуем без snapshot — следующая итерация попробует снова.

    while True:
        try:
            current = list_open_prs()
        except GhError as e:
            # Транзиентная ошибка gh (rate-limit / network / auth).
            # КРИТИЧНО: НЕ удаляем known PR — иначе при восстановлении
            # сети они будут "переоткрыты" с потерей контекста (CI/comments).
            emit(f"WATCH_ERROR list_prs: {e}")
            time.sleep(INTERVAL)
            continue

        if not bootstrapped:
            try:
                known = snapshot(current)
                bootstrapped = True
                emit(
                    f"WATCH_STARTED repo={REPO} interval={INTERVAL}s "
                    f"tracked_prs={len(known)}"
                )
            except GhError as e:
                emit(f"WATCH_ERROR init: {e}")
            time.sleep(INTERVAL)
            continue

        current_by_num = {pr["number"]: pr for pr in current}
        current_nums = {n: pr["title"] for n, pr in current_by_num.items()}

        # Новые open-PR
        for n, title in current_nums.items():
            if n not in known:
                emit(f"PR_OPENED #{n} | {title}")
                try:
                    cs = list_comments(n)
                except GhError:
                    cs = []
                max_id = max((c["id"] for c in cs), default=0)
                known[n] = {
                    "last_comment_id": max_id,
                    "ci": "",
                    "labels": labels_str(current_by_num[n]),
                }

        # Закрытые (только если list_open_prs() реально успешно отработал)
        for n in list(known.keys()):
            if n not in current_nums:
                emit(f"PR_CLOSED #{n}")
                del known[n]

        # Для каждого активного PR: новые комменты + изменения CI + метки.
        # Ошибки на конкретном PR не должны валить всю петлю.
        for n, state in list(known.items()):
            # Labels diff (для needs-human / human-approved / human-rejected).
            cur_labels = labels_str(current_by_num[n])
            if cur_labels != state["labels"]:
                emit(f"PR_LABELS #{n} | {cur_labels or '(none)'}")
                state["labels"] = cur_labels

            try:
                cs = list_comments(n)
                last = state["last_comment_id"]
                new = [c for c in cs if c["id"] > last]
                if new:
                    for c in sorted(new, key=lambda x: x["id"]):
                        author = c["user"]["login"]
                        body = (c["body"] or "").replace("\n", " ").replace("\r", " ")
                        if len(body) > 200:
                            body = body[:200] + "…"
                        emit(f"PR_COMMENT #{n} by {author} [id={c['id']}]: {body}")
                    state["last_comment_id"] = max(c["id"] for c in cs)
            except GhError as e:
                emit(f"WATCH_ERROR comments #{n}: {e}")

            try:
                cur_ci = ci_status(n)
                if cur_ci and cur_ci != state["ci"]:
                    emit(f"PR_CI #{n} | {cur_ci}")
                    state["ci"] = cur_ci
            except GhError as e:
                emit(f"WATCH_ERROR ci #{n}: {e}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
