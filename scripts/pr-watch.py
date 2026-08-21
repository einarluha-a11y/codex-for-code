#!/usr/bin/env python3
"""Реактивный watcher open-PR. Каждая stdout-строка = событие для Claude.

События:
  WATCH_STARTED ...                      — однократно при старте
  PR_OPENED #N | <title>                 — появился новый open-PR
  PR_CLOSED #N                           — PR закрыт/мерджен
  PR_COMMENT #N by <user> [id=<id>]: <body>
  PR_CI #N | <name1=STATE,name2=STATE,...>
  PR_REVIEW #N | <APPROVED|CHANGES_REQUESTED|REVIEW_REQUIRED|(none)>
                                         — изменилось решение ревью
  PR_LABELS #N | <label1,label2,...>     — изменился набор меток
                                           (используется для needs-human /
                                           human-approved / human-rejected
                                           approve-петли, см. docs/ai-workflow.md §6.1)
  WATCH_STOPPED repo=<repo> signal=<signal>
                                         — сигнал, NO_REPO (репозиторий не
                                           определился) или EXCEPTION
                                           (необработанный сбой)
"""
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone


def _detect_repo():
    """Auto-detect <owner>/<repo> from current git remote via gh CLI.

    Модуль уже импортировал subprocess сверху; локальный импорт здесь был
    лишним и вдобавок делал функцию непроверяемой — self-test подменяет
    ГЛОБАЛЬНЫЙ subprocess, а локальный импорт возвращал настоящий модуль и
    полез бы в сеть.
    """
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


# Определение репозитория НЕ делается на импорте. Раньше здесь стоял вызов
# _detect_repo(), и в неподготовленной среде (нет REPO, gh не авторизован)
# процесс падал ДО main() — то есть до первой stdout-строки. Для наблюдателя,
# у которого контракт «каждая строка = событие», это худший исход: ни
# WATCH_STARTED, ни WATCH_ERROR, ни WATCH_STOPPED — вызывающий видит просто
# мёртвый процесс и не знает причины. Теперь определение ленивое, внутри
# main(), и любой отказ выходит наружу событием.
def _repo_from_env(env=None):
    """REPO из окружения, нормализованный до «есть значение / нет значения».

    .strip() не косметика: REPO=" " (пробел из .env, launchd-plist или
    подстановки пустой переменной в шелле) — НЕ пустая строка, поэтому
    прежнее `or ""` его пропускало. Ленивое определение репозитория не
    включалось, и каждый вызов gh уходил с `--repo " "`: watcher жил бы,
    отвечая WATCH_ERROR на каждый опрос — постоянная слепота вместо
    громкого отказа на старте. После нормализации такой ввод неотличим от
    отсутствующего и уходит в авто-определение.

    Чтение окружения по-прежнему происходит на импорте — это намеренно:
    REPO нужен до входа в main(). Отдельной функцией вынесена не сама
    ленивость, а ПРОВЕРЯЕМОСТЬ: результат импортного вычисления уже лежит
    в глобальной REPO, и убедиться, что пробельный ввод отброшен, по нему
    нельзя — а вызвать функцию с подставленным окружением можно.
    """
    source = os.environ if env is None else env
    return (source.get("REPO") or "").strip()


REPO = (
    "selftest/selftest"
    if "--self-test" in sys.argv
    else _repo_from_env()
)
INTERVAL = int(os.environ.get("INTERVAL", "30"))
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
# Потолок множеств анти-спама: сброс лучше неограниченного роста в процессе,
# который живёт сутками.
_WARN_CAP = 1000
_WARNED_CREATED_AT = set()
_WARNED_COMMENT_ID_COLLISIONS = set()
_WARNED_PR_LIMIT = set()
_WARNED_TRUNCATED_CLOSE = set()
_WARNED_CI_SHAPE = set()
_WARNED_JSON_SHAPE = set()
_WARNED_PR_MISSING = set()
# Оба значения гасят «тихую слепоту» на пагинации: gh отдаёт 30 элементов по
# умолчанию и молча теряет остальное. per_page=100 экономит запросы (watcher
# опрашивает каждые INTERVAL секунд), --paginate добирает хвост.
_PER_PAGE = 100
_PR_LIMIT = 200
# gh без поля reviewDecision выходит с кодом 1 и ПУСТЫМ stdout (проверено на
# gh 2.83: stderr 'Unknown JSON field'), то есть КАЖДЫЙ опрос превращался бы в
# WATCH_ERROR и watcher жил бы, не отслеживая ничего. Один раз деградируем до
# набора без этого поля: теряются только события PR_REVIEW.
# --paginate делает НЕСКОЛЬКО HTTP-запросов подряд под ОДНИМ таймаутом, поэтому
# общий лимит обязан быть больше обычного. Замерено на gh 2.83 при быстром
# канале: одна страница ленты ~0.8 с, 31 страница ~15.3 с (≈0.49 с/страница) —
# то есть прежние общие 20 с ломались бы примерно на 40 страницах, а на вдвое
# более медленном канале уже на двадцати. Ценой был бы не сбой, а слепота:
# GhError на каждом опросе, и этот PR никогда не попал бы под наблюдение.
# Короткий лимит для остальных вызовов сохранён намеренно — быстрый отказ.
_GH_TIMEOUT = int(os.environ.get("GH_TIMEOUT", "20"))
_GH_TIMEOUT_PAGINATED = int(os.environ.get("GH_TIMEOUT_PAGINATED", "120"))
_PR_FIELDS = "number,title,labels,reviewDecision"
_PR_FIELDS_NO_REVIEW = "number,title,labels"
_UNKNOWN_FIELD_STDERR = "unknown json field"
_review_field_supported = True


class GhError(Exception):
    """Ошибка вызова gh CLI (network/rate-limit/auth/timeout)."""


class GhNoChecks(GhError):
    """У ветки PR нет НИ ОДНОЙ проверки.

    `gh pr checks` в этом случае выходит с exit=1 и пишет в stderr
    'no checks reported on the '<branch>' branch' — это не сбой gh, а
    легитимное состояние (workflow ещё не поставлен в очередь / нет
    воркфлоу на путях ветки). Подкласс GhError намеренно: код, который
    ловит GhError, ведёт себя как раньше; деградацию включает только тот,
    кто ЯВНО ловит GhNoChecks (ci_status). Настоящие сбои gh
    (rate-limit/auth/network) под этот класс не попадают и не маскируются.
    """


# Строка формата gh: "no checks reported on the '%s' branch" (cli/cli).
_NO_CHECKS_STDERR = "no checks reported"


def gh(args):
    """Вызов gh. Возвращает stdout. Кидает GhError при сбое.

    Таймаут зависит от того, многостраничный ли запрос: точное вхождение
    '--paginate' в аргументы, а не поиск подстроки — иначе путь, случайно
    содержащий это слово, тихо получил бы чужой лимит.
    """
    timeout = _GH_TIMEOUT_PAGINATED if "--paginate" in args else _GH_TIMEOUT
    try:
        out = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        raise GhError(f"subprocess: {e}")
    if out.returncode != 0:
        stderr = out.stderr.strip()
        if _NO_CHECKS_STDERR in stderr.lower():
            raise GhNoChecks(f"exit={out.returncode}: {stderr[:160]}")
        raise GhError(f"exit={out.returncode}: {stderr[:160]}")
    return out.stdout.strip()


def _parse_ndjson(raw):
    """Построчный разбор по принципу «всё или ничего».

    Возвращает список элементов либо None, если хотя бы одна непустая строка
    не разобралась. None означает «это не построчный JSON» — вызывающий
    поднимет ИСХОДНУЮ ошибку разбора.

    Частичный разбор здесь опаснее падения: watcher посчитал бы watermark по
    куску ленты и молча перестал бы эмитить новые события.

    «Ничего не разобрано» и «разобрано в пустой список» — РАЗНЫЕ исходы, и
    различает их отдельный признак, а не пустота результата. Строки `[]\\n[]`
    (страницы без элементов) — законный пустой ответ; пустой stdout — отказ
    gh. Общий `return items` уравнял бы их и превратил молчание gh в
    «комментариев нет», то есть в тихую слепоту watcher'а.
    """
    items = []
    parsed_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            return None
        parsed_any = True
        if isinstance(parsed, list):
            items.extend(parsed)
        else:
            items.append(parsed)
    return items if parsed_any else None


