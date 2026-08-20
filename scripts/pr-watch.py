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
from datetime import datetime, timezone


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


REPO = (
    "selftest/selftest"
    if "--self-test" in sys.argv
    else os.environ.get("REPO") or _detect_repo()
)
INTERVAL = int(os.environ.get("INTERVAL", "30"))
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
_WARNED_CREATED_AT = set()
_WARNED_COMMENT_ID_COLLISIONS = set()


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


def comment_key(c):
    """Ключ порядка и watermark: (created_at, id).

    Только id нельзя: id-пространства issue-comments (~5.4e9) и
    pull-review-comments (~3.0e9) НЕ согласованы — единый id-watermark
    после первого issue-комментария навсегда отсёк бы review-comments.
    created_at всегда приводится к aware datetime; id — тай-брейк.
    Непарсимая дата считается старой, чтобы не задрать watermark и не
    потерять весь последующий поток комментариев.
    """
    s = c.get("created_at") or ""
    if not s:
        return (_EPOCH, c["id"])
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # GitHub отдаёт UTC; aware обязателен для сравнения с watermark.
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt, c["id"])
    except Exception:
        # ШИРОКО намеренно: сюда попадает и смена типа поля в API
        # (не-строка → AttributeError на .replace). Падение watcher'а =
        # тихая остановка всей петли, что хуже деградации одного ключа.
        # Ключ анти-спама — строка: сырое значение может быть нехешируемым.
        warn_key = str(s)[:40]
        if warn_key not in _WARNED_CREATED_AT:
            _WARNED_CREATED_AT.add(warn_key)
            emit(f"WATCH_ERROR comment_key: unparsable created_at={warn_key}")
        return (_EPOCH, c["id"])