def gh_json(args):
    """Вызов gh + parse JSON. Кидает GhError при сбое.

    gh 2.83 склеивает страницы `--paginate` в ОДИН валидный массив (проверено:
    3014 элементов приходят одной строкой), поэтому основной путь — обычный
    разбор. Запасной путь на случай смены формата в будущих версиях gh:
    построчный JSON по принципу «всё или ничего». Без него смена формата дала
    бы GhError на КАЖДОМ опросе — watcher громко жалуется и при этом мёртв,
    ровно тот класс, что уже закрыт фолбэком для отсутствующего reviewDecision.
    """
    raw = gh(args)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception as e:
        items = _parse_ndjson(raw)
        if items is None:
            raise GhError(f"json: {e}")
        if _remember_warning(_WARNED_JSON_SHAPE, args[0] if args else "?"):
            emit("WATCH_ERROR json: gh отдал построчный JSON вместо одного "
                 "массива — формат вывода изменился, разобрано построчно")
        return items


def list_open_prs():
    """ВАЖНО: при ошибке кидает GhError. Пустой [] — это легитимно
    (PR'ов нет), не путать с ошибкой. Иначе watcher ложно эмитил бы
    PR_CLOSED для всех known PR при rate-limit / network glitch.

    --limit обязателен: без него gh отдаёт 30 PR и остальные для watcher'а
    просто не существуют. Флага --paginate у `gh pr list` НЕ существует
    (есть только --limit), поэтому полноту списка гарантировать нечем —
    вместо этого громко предупреждаем о возможном усечении.

    reviewDecision берётся здесь же — отдельный запрос к /pulls/{n}/reviews
    стоил бы ещё одного вызова на КАЖДЫЙ PR в КАЖДОМ опросе, а решение
    ревью доступно полем в уже делаемом запросе.
    """
    global _review_field_supported
    base = ["pr", "list", "--repo", REPO, "--state", "open",
            "--limit", str(_PR_LIMIT + 1), "--json"]
    if _review_field_supported:
        try:
            prs = gh_json(base + [_PR_FIELDS])
        except GhError as e:
            # ТОЛЬКО неизвестное поле. Прочие GhError (rate-limit / auth /
            # network) обязаны пробрасываться: молчаливый повтор запроса
            # замаскировал бы реальный сбой.
            if _UNKNOWN_FIELD_STDERR not in str(e).lower():
                raise
            _review_field_supported = False
            emit(
                "WATCH_ERROR list_prs: gh не знает поле reviewDecision — "
                "событий PR_REVIEW не будет, остальное отслеживается"
            )
            prs = gh_json(base + [_PR_FIELDS_NO_REVIEW])
    else:
        prs = gh_json(base + [_PR_FIELDS_NO_REVIEW])
    if _truncated(prs) and _remember_warning(_WARNED_PR_LIMIT, _PR_LIMIT):
        emit(
            f"WATCH_ERROR list_prs: открытых PR больше {_PR_LIMIT} — список "
            f"усечён, PR сверх лимита не отслеживаются, закрытия на таких "
            f"итерациях не эмитятся"
        )
    return _validated_prs(prs)


def _truncated(prs):
    """True, если снимок ЗАВЕДОМО неполон.

    Запрашивается на один PR больше лимита, поэтому усечение определяется
    точно, а не подозревается: ровно `_PR_LIMIT` записей означают полный
    список, `_PR_LIMIT + 1` — что дальше есть ещё. Сравнение `>=` с самим
    лимитом путало бы эти случаи и глушило закрытия на репозитории, где
    открытых PR ровно лимит.
    """
    return len(prs) > _PR_LIMIT


def _validated_prs(prs):
    """Проверка формы ответа — целиком, а не поэлементно.

    Ниже по коду номер берётся прямым `pr["number"]`, и промах поднял бы
    KeyError, который НЕ ловится `except GhError` и убил бы весь watcher,
    а не один PR.

    Отбрасывать кривую запись при этом нельзя: пропущенный PR неотличим от
    закрытого, и следующий шаг цикла соврал бы `PR_CLOSED`. По той же
    причине нельзя вернуть частичный список. Поэтому любая неожиданная
    запись превращает ВЕСЬ опрос в GhError: событий не будет, но и ложных
    тоже — вызывающий уже умеет пережить неудачный опрос.
    """
    if not isinstance(prs, list):
        raise GhError(
            f"форма ответа: ожидался список PR, пришло {type(prs).__name__}"
        )
    for pr in prs:
        num = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(num, int) or isinstance(num, bool):
            raise GhError(
                "форма ответа: запись PR без числового поля number"
            )
    return prs


def labels_str(pr_obj):
    """Стабильное строковое представление набора меток для diff."""
    names = sorted(l.get("name", "") for l in (pr_obj.get("labels") or []))
    return ",".join(names)


def emit(line):
    # Один print = одно событие. flush обязателен — без него Monitor не видит.
    print(line, flush=True)


def _norm_id_key(raw_id):
    """Однородный тай-брейк для любого id, включая отсутствующий.

    Прямое c["id"] роняло watcher на объекте без поля, а сравнение int
    против str при равных датах давало TypeError. Числовые id идут раньше
    нечисловых и не схлопываются в одно значение.

    У отсутствующего id СВОЙ разряд: иначе он давал бы (1, "None") и
    схлопывался с легитимным строковым id "None" в один ключ — при равных
    датах один из двух комментариев тихо считался бы уже виденным.
    """
    if raw_id is None:
        return (2, "")
    try:
        return (0, int(raw_id))
    except Exception:
        return (1, str(raw_id))


def _numeric_id(raw_id):
    """Числовое значение id или None, если привести нельзя.

    Ровно та же нормализация, что у тай-брейка сортировки, и намеренно
    через _norm_id_key: детект коллизий обязан видеть те же пары, что
    схлопнутся в одинаковый ключ (int 10 и строка "10"). Отдельная копия
    int()-логики разошлась бы с ключом при первой же правке.
    """
    kind, value = _norm_id_key(raw_id)
    return value if kind == 0 else None


def _remember_warning(warned, key):
    """True, если про этот ключ ещё не предупреждали.

    Watcher опрашивает GitHub каждые INTERVAL секунд: без памяти одно
    битое значение дало бы поток одинаковых строк в чате владельца.
    """
    if key in warned:
        return False
    if len(warned) >= _WARN_CAP:
        # Редкий повтор после сброса дешевле неограниченного роста.
        warned.clear()
    warned.add(key)
    return True


def _clear_warning_sets():
    """Сбросить ВСЕ множества анти-спама — детерминизм self-test.

    Контракт префикса: глобальное имя `_WARNED_*` принадлежит множествам
    анти-спама и ничему больше — заводить с этим префиксом иное значение
    нельзя. Перебор ограничен множествами, поэтому чужой тип он не тронет,
    но чужое МНОЖЕСТВО с таким именем молча попало бы под сброс.

    Перебором по globals, а не перечислением имён: перечисление забывается.
    Ровно так `_WARNED_JSON_SHAPE` и `_WARNED_PR_MISSING`, заведённые в
    соседних правках, остались вне сброса, и результат тестов начал
    зависеть от того, что процесс делал до них.
    """
    for name, value in list(globals().items()):
        if name.startswith("_WARNED_") and isinstance(value, set):
            value.clear()


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
        return (_EPOCH, _norm_id_key(c.get("id")))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # GitHub отдаёт UTC; aware обязателен для сравнения с watermark.
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt, _norm_id_key(c.get("id")))
    except Exception as e:
        # ШИРОКО намеренно: сюда попадает и смена типа поля в API
        # (не-строка → AttributeError на .replace). Падение watcher'а =
        # тихая остановка всей петли, что хуже деградации одного ключа.
        # Ключ анти-спама — строка: сырое значение может быть нехешируемым.
        warn_key = str(s)[:40]
        if _remember_warning(_WARNED_CREATED_AT, warn_key):
            err = f"{type(e).__name__}: {e}"[:60]
            emit(
                f"WATCH_ERROR comment_key: unparsable created_at={warn_key} "
                f"err={err}"
            )
        return (_EPOCH, _norm_id_key(c.get("id")))


def _validated_comments(items, kind):
    """Лента комментариев или GhError, если её форма чужая.

    Номер PR в сообщение не входит намеренно: вызывающий уже печатает его
    в `WATCH_ERROR comments #N`, и дубль только удлинял бы строку.

    Проверка стоит в ИСТОЧНИКЕ, а не у каждого потребителя. Элемент без
    `.get` роняет ленту в трёх местах сразу — детект коллизий id, ключ
    сортировки, эмит тела, — и точечная защита любого из них оставляет
    остальные открытыми. AttributeError не наследует GhError, поэтому
    пролетает мимо единственного `except` в петле и убивает watcher
    целиком, а не одну ленту.

    Отказ ленты целиком, а не пропуск кривых записей: watermark берётся
    как максимум ленты, поэтому частичная лента задрала бы его выше
    пропущенных комментариев — вернуться к ним watcher уже не смог бы
    никогда. Отказ переживается: следующая итерация повторит запрос.
    """
    if not isinstance(items, list):
        raise GhError(
            f"форма ленты {kind}: ожидался список, "
            f"пришло {type(items).__name__}"
        )
    for c in items:
        if not isinstance(c, dict):
            raise GhError(
                f"форма ленты {kind}: запись типа {type(c).__name__} "
                f"вместо объекта комментария"
            )
    return items


def _comments_path(kind, pr):
    """Путь к ленте комментариев PR. kind: issues | pulls."""
    return f"repos/{REPO}/{kind}/{pr}/comments?per_page={_PER_PAGE}"


def list_comments(pr):
    """Issue-comments + pull-review-comments одной лентой.

    СТРОГО: сбой любого из двух эндпоинтов = GhError всей функции —
    частичная лента сдвинула бы watermark по неполным данным.
    Инвариант непересечения эндпоинтов проверяется в рантайме: коллизия
    вызывает громкое предупреждение, но не дедуп — он мог бы потерять
    легитимный комментарий из другого id-пространства.

    Лента обязательно полная (--paginate): GitHub отдаёт страницами по
    возрастанию даты, поэтому без пагинации watcher на PR с числом
    комментариев больше страницы видел бы только САМЫЕ СТАРЫЕ, ставил
    watermark по ним и молча переставал эмитить новые.
    """
    issues = _validated_comments(
        gh_json(["api", "--paginate", _comments_path("issues", pr)]), "issues"
    )
    reviews = _validated_comments(
        gh_json(["api", "--paginate", _comments_path("pulls", pr)]), "pulls"
    )
    # В детект коллизий идут только приводимые к числу id: отсутствующий id
    # не образует коллизии, а c["id"] дал бы KeyError мимо GhError —
    # вызывающий код ловит только его, и watcher упал бы целиком. Проверка
    # isinstance(int) здесь была бы уже, чем ключ сортировки: пара int 10 /
    # строка "10" схлопывается в один ключ, но коллизией не считалась бы.
    issue_ids = {
        i for i in (_numeric_id(c.get("id")) for c in issues) if i is not None
    }
    review_ids = {
        i for i in (_numeric_id(c.get("id")) for c in reviews) if i is not None
    }
    new_collisions = [
        collision for collision in sorted(issue_ids & review_ids)
        if _remember_warning(_WARNED_COMMENT_ID_COLLISIONS, (pr, collision))
    ]
    if new_collisions:
        shown = ",".join(str(collision) for collision in new_collisions[:3])
        emit(
            f"WATCH_ERROR list_comments #{pr}: "
            f"id collision issue/pulls endpoints: {shown}"
        )
    return sorted(issues + reviews, key=comment_key)


def ci_status(pr):
    """Строка состояния проверок PR; "" = проверок нет.

    PR без единой проверки — НЕ ошибка: деградируем до "" (ровно тот же
    смысл, что у только что открытого PR в main()) — иначе один такой PR
    ронял бы snapshot() целиком и откладывал bootstrap отслеживания ВСЕХ
    остальных PR минимум на INTERVAL (инцидент claude-control-center:
    'WATCH_ERROR init: exit=1: no checks reported on the ... branch').
    Прочие сбои gh пробрасываются GhError — маскировать rate-limit/auth
    нельзя: watcher замолчал бы, выглядя работающим.
    """
    try:
        raw = gh_json(
            ["pr", "checks", str(pr), "--repo", REPO,
             "--json", "name,state"]
        )
    except GhNoChecks:
        return ""
    # Прямые c['name'] / c['state'] роняли бы watcher ЦЕЛИКОМ при смене
    # формы ответа: KeyError — не GhError, а вызывающий код ловит только
    # GhError. Неполную запись показываем как '?' и предупреждаем один раз:
    # тихо подставить пустое значение хуже — оператор увидел бы «проверок
    # нет» вместо «gh ответил не тем».
    pairs = []
    incomplete = False
    for c in raw:
        if not isinstance(c, dict):
            incomplete = True
            pairs.append("?=?")
            continue
        if "name" not in c or "state" not in c:
            incomplete = True
        pairs.append(f"{c.get('name', '?')}={c.get('state', '?')}")
    if incomplete and _remember_warning(_WARNED_CI_SHAPE, pr):
        emit(
            f"WATCH_ERROR ci #{pr}: ответ gh без полей name/state — "
            f"состояние проверок показано частично"
        )
    return ",".join(sorted(pairs))


def review_str(pr_obj):
    """Решение ревью: APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED / ""."""
    return pr_obj.get("reviewDecision") or ""


def snapshot(prs):
    """Снимок текущего состояния PR без эмита истории."""
    known = {}
    for pr in prs:
        n = pr["number"]
        cs = list_comments(n)
        known[n] = {
            "last_comment_key": max(
                (comment_key(c) for c in cs),
                default=(_EPOCH, _norm_id_key(0)),
            ),
            "ci": ci_status(n),
            "labels": labels_str(pr),
            "review": review_str(pr),
        }
    return known


def handle_signal(signum, _frame):
    # Обработчик ставится РАНЬШЕ определения репозитория, поэтому пустой
    # REPO здесь — штатное состояние, а не «не должно случиться». Токен
    # `repo=` без значения ломал бы разбор строки события по парам
    # key=value у потребителя; форма совпадает с той, что печатает run().
    # Имя сигнала берётся под страховкой. Это не про наблюдавшийся сбой:
    # обработчик стоит на SIGTERM и SIGINT, и ядро передаёт сюда только их,
    # так что Signals(signum) в проде не падает. Но handle_signal — ПОСЛЕДНЕЕ
    # место, которое печатает событие: исключение здесь означает ровно ту
    # тихую смерть, против которой писан весь раунд. Страховка стоит трёх
    # строк и снимает целый класс: чужой signum (функцию позовут напрямую или
    # повесят на новый сигнал) и окружение, где `signal` подменён.
    try:
        name = signal.Signals(signum).name
    except (ValueError, AttributeError):
        name = f"SIG{signum}"
    emit(f"WATCH_STOPPED repo={REPO or '(unknown)'} signal={name}")
    sys.exit(0)