def list_comments(pr):
    """Issue-comments + pull-review-comments одной лентой.

    СТРОГО: сбой любого из двух эндпоинтов = GhError всей функции —
    частичная лента сдвинула бы watermark по неполным данным.
    Инвариант непересечения эндпоинтов проверяется в рантайме: коллизия
    вызывает громкое предупреждение, но не дедуп — он мог бы потерять
    легитимный комментарий из другого id-пространства.
    """
    issues = gh_json(["api", f"repos/{REPO}/issues/{pr}/comments"])
    reviews = gh_json(["api", f"repos/{REPO}/pulls/{pr}/comments"])
    collisions = {c["id"] for c in issues} & {c["id"] for c in reviews}
    new_collisions = sorted(
        collision for collision in collisions
        if (pr, collision) not in _WARNED_COMMENT_ID_COLLISIONS
    )
    if new_collisions:
        _WARNED_COMMENT_ID_COLLISIONS.update(
            (pr, collision) for collision in new_collisions
        )
        shown = ",".join(str(collision) for collision in new_collisions[:3])
        emit(
            f"WATCH_ERROR list_comments #{pr}: "
            f"id collision issue/pulls endpoints: {shown}"
        )
    return sorted(issues + reviews, key=comment_key)


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
        known[n] = {
            "last_comment_key": max((comment_key(c) for c in cs), default=(_EPOCH, 0)),
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

    known = {}            # pr_number -> {"last_comment_key": (datetime, int), "ci": str, "labels": str}
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
                known[n] = {
                    "last_comment_key": max(
                        (comment_key(c) for c in cs), default=(_EPOCH, 0)
                    ),
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
                last = state["last_comment_key"]
                new = [c for c in cs if comment_key(c) > last]
                if new:
                    # cs уже отсортирован comment_key'ем в list_comments.
                    for c in new:
                        author = c["user"]["login"]
                        body = (c["body"] or "").replace("\n", " ").replace("\r", " ")
                        if len(body) > 200:
                            body = body[:200] + "…"
                        emit(f"PR_COMMENT #{n} by {author} [id={c['id']}]: {body}")
                    state["last_comment_key"] = max(comment_key(c) for c in cs)
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


def self_test():
    """Детерминированная проверка ключей и объединения лент без сети/gh."""
    results = []
    captured = []
    original_emit = globals()["emit"]
    original_gh_json = globals()["gh_json"]

    def check(name, condition):
        results.append((name, bool(condition)))

    try:
        globals()["emit"] = captured.append
        _WARNED_CREATED_AT.clear()
        _WARNED_COMMENT_ID_COLLISIONS.clear()

        plain = {"id": 1, "created_at": "2026-08-20T12:34:56Z"}
        fractional = {"id": 2, "created_at": "2026-08-20T12:34:56.123Z"}
        offset = {"id": 3, "created_at": "2026-08-20T15:34:56+03:00"}
        naive = {"id": 4, "created_at": "2026-08-20T12:34:56"}
        empty = {"id": 5, "created_at": ""}
        broken = {"id": 6, "created_at": "not-a-date"}

        plain_key = comment_key(plain)
        check("zulu", isinstance(plain_key[0], datetime)
              and plain_key[0].tzinfo is not None
              and plain_key[0].utcoffset().total_seconds() == 0)

        fractional_key = comment_key(fractional)
        check("fractional", fractional_key[0] > plain_key[0])

        offset_key = comment_key(offset)
        check("offset", offset_key[0] == plain_key[0])

        naive_key = comment_key(naive)
        check("naive", naive_key[0].tzinfo is not None
              and naive_key[0] > _EPOCH)

        check("empty", comment_key(empty) == (_EPOCH, 5))

        before = len(captured)
        broken_key = comment_key(broken)
        comment_key(broken)
        warnings = captured[before:]
        check("unparsable", broken_key == (_EPOCH, 6)
              and len(warnings) == 1
              and warnings[0].startswith("WATCH_ERROR comment_key:"))

        # Смена типа поля в API: .replace() на не-строке даёт AttributeError,
        # а нехешируемое значение уронило бы ещё и анти-спам-множество.
        wrong_type = {"id": 7, "created_at": 1755690000}
        unhashable = {"id": 8, "created_at": {"iso": "2026-08-20T12:34:56Z"}}
        before = len(captured)
        wrong_type_key = comment_key(wrong_type)
        unhashable_key = comment_key(unhashable)
        comment_key(wrong_type)
        warnings = captured[before:]
        check("wrong-type", wrong_type_key == (_EPOCH, 7)
              and unhashable_key == (_EPOCH, 8)
              and len(warnings) == 2)

        mixed = [plain, fractional, offset, naive, broken, empty]
        ordered = sorted(mixed, key=comment_key)
        keys = [comment_key(c) for c in mixed]
        check("mixed-sort", all(isinstance(key[0], datetime)
                                and key[0].tzinfo is not None for key in keys)
              and [c["id"] for c in ordered] == [5, 6, 1, 3, 4, 2])

        check("watermark", all(comment_key(c) > (_EPOCH, 0) for c in mixed))

        issue_collision = [
            {"id": 10, "created_at": "2026-08-20T12:00:00Z"},
            {"id": 20, "created_at": "2026-08-20T12:02:00Z"},
        ]
        review_collision = [
            {"id": 10, "created_at": "2026-08-20T12:01:00Z"},
        ]

        def collision_gh(args):
            return issue_collision if "/issues/" in args[1] else review_collision

        globals()["gh_json"] = collision_gh
        before = len(captured)
        collision_feed = list_comments(7)
        list_comments(7)
        warnings = captured[before:]
        check("id-collision", len(collision_feed) == 3
              and len(warnings) == 1
              and warnings[0].startswith("WATCH_ERROR list_comments #7:"))

        issue_clean = [{"id": 30, "created_at": "2026-08-20T12:02:00Z"}]
        review_clean = [{"id": 40, "created_at": "2026-08-20T12:01:00Z"}]

        def clean_gh(args):
            return issue_clean if "/issues/" in args[1] else review_clean

        globals()["gh_json"] = clean_gh
        before = len(captured)
        clean_feed = list_comments(8)
        check("no-collision", captured[before:] == []
              and [c["id"] for c in clean_feed] == [40, 30])
    except Exception as e:
        results.append((f"unexpected: {type(e).__name__}: {e}", False))
    finally:
        globals()["emit"] = original_emit
        globals()["gh_json"] = original_gh_json

    failed = [name for name, ok in results if not ok]
    total = len(results)
    if failed:
        for name in failed:
            print(f"SELF-TEST FAIL {name}")
        print(f"SELF-TEST FAILED {len(failed)}/{total}")
        return 1
    print(f"SELF-TEST OK {total}/{total}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    main()