def main():
    # Обработчики сигналов ставятся ПЕРВЫМИ — раньше любой работы, которая
    # может занять время. Прошлый порядок (сначала репозиторий) был моей
    # ошибкой: _detect_repo() запускает `gh repo view` с таймаутом 10 с, и
    # весь этот отрезок процесс жил без обработчиков — SIGTERM убивал его
    # молча, ровно тот исход, ради которого определение и переносилось с
    # импорта внутрь main(). Разменивалось это на косметику: пустой токен
    # `repo=` в раннем событии. После нормализации в handle_signal размена
    # больше нет — печатается `repo=(unknown)`, а окно молчания закрыто.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Репозиторий определяется здесь, а не на импорте: отказ обязан выйти
    # событием, а не падением до первой строки stdout.
    global REPO
    if not REPO:
        try:
            REPO = _detect_repo()
        except Exception as e:
            emit(f"WATCH_ERROR init: {e}")
            emit("WATCH_STOPPED repo=(unknown) signal=NO_REPO")
            return 1

    # pr_number -> {"last_comment_key": (datetime, (kind, value)),
    #               "ci": str, "labels": str, "review": str}
    # Второй элемент ключа — разряд из _norm_id_key, а не голый int.
    known = {}
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
                        (comment_key(c) for c in cs),
                        default=(_EPOCH, _norm_id_key(0)),
                    ),
                    "ci": "",
                    "labels": labels_str(current_by_num[n]),
                    "review": review_str(current_by_num[n]),
                }

        # Закрытые — только по ПОЛНОМУ снимку. Усечённый список лжёт ровно
        # так же, как сетевой сбой: отсутствие PR в ответе означает «его не
        # видно», а не «он закрыт». Событие необратимо (known теряет PR, и
        # следующий полный снимок соврёт ещё и PR_OPENED), поэтому на
        # заведомо неполном снимке шаг пропускается целиком. Пропуск
        # переживается: закрытия доедут, как только список перестанет
        # упираться в лимит.
        if _truncated(current):
            if _remember_warning(_WARNED_TRUNCATED_CLOSE, _PR_LIMIT):
                emit(
                    "WATCH_ERROR list_prs: снимок усечён лимитом — закрытия "
                    "PR на таких итерациях не эмитятся"
                )
        else:
            for n in list(known.keys()):
                if n not in current_nums:
                    emit(f"PR_CLOSED #{n}")
                    del known[n]

        # Для каждого активного PR: новые комменты + изменения CI + метки.
        # Ошибки на конкретном PR не должны валить всю петлю.
        for n, state in list(known.items()):
            # Снятие закрытых выше делает known подмножеством current_by_num,
            # так что промах ключа сейчас невозможен. Страховка нужна на случай
            # будущей правки порядка шагов: KeyError не ловится `except GhError`
            # ниже и убил бы ВЕСЬ watcher, а не один PR. Пропускаем громко —
            # тихий пропуск неотличим от «событий не было».
            pr_obj = current_by_num.get(n)
            if pr_obj is None:
                if _remember_warning(_WARNED_PR_MISSING, n):
                    emit(f"WATCH_ERROR #{n}: PR отсутствует в снимке — "
                         f"пропущен на этой итерации")
                continue

            # Labels diff (для needs-human / human-approved / human-rejected).
            cur_labels = labels_str(pr_obj)
            if cur_labels != state["labels"]:
                emit(f"PR_LABELS #{n} | {cur_labels or '(none)'}")
                state["labels"] = cur_labels

            # Решение ревью (approve / request changes) — из уже полученного
            # списка PR, без отдельного запроса на каждый PR.
            cur_review = review_str(pr_obj)
            if cur_review != state.get("review", ""):
                emit(f"PR_REVIEW #{n} | {cur_review or '(none)'}")
                state["review"] = cur_review

            try:
                cs = list_comments(n)
                last = state["last_comment_key"]
                new = [c for c in cs if comment_key(c) > last]
                if new:
                    # cs уже отсортирован comment_key'ем в list_comments.
                    for c in new:
                        # Безопасное извлечение: KeyError здесь не GhError и
                        # пролетел бы мимо except ниже, уронив весь watcher.
                        user = c.get("user")
                        if not isinstance(user, dict):
                            user = {}
                        author = user.get("login") or "?"
                        body = c.get("body") or ""
                        if not isinstance(body, str):
                            body = str(body)
                        body = body.replace("\n", " ").replace("\r", " ")
                        if len(body) > 200:
                            body = body[:200] + "…"
                        emit(
                            f"PR_COMMENT #{n} by {author} "
                            f"[id={c.get('id')}]: {body}"
                        )
                    state["last_comment_key"] = max(comment_key(c) for c in cs)
            except GhError as e:
                emit(f"WATCH_ERROR comments #{n}: {e}")

            try:
                cur_ci = ci_status(n)
                # Переход в "" НЕ эмитим намеренно: между push и созданием
                # чеков gh штатно отвечает 'no checks reported' → "", и эмит
                # на любое изменение давал бы ровно один ложный пустой PR_CI
                # на КАЖДЫЙ push каждого PR. Цена размена названа прямо:
                # редкий случай «проверки убрали совсем» остаётся незамеченным.
                if cur_ci and cur_ci != state["ci"]:
                    emit(f"PR_CI #{n} | {cur_ci}")
                    state["ci"] = cur_ci
            except GhError as e:
                emit(f"WATCH_ERROR ci #{n}: {e}")

        time.sleep(INTERVAL)


def _crash_site(exc):
    """Файл и строка самого глубокого кадра — локализация в ОДНУ строку.

    Ревью предлагало печатать полный traceback. Диагноз принят (по одной
    строке `TypeError: ...` не найти место падения), лечение — другое:
    контракт наблюдателя «каждая stdout-строка = событие», и многострочный
    дамп превратился бы в поток мусорных событий у любого потребителя,
    который читает stdout построчно. Место падения даёт ту же локализацию,
    не ломая контракт; полный traceback остаётся в stderr, если процесс
    падает по-настоящему.
    """
    try:
        frames = traceback.extract_tb(exc.__traceback__)
    except Exception:
        return ""
    if not frames:
        return ""
    last = frames[-1]
    return f" at {os.path.basename(last.filename)}:{last.lineno}"


def run():
    """Запуск main() с гарантией финального события.

    Контракт наблюдателя — «каждая stdout-строка = событие». Любое
    необработанное исключение внутри main() (не GhError, а любой сбой:
    смена формата ответа, MemoryError, баг правки) обрывало процесс молча:
    вызывающий видел прекратившийся поток и не мог отличить «упал» от
    «событий пока нет». Теперь такой сбой печатает причину и закрывает
    поток тем же WATCH_STOPPED, что и штатный сигнал.

    SystemExit пробрасывается как есть: его поднимает handle_signal, уже
    напечатавший своё WATCH_STOPPED, и перехват дал бы второе, ложное
    событие с signal=EXCEPTION.
    """
    try:
        return main() or 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        # Ctrl-C — не крах, а штатное завершение. Прежний перехват
        # BaseException делал его неотличимым от бага: печатал
        # `WATCH_ERROR fatal: KeyboardInterrupt` и возвращал 1, то есть
        # соврал бы и потребителю событий, и коду выхода (130 → 1).
        # Событие всё равно нужно — окно до регистрации обработчиков
        # существует, и в нём SIGINT приходит сюда исключением, — но само
        # исключение пробрасывается, чтобы интерпретатор завершился как
        # положено. Форма события совпадает с сигнальным путём.
        emit(f"WATCH_STOPPED repo={REPO or '(unknown)'} signal=SIGINT")
        raise
    except Exception as e:
        emit(f"WATCH_ERROR fatal: {type(e).__name__}: {e}{_crash_site(e)}")
        emit(f"WATCH_STOPPED repo={REPO or '(unknown)'} signal=EXCEPTION")
        return 1


def self_test():
    """Детерминированная проверка ключей и объединения лент без сети/gh."""
    results = []
    captured = []
    original_emit = globals()["emit"]
    original_gh_json = globals()["gh_json"]
    original_subprocess = globals()["subprocess"]
    original_review_field = globals()["_review_field_supported"]
    original_signal = globals()["signal"]
    original_time = globals()["time"]
    original_repo = globals()["REPO"]
    original_main = globals()["main"]

    def check(name, condition):
        results.append((name, bool(condition)))

    try:
        globals()["emit"] = captured.append
        _clear_warning_sets()

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

        check("empty", comment_key(empty) == (_EPOCH, _norm_id_key(5)))

        before = len(captured)
        broken_key = comment_key(broken)
        comment_key(broken)
        warnings = captured[before:]
        check("unparsable", broken_key == (_EPOCH, _norm_id_key(6))
              and len(warnings) == 1
              and warnings[0].startswith("WATCH_ERROR comment_key:"))

        # Диагностика в предупреждении: без типа/текста исключения непонятно,
        # ЧЕМ именно не понравилось значение.
        check("unparsable-diagnostic", "err=" in warnings[0])

        # Смена типа поля в API: .replace() на не-строке даёт AttributeError,
        # а нехешируемое значение уронило бы ещё и анти-спам-множество.
        wrong_type = {"id": 7, "created_at": 1755690000}
        unhashable = {"id": 8, "created_at": {"iso": "2026-08-20T12:34:56Z"}}
        before = len(captured)
        wrong_type_key = comment_key(wrong_type)
        unhashable_key = comment_key(unhashable)
        comment_key(wrong_type)
        warnings = captured[before:]
        check("wrong-type", wrong_type_key == (_EPOCH, _norm_id_key(7))
              and unhashable_key == (_EPOCH, _norm_id_key(8))
              and len(warnings) == 2)

        # Объект без id: прямое c["id"] роняло бы watcher на первом же
        # изменении формата ответа.
        missing_id_key = comment_key({"created_at": ""})
        check("missing-id", missing_id_key == (_EPOCH, _norm_id_key(None))
              and missing_id_key < comment_key(plain))

        # Разные типы id при РАВНЫХ датах: голое сравнение int против str
        # даёт TypeError и валит сортировку всей ленты.
        same_date_int = {"id": 42, "created_at": "2026-08-20T12:34:56Z"}
        same_date_text = {"id": "abc", "created_at": "2026-08-20T12:34:56Z"}
        check("nonnumeric-id-order",
              [c.get("id") for c in sorted([same_date_text, same_date_int],
                                           key=comment_key)] == [42, "abc"])

        mixed = [plain, fractional, offset, naive, broken, empty]
        ordered = sorted(mixed, key=comment_key)
        keys = [comment_key(c) for c in mixed]
        check("mixed-sort", all(isinstance(key[0], datetime)
                                and key[0].tzinfo is not None for key in keys)
              and [c["id"] for c in ordered] == [5, 6, 1, 3, 4, 2])

        check("watermark",
              all(comment_key(c) > (_EPOCH, _norm_id_key(0)) for c in mixed))

        # Анти-спам не имеет права расти без границы в процессе, который
        # живёт сутками.
        _WARNED_CREATED_AT.clear()
        for i in range(_WARN_CAP + 10):
            comment_key({"id": i, "created_at": f"broken-{i}"})
        check("warning-cap", len(_WARNED_CREATED_AT) <= _WARN_CAP)

        issue_collision = [
            {"id": 10, "created_at": "2026-08-20T12:00:00Z"},
            {"id": 20, "created_at": "2026-08-20T12:02:00Z"},
        ]
        review_collision = [
            {"id": 10, "created_at": "2026-08-20T12:01:00Z"},
        ]

        def _is_issues(args):
            """Признак эндпоинта, не зависящий от позиции пути и query."""
            return "issues" in args[-1].split("?")[0].split("/")

        calls = []

        def collision_gh(args):
            calls.append(args)
            return issue_collision if _is_issues(args) else review_collision

        globals()["gh_json"] = collision_gh
        before = len(captured)
        collision_feed = list_comments(7)
        list_comments(7)
        warnings = captured[before:]
        check("id-collision", len(collision_feed) == 3
              and len(warnings) == 1
              and warnings[0].startswith("WATCH_ERROR list_comments #7:"))

        # Регресс-защита: без --paginate watcher видит только первую
        # страницу (самые СТАРЫЕ комментарии) и молча слепнет на активном PR.
        check("pagination",
              bool(calls)
              and all("--paginate" in args for args in calls)
              and all(f"per_page={_PER_PAGE}" in args[-1] for args in calls))

        issue_clean = [{"id": 30, "created_at": "2026-08-20T12:02:00Z"}]
        review_clean = [{"id": 40, "created_at": "2026-08-20T12:01:00Z"}]

        def clean_gh(args):
            return issue_clean if _is_issues(args) else review_clean

        globals()["gh_json"] = clean_gh
        before = len(captured)
        clean_feed = list_comments(8)
        check("no-collision", captured[before:] == []
              and [c["id"] for c in clean_feed] == [40, 30])

        # Объекты без id в ленте: детект коллизий обязан их игнорировать,
        # а не падать KeyError мимо GhError.
        issue_missing = [
            {"created_at": "2026-08-20T12:00:00Z"},
            {"id": 50, "created_at": "2026-08-20T12:02:00Z"},
        ]
        review_missing = [
            {"created_at": "2026-08-20T12:01:00Z"},
            {"id": 60, "created_at": "2026-08-20T12:03:00Z"},
        ]

        def missing_gh(args):
            return issue_missing if _is_issues(args) else review_missing

        globals()["gh_json"] = missing_gh
        before = len(captured)
        missing_feed = list_comments(9)
        check("missing-id-feed",
              len(missing_feed) == 4 and captured[before:] == [])

        check("review-decision",
              review_str({"reviewDecision": "APPROVED"}) == "APPROVED"
              and review_str({"reviewDecision": None}) == ""
              and review_str({}) == "")

        # Инцидент claude-control-center: ОДИН PR без чеков ронял snapshot()
        # целиком → bootstrapped=False, known пуст, отслеживание ВСЕХ PR
        # откладывалось минимум на INTERVAL.
        def snapshot_gh(args):
            if args[0] == "pr" and args[1] == "checks":
                if args[2] == "2":
                    raise GhNoChecks(
                        "exit=1: no checks reported on the 'topic' branch"
                    )
                return [{"name": "codex", "state": "SUCCESS"}]
            return []

        globals()["gh_json"] = snapshot_gh
        snap = snapshot([{"number": 1, "labels": []}, {"number": 2, "labels": []}])
        check("snapshot-survives-no-checks",
              set(snap) == {1, 2}
              and snap[1]["ci"] == "codex=SUCCESS"
              and snap[2]["ci"] == "")

        # Классификация stderr — на уровне gh(), с настоящим gh_json.
        globals()["gh_json"] = original_gh_json

        class _Result:
            def __init__(self, returncode, stdout, stderr):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        class _FakeSubprocess:
            """Подменяет ТОЛЬКО subprocess.run в gh(): без сети и без gh CLI."""

            def __init__(self, result):
                self._result = result

            def run(self, *_args, **_kwargs):
                return self._result

        globals()["subprocess"] = _FakeSubprocess(
            _Result(1, "", "no checks reported on the 'topic' branch\n")
        )
        no_checks_error = None
        try:
            gh(["pr", "checks", "1", "--repo", REPO, "--json", "name,state"])
        except GhError as e:
            no_checks_error = e
        check("no-checks-classified", isinstance(no_checks_error, GhNoChecks))
        check("no-checks-degrades", ci_status(1) == "")

        # Настоящий сбой gh не имеет права превратиться в пустой CI: иначе
        # rate-limit/auth выглядели бы как "проверок нет" и watcher молчал.
        globals()["subprocess"] = _FakeSubprocess(
            _Result(1, "", "API rate limit exceeded for user ID 1")
        )
        rate_limit_error = None
        try:
            ci_status(1)
        except GhError as e:
            rate_limit_error = e
        check("gh-error-not-masked",
              isinstance(rate_limit_error, GhError)
              and not isinstance(rate_limit_error, GhNoChecks))

        # Многостраничный запрос обязан получать БОЛЬШИЙ таймаут: --paginate
        # делает несколько HTTP-запросов подряд под одним лимитом (замерено:
        # 31 страница ≈ 15.3 с при быстром канале), и прежние общие 20 с
        # обрекли бы активный PR на GhError в КАЖДОМ опросе.
        class _TimeoutSpy:
            """Ловит kwargs subprocess.run, не выходя в сеть."""

            def __init__(self):
                self.timeouts = []

            def run(self, *_args, **kwargs):
                self.timeouts.append(kwargs.get("timeout"))
                return _Result(0, "[]", "")

        spy = _TimeoutSpy()
        globals()["subprocess"] = spy
        gh(["api", "--paginate", "repos/x/y/issues/1/comments?per_page=100"])
        gh(["pr", "checks", "1", "--repo", REPO, "--json", "name,state"])
        gh(["api", "repos/x/y/pulls?paginate-lookalike=1"])
        check("paginated-timeout",
              spy.timeouts == [_GH_TIMEOUT_PAGINATED, _GH_TIMEOUT, _GH_TIMEOUT]
              and _GH_TIMEOUT_PAGINATED > _GH_TIMEOUT)

        globals()["subprocess"] = original_subprocess

        # Ответ gh без ожидаемых полей: прямое c['name'] дало бы KeyError,
        # который НЕ GhError и уронил бы весь watcher, а не один PR.
        _WARNED_CI_SHAPE.clear()

        def broken_shape_gh(_args):
            return [{"name": "codex"}, {"state": "SUCCESS"}, "не-словарь"]

        globals()["gh_json"] = broken_shape_gh
        before = len(captured)
        shape_ci = ci_status(11)
        ci_status(11)
        warnings = captured[before:]
        check("ci-shape-survives",
              shape_ci == "?=?,?=SUCCESS,codex=?"
              and len(warnings) == 1
              and warnings[0].startswith("WATCH_ERROR ci #11:"))

        # Отсутствующий id обязан иметь СВОЙ разряд: (1, "None") схлопнулся
        # бы с легитимным строковым id "None" в один ключ.
        check("missing-id-bucket",
              _norm_id_key(None) == (2, "")
              and _norm_id_key("None") == (1, "None")
              and _norm_id_key(None) != _norm_id_key("None")
              and _norm_id_key(5) < _norm_id_key("abc") < _norm_id_key(None))

        # Сошлось у двух независимых ревьюеров: детект коллизий обязан
        # видеть ровно те пары, что схлопываются в одинаковый тай-брейк.
        # isinstance(int) пропускал пару int 10 / строка "10".
        _WARNED_COMMENT_ID_COLLISIONS.clear()
        issue_mixed = [{"id": 10, "created_at": "2026-08-20T12:00:00Z"}]
        review_mixed = [{"id": "10", "created_at": "2026-08-20T12:01:00Z"}]

        def mixed_type_gh(args):
            return issue_mixed if _is_issues(args) else review_mixed

        globals()["gh_json"] = mixed_type_gh
        before = len(captured)
        list_comments(12)
        warnings = captured[before:]
        check("collision-mixed-type",
              _norm_id_key(10) == _norm_id_key("10")
              and len(warnings) == 1
              and "id collision" in warnings[0])

        # Усечение списка PR: --paginate у `gh pr list` не существует, но
        # молчать про потерю PR нельзя. Запрашивается лимит+1, поэтому
        # усечение определяется точно.
        _WARNED_PR_LIMIT.clear()
        globals()["gh_json"] = lambda _args: [
            {"number": i, "labels": []} for i in range(_PR_LIMIT + 1)
        ]
        before = len(captured)
        list_open_prs()
        list_open_prs()
        warnings = captured[before:]
        check("pr-limit-truncation",
              len(warnings) == 1 and "список усечён" in warnings[0])

        # Ровно лимит — список ПОЛНЫЙ, а не «подозрительный». Прежнее
        # сравнение `>=` глушило бы здесь закрытия PR навсегда.
        _WARNED_PR_LIMIT.clear()
        globals()["gh_json"] = lambda _args: [
            {"number": i, "labels": []} for i in range(_PR_LIMIT)
        ]
        before = len(captured)
        exact = list_open_prs()
        check("pr-limit-exact-is-complete",
              captured[before:] == [] and not _truncated(exact))

        globals()["gh_json"] = lambda _args: [{"number": 1, "labels": []}]
        before = len(captured)
        list_open_prs()
        check("pr-limit-quiet-below", captured[before:] == [])

        # Запрашиваем на один PR больше лимита — иначе полный список и
        # усечённый неотличимы.
        asked = []
        globals()["gh_json"] = lambda args: (asked.append(args) or [])
        list_open_prs()
        check("pr-limit-asks-one-over",
              any(str(_PR_LIMIT + 1) in a for a in asked[0]))

        # Старый gh не знает reviewDecision: без фолбэка КАЖДЫЙ опрос давал
        # бы WATCH_ERROR, и watcher жил бы, не отслеживая ничего.
        globals()["_review_field_supported"] = True
        fallback_calls = []

        def old_gh(args):
            fallback_calls.append(args)
            if _PR_FIELDS in args:
                raise GhError('exit=1: Unknown JSON field: "reviewDecision"')
            return [{"number": 3, "labels": []}]

        globals()["gh_json"] = old_gh
        before = len(captured)
        degraded = list_open_prs()
        list_open_prs()
        warnings = captured[before:]
        check("review-field-fallback",
              degraded == [{"number": 3, "labels": []}]
              and len(warnings) == 1
              and "reviewDecision" in warnings[0]
              # Второй опрос уже не пробует неизвестное поле повторно.
              and len(fallback_calls) == 3)

        # Настоящий сбой gh не имеет права превратиться в повтор запроса:
        # rate-limit замаскировался бы под «старый gh».
        globals()["_review_field_supported"] = True
        retry_calls = []

        def rate_limited_gh(args):
            retry_calls.append(args)
            raise GhError("exit=1: API rate limit exceeded for user ID 1")

        globals()["gh_json"] = rate_limited_gh
        propagated = None
        try:
            list_open_prs()
        except GhError as e:
            propagated = e
        check("review-fallback-not-masking",
              isinstance(propagated, GhError)
              and len(retry_calls) == 1
              and globals()["_review_field_supported"] is True)

        # --- форма ответа gh: один массив, построчный JSON, обрыв ---
        globals()["gh_json"] = original_gh_json

        # Основной путь: gh 2.83 отдаёт ОДИН валидный массив.
        check("json-single-array",
              _parse_ndjson('[{"id": 1}, {"id": 2}]') == [{"id": 1},
                                                          {"id": 2}])

        # Смена формата: построчный JSON собирается в общий список.
        check("json-ndjson-merged",
              _parse_ndjson('{"id": 1}\n{"id": 2}') == [{"id": 1}, {"id": 2}])
        check("json-ndjson-arrays-flattened",
              _parse_ndjson('[{"id": 1}]\n[{"id": 2}]') == [{"id": 1},
                                                            {"id": 2}])

        # Обрыв на середине НЕ имеет права разобраться частично: половина
        # ленты дала бы watermark по куску и молчание вместо событий.
        check("json-truncated-rejected",
              _parse_ndjson('{"id": 1}\n{"id": 2') is None)
        check("json-garbage-rejected", _parse_ndjson("не json") is None)
        check("json-empty-rejected", _parse_ndjson("\n  \n") is None)

        # gh_json на построчном ответе: разбирает и предупреждает ОДИН раз.
        _WARNED_JSON_SHAPE.clear()
        globals()["subprocess"] = _FakeSubprocess(
            _Result(0, '{"id": 1}\n{"id": 2}', ""))
        before = len(captured)
        ndjson_result = gh_json(["api", "x"])
        first_warned = len(captured) - before
        ndjson_again = gh_json(["api", "x"])
        second_warned = len(captured) - before - first_warned
        check("json-ndjson-warns-once",
              ndjson_result == [{"id": 1}, {"id": 2}]
              and ndjson_again == ndjson_result
              and first_warned == 1
              and second_warned == 0)

        # Обрыв поднимает ИСХОДНУЮ ошибку разбора, а не тихий частичный список.
        globals()["subprocess"] = _FakeSubprocess(
            _Result(0, '{"id": 1}\n{"id": 2', ""))
        raised = None
        try:
            gh_json(["api", "x"])
        except GhError as e:
            raised = e
        check("json-truncated-raises", isinstance(raised, GhError))

        # --- форма списка PR: номер обязан быть числом ---
        good = [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}]
        check("pr-shape-passthrough", _validated_prs(good) == good)

        def rejects(value):
            try:
                _validated_prs(value)
            except GhError:
                return True
            return False

        # Каждый случай убил бы watcher на прямом pr["number"].
        check("pr-shape-missing-number", rejects([{"title": "нет номера"}]))
        check("pr-shape-string-number", rejects([{"number": "7"}]))
        check("pr-shape-bool-number", rejects([{"number": True}]))
        check("pr-shape-not-dict", rejects(["не словарь"]))
        check("pr-shape-not-list", rejects({"number": 1}))

        # Пустой список легитимен: PR'ов просто нет.
        check("pr-shape-empty-ok", _validated_prs([]) == [])

        # Кривая запись НЕ имеет права стать частичным списком: пропущенный
        # PR неотличим от закрытого, и watcher соврал бы PR_CLOSED.
        mixed = [{"number": 1}, {"title": "кривая"}, {"number": 3}]
        check("pr-shape-no-partial", rejects(mixed))

        # Проверки выше доказывают ЛОГИКУ валидатора, но проходят и тогда,
        # когда он не вызывается из list_open_prs (проверено откатом).
        # Поэтому отдельно доказываем ПРОВОДКУ — иначе это ровно тот случай,
        # когда механизм построен, выглядит рабочим и ни к чему не подключён.
        globals()["_review_field_supported"] = True
        globals()["gh_json"] = lambda _args: [{"title": "без номера"}]
        wired = None
        try:
            list_open_prs()
        except GhError as e:
            wired = e
        check("pr-shape-wired-into-list-prs", isinstance(wired, GhError))

        # --- форма ленты комментариев: только объекты ---
        feed = [{"id": 1, "created_at": "2026-08-20T12:00:00Z"}]
        check("comments-shape-passthrough",
              _validated_comments(feed, "issues") == feed)
        check("comments-shape-empty-ok",
              _validated_comments([], "pulls") == [])

        def rejects_feed(value):
            try:
                _validated_comments(value, "issues")
            except GhError:
                return True
            return False

        # Строка вместо объекта роняла бы .get на AttributeError — мимо
        # GhError, то есть уносила бы весь watcher, а не одну ленту.
        check("comments-shape-not-dict", rejects_feed(["строка"]))
        check("comments-shape-not-list", rejects_feed({"id": 1}))
        check("comments-shape-no-partial",
              rejects_feed([{"id": 1}, "кривая", {"id": 2}]))

        # Точечная защита только детекта коллизий оставила бы ключ
        # сортировки открытым: тот же элемент падает и в comment_key.
        sort_crash = None
        try:
            sorted(["не объект"], key=comment_key)
        except AttributeError as e:
            sort_crash = e
        check("comments-shape-sort-key-also-unsafe",
              isinstance(sort_crash, AttributeError))

        # Проводка: кривую ленту обязана отвергать РАБОЧАЯ функция, а не
        # только валидатор в вакууме.
        globals()["gh_json"] = lambda _args: [{"id": 1}, "не объект"]
        feed_wired = None
        try:
            list_comments(1)
        except GhError as e:
            feed_wired = e
        check("comments-shape-wired-into-list-comments",
              isinstance(feed_wired, GhError))

        # --- сброс анти-спама покрывает и множества, которых ещё нет ---
        globals()["_WARNED_SELFTEST_PROBE"] = {"остаток"}
        try:
            _clear_warning_sets()
            check("warning-reset-covers-new-sets",
                  not globals()["_WARNED_SELFTEST_PROBE"])
        finally:
            del globals()["_WARNED_SELFTEST_PROBE"]

        # --- пустой построчный ответ ≠ отсутствие ответа ---
        # Страницы без элементов — законный пустой результат.
        check("json-ndjson-empty-pages-ok", _parse_ndjson("[]\n[]") == [])
        check("json-ndjson-empty-object-list-ok",
              _parse_ndjson("[]") == [] or _parse_ndjson("[]\n") == [])
        # А пустой stdout остаётся отказом: буквальный `return items` из
        # рекомендации ревью уравнял бы молчание gh с «событий нет» —
        # то есть превратил бы сбой в тихую слепоту watcher'а.
        check("json-empty-stdout-still-rejected",
              _parse_ndjson("\n  \n") is None)

        # Настоящий gh_json: выше он подменён заглушкой предыдущего теста.
        globals()["gh_json"] = original_gh_json
        globals()["subprocess"] = _FakeSubprocess(_Result(0, "[]\n[]", ""))
        check("json-ndjson-empty-wired", gh_json(["api", "x"]) == [])

        # --- закрытия PR не эмитятся на заведомо неполном снимке ---
        class _StopLoop(Exception):
            pass

        class _SignalName:
            """Мимикрия signal.Signals: объект с .name, ValueError на чужой.

            Прежняя заглушка отдавала голое число, у которого нет .name.
            После появления страховки в handle_signal это стало опасно:
            фейк молча уводил бы КАЖДЫЙ вызов в аварийную ветку, и
            регрессия основной прошла бы незамеченной. Заглушка обязана
            повторять интерфейс подменяемого, иначе она проверяет не то.
            """

            _NAMES = {15: "SIGTERM", 2: "SIGINT"}

            def __init__(self, value):
                if value not in self._NAMES:
                    raise ValueError(value)
                self.name = self._NAMES[value]

        class _FakeSignal:
            SIGTERM = 15
            SIGINT = 2

            @staticmethod
            def signal(_sig, _handler):
                return None

            @staticmethod
            def Signals(value):
                return _SignalName(value)

        class _FakeTime:
            @staticmethod
            def sleep(_seconds):
                raise _StopLoop()

        def one_iteration(second_snapshot):
            """Бутстрап на PR 1 и 2, затем ОДНА итерация с другим списком."""
            _clear_warning_sets()
            snapshots = [
                [{"number": 1, "title": "первый", "labels": []},
                 {"number": 2, "title": "второй", "labels": []}],
                second_snapshot,
            ]

            def loop_gh(args):
                if args[0] == "pr" and args[1] == "list":
                    return snapshots.pop(0) if len(snapshots) > 1 else \
                        snapshots[0]
                return []

            globals()["gh_json"] = loop_gh
            globals()["signal"] = _FakeSignal
            globals()["time"] = _FakeTime
            start = len(captured)
            try:
                main()
            except _StopLoop:
                pass
            return captured[start:]

        truncated = [
            {"number": i, "title": f"pr-{i}", "labels": []}
            for i in range(100, 100 + _PR_LIMIT + 1)
        ]
        lines = one_iteration(truncated)
        check("truncated-snapshot-no-false-close",
              not any(line.startswith("PR_CLOSED") for line in lines))

        # Контроль: на ПОЛНОМ снимке закрытие обязано эмититься. Без него
        # проверка выше проходила бы и при петле, которая не эмитит ничего.
        lines = one_iteration([{"number": 2, "title": "второй", "labels": []}])
        check("full-snapshot-still-closes",
              any(line == "PR_CLOSED #1" for line in lines))

        # --- Старт без репозитория: событие, а не молчаливая смерть ---
        # Раньше _detect_repo() звался на импорте и неподготовленная среда
        # убивала процесс ДО первой stdout-строки.
        globals()["REPO"] = ""
        globals()["subprocess"] = _FakeSubprocess(
            _Result(1, "", "gh: not authenticated")
        )
        start = len(captured)
        # main() обязан вернуться СРАЗУ. Если ленивое определение уберут,
        # он уйдёт в петлю и умрёт на подменённом sleep — ловим здесь,
        # чтобы это было именованным провалом, а не обрывом остальных
        # кейсов через общий except.
        try:
            rc = main()
        except BaseException as e:
            rc = f"не вернулся: {type(e).__name__}"
        lines = captured[start:]
        check(
            "no-repo-emits-events-not-crash",
            rc == 1
            and any(line.startswith("WATCH_ERROR init:") for line in lines)
            and any(line == "WATCH_STOPPED repo=(unknown) signal=NO_REPO"
                    for line in lines),
        )

        # Обработчики сигналов обязаны стоять РАНЬШЕ определения репозитория:
        # `gh repo view` держит до 10 с, и SIGTERM в этом окне убивал процесс
        # молча. Проба снимает список зарегистрированных сигналов В МОМЕНТ
        # вызова gh — если порядок переставят обратно, он окажется пустым.
        registered = []

        class _OrderProbeSignal:
            SIGTERM = 15
            SIGINT = 2

            @staticmethod
            def signal(sig, _handler):
                registered.append(sig)

            @staticmethod
            def Signals(value):
                return _SignalName(value)

        class _OrderProbeSubprocess:
            seen_at_call = None

            def run(self, *_args, **_kwargs):
                _OrderProbeSubprocess.seen_at_call = list(registered)
                return _Result(1, "", "gh: not authenticated")

        globals()["REPO"] = ""
        globals()["signal"] = _OrderProbeSignal
        globals()["subprocess"] = _OrderProbeSubprocess()
        try:
            main()
        except BaseException as e:
            _OrderProbeSubprocess.seen_at_call = f"main упал: {type(e).__name__}"
        check(
            "signal-handlers-registered-before-repo-detection",
            _OrderProbeSubprocess.seen_at_call == [15, 2],
        )
        globals()["signal"] = _FakeSignal

        # Определение репозитория обязано ходить через ГЛОБАЛЬНЫЙ subprocess.
        # Вернётся локальный `import subprocess` внутри _detect_repo() — этот
        # кейс поймает: подменённый ответ перестанет доезжать, и функция
        # уйдёт в настоящую сеть.
        globals()["subprocess"] = _FakeSubprocess(
            _Result(0, "fake/repo\n", "")
        )
        try:
            detected = _detect_repo()
        except BaseException as e:
            detected = f"ошибка: {type(e).__name__}"
        check("detect-repo-uses-patched-subprocess", detected == "fake/repo")
        globals()["REPO"] = original_repo
        globals()["subprocess"] = original_subprocess

        # Пустое/пробельное значение переменной окружения обязано быть
        # неотличимо от отсутствующего, иначе gh зовётся с `--repo " "`.
        check(
            "blank-repo-env-is-treated-as-absent",
            _repo_from_env({}) == ""
            and _repo_from_env({"REPO": ""}) == ""
            and _repo_from_env({"REPO": "   "}) == ""
            and _repo_from_env({"REPO": " owner/name \n"}) == "owner/name",
        )

        # --- Необработанный сбой закрывает поток событием ---
        def _boom():
            raise RuntimeError("bang")

        globals()["main"] = _boom
        start = len(captured)
        rc = run()
        lines = captured[start:]
        check(
            "fatal-exception-emits-final-event",
            rc == 1
            and any(line.startswith("WATCH_ERROR fatal: RuntimeError: bang")
                    for line in lines)
            and any(line.endswith("signal=EXCEPTION") for line in lines),
        )

        # Событие обязано называть МЕСТО падения: по одному имени класса
        # исключения место не найти, а полный traceback ломал бы контракт
        # «одна строка = одно событие».
        # Ожидание выводится из кода самой функции, а не пишется строкой:
        # зашитое «pr-watch.py» ломало кейс на любой копии файла (ровно это
        # и вышло на мутационном прогоне из /tmp) — провал был бы про имя
        # копии, а не про поведение.
        boom_site = " at {}:{}".format(
            os.path.basename(_boom.__code__.co_filename),
            _boom.__code__.co_firstlineno + 1,
        )
        check(
            "fatal-event-names-crash-site",
            any(line.startswith("WATCH_ERROR fatal:")
                and line.endswith(boom_site)
                for line in lines),
        )

        # Ctrl-C — не крах. Перехват BaseException выдавал его за баг
        # (`WATCH_ERROR fatal: KeyboardInterrupt`, код возврата 1) и гасил
        # штатное прерывание.
        def _interrupt():
            raise KeyboardInterrupt()

        globals()["main"] = _interrupt
        start = len(captured)
        propagated = False
        try:
            run()
        except KeyboardInterrupt:
            propagated = True
        except BaseException as e:
            propagated = f"подменено на {type(e).__name__}"
        lines = captured[start:]
        check(
            "fatal-guard-does-not-mask-keyboardinterrupt",
            propagated is True
            and any(line.endswith("signal=SIGINT") for line in lines)
            and not any("signal=EXCEPTION" in line for line in lines)
            and not any(line.startswith("WATCH_ERROR fatal:")
                        for line in lines),
        )

        # Штатный выход по сигналу НЕ должен превращаться во второе, ложное
        # WATCH_STOPPED: handle_signal своё уже напечатал.
        def _exiter():
            sys.exit(0)

        globals()["main"] = _exiter
        start = len(captured)
        passed_through = False
        try:
            run()
        except SystemExit:
            passed_through = True
        check(
            "fatal-guard-passes-systemexit",
            passed_through
            and not any("signal=EXCEPTION" in line
                        for line in captured[start:]),
        )
        globals()["main"] = original_main

        # Сигнальный путь на неопределившемся REPO печатал `repo= signal=…`:
        # пустой токен ломает разбор строки по парам key=value. Нужен
        # настоящий signal — подменённый отдаёт из Signals() число без .name.
        globals()["signal"] = original_signal
        globals()["REPO"] = ""
        start = len(captured)
        try:
            handle_signal(original_signal.SIGTERM, None)
        except SystemExit:
            pass
        check(
            "signal-event-normalizes-empty-repo",
            captured[start:] == ["WATCH_STOPPED repo=(unknown) signal=SIGTERM"],
        )

        # Заглушка сигналов сверяется с НАСТОЯЩИМ модулем, а не сама с
        # собой: расхождение интерфейса — тихая порча всех кейсов, которые
        # через неё идут. Ground truth здесь доступен даром, поэтому
        # верность двойника проверяется, а не декларируется.
        def _stub_signal_name(value):
            # Доступ обёрнут намеренно: без обёртки заглушка, вернувшая
            # голое число, поднимает AttributeError прямо в выражении
            # check() — провал выходит безымянным и обрывает остальные
            # кейсы вместо того, чтобы назвать себя.
            try:
                return _FakeSignal.Signals(value).name
            except Exception as e:
                return f"нет .name ({type(e).__name__})"

        check(
            "fake-signal-mimics-real-interface",
            _stub_signal_name(_FakeSignal.SIGTERM)
            == original_signal.Signals(original_signal.SIGTERM).name
            and _stub_signal_name(_FakeSignal.SIGINT)
            == original_signal.Signals(original_signal.SIGINT).name,
        )

        # Обработчик — последнее, что печатает событие. Неизвестный номер
        # сигнала не должен превращать финальное событие в исключение:
        # тогда поток оборвался бы молча, ровно тот класс, что закрывался
        # всем раундом.
        globals()["REPO"] = "owner/name"
        start = len(captured)
        raised = None
        try:
            handle_signal(9999, None)
        except SystemExit:
            pass
        except BaseException as e:
            raised = type(e).__name__
        check(
            "signal-event-survives-unknown-signal-number",
            raised is None
            and captured[start:] == ["WATCH_STOPPED repo=owner/name signal=SIG9999"],
        )
        globals()["REPO"] = original_repo
    except Exception as e:
        results.append((f"unexpected: {type(e).__name__}: {e}", False))
    finally:
        globals()["emit"] = original_emit
        globals()["gh_json"] = original_gh_json
        globals()["subprocess"] = original_subprocess
        globals()["_review_field_supported"] = original_review_field
        globals()["signal"] = original_signal
        globals()["time"] = original_time
        globals()["REPO"] = original_repo
        globals()["main"] = original_main
        _clear_warning_sets()

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
    sys.exit(run())
