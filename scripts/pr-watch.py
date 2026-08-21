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

Коды выхода:
  0   — штатное завершение main(), а также SIGTERM/SIGINT, пришедшие в
        установленный обработчик: handle_signal печатает WATCH_STOPPED и
        выходит через sys.exit(0).
  1   — необработанный сбой. Перед выходом печатаются WATCH_ERROR и
        WATCH_STOPPED signal=EXCEPTION.
  130 — SIGINT, пришедший в узкое окно ДО установки обработчиков (первые
        строки main()). Там сигнал приходит исключением; KeyboardInterrupt
        пробрасывается, и код выхода даёт интерпретатор. Ранние версии
        печатали здесь WATCH_ERROR и возвращали 1 — единица врала и
        оболочке, и потребителю событий (разбор в комментарии run()).
"""
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone


def _repo_from_remote_url(url):
    """Извлечь owner/name из адреса git-remote без подпроцессов и сети.

    Поддерживает формы github.com:
      git@github.com:owner/repo.git   — SCP-like (с .git и без)
      ssh://git@github.com/owner/repo.git
      https://github.com/owner/repo.git  (http://, с логином)
      git://github.com/owner/repo.git

    Регистр хоста и порт игнорируются: GitHub.com, github.com:443 и
    ssh://git@GitHub.com:22/owner/repo.git считаются одним и тем же хостом.
    Хост, отличный от github.com (GitHub Enterprise, GitLab и т.п.),
    → пустая строка. Мусорный ввод → пустая строка, без исключений.
    """
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    # SCP-like: git@github.com:owner/repo.git
    m = re.match(r'^git@([^:]+):(.+)$', url)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # URL-like: scheme://[user@]host[:port]/path
        m = re.match(r'^(?:https?|git|ssh)://(?:[^@/]+@)?([^/]+)/(.+)$', url)
        if not m:
            return ""
        host, path = m.group(1), m.group(2)
    # Обе ветки разбора сходятся здесь: регистр и порт отбрасываются один раз.
    host = host.lower().split(":")[0]
    if host != "github.com":
        return ""
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not _repo_is_valid(path):
        return ""
    return path


def _detect_repo():
    """Auto-detect <owner>/<repo> сначала из адреса git-remote, затем через gh.

    Прежний единственный путь — `gh repo view --json nameWithOwner` — GraphQL:
    при исчерпанном лимите наблюдатель падал на старте, до первой строки
    stdout. Адрес origin лежит на диске, сеть и лимит не нужны. gh остаётся
    запасным путём для сред, где origin не github.com, а gh настроен.

    Оба пути ходят через ГЛОБАЛЬНЫЙ subprocess, а не через локальный импорт:
    self-test подменяет глобал, и локальный импорт обошёл бы подмену.
    """
    try:
        out = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            parsed = _repo_from_remote_url(out.stdout.strip())
            if parsed:
                return parsed
    except Exception:
        pass
    try:
        out = subprocess.run(
            ['gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    raise RuntimeError(
        "Cannot detect repo: set REPO env var, ensure origin is a github.com"
        " remote, or run inside a gh-authorized git repo."
    )


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

    Зовётся из main(), а не на импорте. Прежде окружение читалось при
    импорте, и политика чтения REPO была двойной: переменная — рано,
    авто-определение — поздно. Значение, выставленное после импорта
    (обёртка, которая импортирует модуль и потом зовёт main()), молча
    игнорировалось, а комментарий про ленивость расходился с кодом.
    Теперь оба источника читаются в одной точке.
    """
    source = os.environ if env is None else env
    return (source.get("REPO") or "").strip()


def _repo_is_valid(value):
    """Форма `owner/name` — проверяется один раз на старте, а не на опросе.

    Кривое значение (лишний пробел внутри, потерянный слэш, обрезанная
    половина) не ломает вызов gh как таковой — `--repo` съедает его как
    значение флага, инъекции здесь нет. Ломается другое: gh отвечает
    отказом на КАЖДЫЙ опрос, и watcher живёт, ровно раз в INTERVAL печатая
    невнятный WATCH_ERROR. Постоянная слепота вместо громкого отказа —
    тот же класс, что и пробельный REPO выше.

    Набор символов намеренно широкий: GitHub допускает точку в начале
    (`.github` — очень частое имя), поэтому проверяется только алфавит и
    наличие обеих половин. Более узкое правило отвергало бы существующие
    репозитории, а отказ стартовать на живом имени хуже невнятной ошибки.
    """
    owner, sep, name = value.partition("/")
    if not sep or not owner or not name:
        return False
    allowed = set(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789._-"
    )
    return all(ch in allowed for ch in owner + name)


class _EnvValueError(ValueError):
    """Числовая переменная окружения не разобралась.

    Выделенное исключение, а не sys.exit, намеренно: разбор обязан быть
    проверяемым без убийства собственного процесса — иначе кейс самопроверки
    на разбор валил бы интерпретатор вместо того, чтобы поймать отказ.
    Убивает процесс не разбор, а обёртка уровня модуля.
    """


def _env_positive_int(name, default):
    """Целое ≥ 1 из окружения, иначе _EnvValueError.

    Принимается ТОЛЬКО десятичное целое ≥ 1 после strip(). Всё остальное —
    пустая строка, `abc`, `30s`, `3.5`, `0`, `-1`, `0x10`, `1_0` —
    отвергается. int() здесь не годится: он глотает `1_0` как 10, знак `+`,
    юникод-цифры и окружающие пробелы — то есть тихо принимал бы значения,
    которых оператор не задавал. Поэтому форма проверяется явным
    `[0-9]+` по ASCII, и лишь потом переводится в число.

    Верхнего предела нет намеренно: большой интервал — законный выбор
    оператора, а выдуманный потолок менял бы смысл настройки.
    """
    cleaned = os.environ.get(name, default).strip()
    if not re.fullmatch(r"[0-9]+", cleaned):
        raise _EnvValueError("ожидается целое число ≥ 1")
    value = int(cleaned)
    if value < 1:
        raise _EnvValueError("ожидается целое число ≥ 1")
    return value


def _watch_fatal_line(name, raw, reason):
    """Одна строка отказа: значение как задано, но без права разорвать поток.

    Значение печатается как задал оператор, но перевод строки внутри него
    заменяется пробелом (иначе одна кривая переменная расклеила бы строку
    события на две у потребителя), а длина обрезается до 80 символов —
    диагностики хватает, а забитый мусором поток нет.
    """
    shown = str(raw).replace("\n", " ").replace("\r", " ")
    if len(shown) > 80:
        shown = shown[:80]
    return f"WATCH_FATAL {name}={shown} — {reason}"


def _load_positive_int_or_die(name, default):
    """Разбор переменной на уровне модуля: отказ громкий и в поток потребителя.

    Отказ уходит в fd 1 — тот же дескриптор, куда наблюдатель пишет свои
    события, — и процесс завершается кодом 2. Никакой тихой подстановки
    значения по умолчанию: оператор задал настройку, наблюдатель её не
    понял — это отказ, а не повод молча решить за него. Тихая подстановка
    дала бы ровно ту слепоту, от которой уходит этот файл: наблюдатель, не
    делающий того, что просили, но внешне живой.
    """
    try:
        return _env_positive_int(name, default)
    except _EnvValueError as exc:
        raw = os.environ.get(name, default)
        os.write(
            1,
            (_watch_fatal_line(name, raw, str(exc)) + "\n").encode(
                "utf-8", "replace"
            ),
        )
        sys.exit(2)


REPO = "selftest/selftest" if "--self-test" in sys.argv else ""
INTERVAL = _load_positive_int_or_die("INTERVAL", "30")
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
_GH_TIMEOUT = _load_positive_int_or_die("GH_TIMEOUT", "20")
_GH_TIMEOUT_PAGINATED = _load_positive_int_or_die("GH_TIMEOUT_PAGINATED", "120")
_PR_FIELDS = "number,title,labels,reviewDecision"
_PR_FIELDS_NO_REVIEW = "number,title,labels"
_UNKNOWN_FIELD_STDERR = "unknown json field"
_review_field_supported = True
# Формат финального события обработчика сигнала вынесен в константу, чтобы
# кейс длины строки строил её ТЕМ ЖЕ форматом, что и handle_signal, а не
# независимым литералом: смена формата обязана валить кейс, а не расходиться
# с ним молча.
_STOP_EVENT_FMT = "WATCH_STOPPED repo={repo} signal={sig}"


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


def _emit_raw(line):
    """Событие одним системным вызовом, мимо буфера Python.

    Нужно ровно одному месту — обработчику сигнала. print() туда не
    годится, и это не теория: сигнал приходит между байткодами того же
    потока, и если он застал основной поток внутри записи в stdout, то
    вложенный print падает с `RuntimeError: reentrant call inside
    <_io.BufferedWriter>` — CPython защищает буфер от повторного входа.
    Замер на 8000 доставках, пока основной поток непрерывно печатал: 6485
    (81%) обращений в обработчике падали именно так. Результат для
    наблюдателя — штатная остановка по SIGTERM, доложенная как аварийное
    завершение: WATCH_STOPPED не печатается, а RuntimeError всплывает в
    основном потоке и уходит в фатальную ветку run().

    os.write в буфер не заходит и блокировку не берёт, поэтому этот путь
    не зависит от того, чем занят stdout. Исключения гасятся: обработчик
    печатает ПОСЛЕДНЕЕ событие, и падать ему негде и незачем.

    Цена размена названа прямо: строка уходит в fd 1 раньше того, что уже
    лежит в буфере, и прерванное событие допечатается интерпретатором
    ПОСЛЕ WATCH_STOPPED. Порядок нарушается, строки — нет; но держится это
    «нет» не на одной PIPE_BUF, и разница существенна.

    Что гарантирует POSIX: запись в канал длиной до PIPE_BUF атомарна —
    САМА строка события не приедет разорванной. Значение платформенное:
    512 байт на macOS (это же минимум, гарантированный POSIX) и 4096 на
    Linux; расчёт ведётся по МЕНЬШЕМУ, потому что файл раскатывается
    побайтово на обе платформы. Потолок продового формата
    `WATCH_STOPPED repo=<owner/name> signal=<NAME>` — около 180 байт.
    Помещаемость держится кейсом emit-raw-line-fits-smallest-pipe-buf,
    единственность вызова — emit-raw-has-single-production-call-site.

    Чего POSIX не гарантирует: целостности СОСЕДНЕЙ строки, внутрь которой
    событие вклинится, если её запись прервана незавершённой. Ревью
    предлагало лечить это проверкой типа fd 1 и уходом в stderr, когда
    stdout не канал. Замер отвергает по двум пунктам. Тип дескриптора
    здесь не разделитель: fpathconf(1, PC_PIPE_BUF) на macOS даёт 512 и
    для канала, и для обычного файла. А разрыв соседней строки
    воспроизводится не превышением PIPE_BUF, а превышением ЁМКОСТИ канала:
    строка в 200000 байт разорвалась ровно на 131072-м, тогда как 800 и
    4000 байт на забитом канале не разорвались ни разу. Строк такого
    порядка в этом потоке нет — самая длинная содержит тело комментария,
    обрезанное до 200 символов. Цена же предложенного лечения реальна:
    ПОСЛЕДНЕЕ событие уходило бы в stderr ровно тогда, когда потребитель
    пишет stdout в файл, то есть пропадало бы из ленты, ради которой эта
    функция и написана. Доставка в fd 1 при stdout-файле держится кейсом
    emit-raw-writes-to-fd1-when-stdout-is-regular-file.

    Дескриптор 1 записан числом намеренно. Ревью предлагало
    sys.__stdout__.fileno(); замер отвергает по трём пунктам. Значение то
    же — исходный поток интерпретатора по построению сидит на fd 1.
    Защиты не добавляет: после os.close(1) вызов всё равно вернул 1, то
    есть закрытый дескриптор он не замечает. А платить пришлось бы новым
    отказом: sys.__stdout__ по документации бывает None, и тогда
    AttributeError гасится except ниже — ПОСЛЕДНЕЕ событие пропадает
    молча, ровно тот исход, против которого эта функция и написана.
    Держится кейсом emit-raw-survives-none-original-stdout.

    Провал самой записи больше не молчит. Замер: при закрытом fd 1 вызов
    падает с OSError EBADF, а fd 2 в тот же момент жив и принимает
    строку. Раньше событие здесь исчезало бесследно, хотя единственный
    оставшийся канал работал. Теперь в fd 2 уходит причина и САМО
    событие: оператор в логе видит не факт потери, а потерянное
    содержимое. Граница названа прямо — это НЕ доставка потребителю, он
    читает stdout, и для него событие всё равно потеряно; это сохранение
    улики в выжившем канале. Держится кейсом
    emit-raw-falls-back-to-stderr-when-fd1-broken.
    """
    try:
        os.write(1, (line + "\n").encode("utf-8", "replace"))
    except Exception as exc:
        try:
            os.write(
                2,
                ("PR_WATCH_EMIT_FALLBACK %s: %s | %s\n"
                 % (type(exc).__name__, exc, line)).encode("utf-8", "replace"),
            )
        except Exception:
            pass


def _force_utf8_stdout():
    """Событие не должно зависеть от локали процесса.

    print() кодирует строку кодировкой sys.stdout, а она берётся из локали.
    Штатные предохранители Python обычно спасают: под LC_ALL=C включается
    UTF-8-режим (замер: utf8_mode=1), и кириллица печатается. Но их
    снимают одной переменной окружения, и тогда кодировка становится
    ascii. Замер при PYTHONUTF8=0 и LC_ALL=C: print русского заголовка PR
    падает с UnicodeEncodeError, исключение уходит в фатальную ветку
    run(), и watcher умирает на первом же таком PR — а заголовки в парке
    русские. _emit_raw кодирует utf-8 явно; здесь то же самое для
    обычного пути, чтобы контракт «строка = событие» не зависел от среды.
    errors="replace": нечитаемый символ не имеет права обрывать поток
    событий. Держится кейсом run-forces-utf8-stdout.

    Перехват остаётся широким намеренно — сужение до AttributeError
    (рекомендация ревью) отвергнуто: вызов _force_utf8_stdout() стоит ДО
    try в run(), поэтому любое непредусмотренное исключение отсюда уйдёт
    голым трейсбеком, без единого события — ровно тот класс отказа, против
    которого написан весь файл.

    Прежний `except Exception: pass` был дефектом: отказ молчал, а его
    цена названа в этом же docstring — смерть watcher-а на первом же
    русском заголовке PR. Теперь причина уходит в fd 2 тем же приёмом,
    что в _emit_raw. Держится кейсом utf8-failure-reports-cause-to-fd2.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception as exc:
        try:
            os.write(
                2,
                ("PR_WATCH_UTF8_FAILED %s: %s\n"
                 % (type(exc).__name__, exc)).encode("utf-8", "replace"),
            )
        except Exception:
            pass


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
    # _emit_raw, а не emit: вложенный print падает, если сигнал застал
    # основной поток внутри записи в stdout (см. _emit_raw). Формат берётся
    # из _STOP_EVENT_FMT — тот же, которым кейс длины проверяет помещаемость
    # строки в PIPE_BUF.
    _emit_raw(_STOP_EVENT_FMT.format(repo=REPO or "(unknown)", sig=name))
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
    # событием, а не падением до первой строки stdout. Оба источника —
    # переменная окружения и авто-определение — читаются в ОДНОЙ точке:
    # раздвоенная политика (переменная на импорте, детект в main) делала
    # поведение зависимым от момента импорта.
    global REPO
    if not REPO:
        REPO = _repo_from_env()
    if not REPO:
        try:
            REPO = _detect_repo()
        except Exception as e:
            emit(f"WATCH_ERROR init: {e}")
            emit("WATCH_STOPPED repo=(unknown) signal=NO_REPO")
            return 1

    # Форма проверяется один раз здесь, а не на каждом опросе: кривое имя
    # иначе даёт бесконечную ленту одинаковых WATCH_ERROR вместо отказа.
    # В событии печатается `(unknown)`, а не само значение: пробел внутри
    # разорвал бы строку события на пары key=value у потребителя.
    if not _repo_is_valid(REPO):
        emit(f"WATCH_ERROR init: некорректный REPO {REPO!r}, ожидается owner/name")
        emit("WATCH_STOPPED repo=(unknown) signal=BAD_REPO")
        return 1

    # pr_number -> {"last_comment_key": (datetime, (kind, value)) | None,
    #               "ci": str, "labels": str, "review": str}
    # Второй элемент ключа — разряд из _norm_id_key, а не голый int.
    # None означает «bootstrap комментариев не удался»: на первом успешном
    # опросе watermark перенимается без эмита.
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
                    wm = max(
                        (comment_key(c) for c in cs),
                        default=(_EPOCH, _norm_id_key(0)),
                    )
                except GhError as e:
                    emit(f"WATCH_ERROR comments #{n}: {e}")
                    # Watermark неизвестен. На первом успешном опросе он будет
                    # перенят без эмита. Цена: комментарии, пришедшие внутри
                    # окна сбоя, будут пропущены один раз — осознанный размен:
                    # он строго лучше повтора всей истории (та же семантика,
                    # что принята при начальном bootstrap без ложных PR_OPENED).
                    wm = None
                known[n] = {
                    "last_comment_key": wm,
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
                if last is None:
                    # Первый успешный опрос после сбоя bootstrap нового PR:
                    # перенимаем текущий максимум без эмита — история не
                    # повторяется. Комментарии, пришедшие в окне сбоя,
                    # пропускаются один раз (названная цена).
                    state["last_comment_key"] = max(
                        (comment_key(c) for c in cs),
                        default=(_EPOCH, _norm_id_key(0)),
                    )
                else:
                    new = [c for c in cs if comment_key(c) > last]
                    if new:
                        # cs уже отсортирован comment_key'ем в list_comments.
                        for c in new:
                            # Безопасное извлечение: KeyError здесь не GhError
                            # и пролетел бы мимо except ниже, уронив watcher.
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
                        state["last_comment_key"] = max(
                            comment_key(c) for c in cs
                        )
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
    _force_utf8_stdout()
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
        # Печать здесь идёт обычным emit, и это осознанно. Блок except
        # выполняется ПОСЛЕ раскрутки стека: запись в буфер уже вышла с
        # ошибкой, замок отпущен, реентрантного срыва тут нет — в отличие
        # от обработчика сигнала, который работает ВЛОЖЕННО, пока замок
        # держится (см. _emit_raw). Замер настоящим Ctrl-C: 995 прерываний
        # внутри незавершённой записи, 995 напечатано, 0 срывов. Плюс
        # порядок: emit кладёт событие ПОСЛЕ прерванного, а os.write
        # обогнал бы его. Держится кейсами except-*-emits-after-unwound-write.
        emit(f"WATCH_STOPPED repo={REPO or '(unknown)'} signal=SIGINT")
        raise
    except Exception as e:
        # Тот же разбор, что и веткой выше: здесь стек уже раскручен.
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
    original_emit_raw = globals()["_emit_raw"]
    original_repo_from_env = globals()["_repo_from_env"]
    original_detect_repo = globals()["_detect_repo"]

    def check(name, condition):
        results.append((name, bool(condition)))

    try:
        globals()["emit"] = captured.append
        globals()["_emit_raw"] = captured.append
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

        # --- Bootstrap нового PR: отказ list_comments громкий, история не повторяется ---
        # Раньше except GhError молчал и ставил watermark в (_EPOCH, _norm_id_key(0)):
        # на следующем опросе ВСЯ история PR уезжала потребителю как новые события.

        class _FakeTimeCounter:
            """Заменяет _FakeTime: позволяет N итераций до остановки."""

            def __init__(self, n):
                self._remaining = n

            def sleep(self, _):
                if self._remaining <= 0:
                    raise _StopLoop()
                self._remaining -= 1

        def run_iterations(gh_func, max_sleeps=0):
            """Бутстрап на пустом списке, затем (max_sleeps + 1) итераций."""
            _clear_warning_sets()
            globals()["gh_json"] = gh_func
            globals()["signal"] = _FakeSignal
            globals()["time"] = _FakeTimeCounter(max_sleeps)
            start = len(captured)
            try:
                main()
            except _StopLoop:
                pass
            return captured[start:]

        _OLD_C = {"id": 100, "created_at": "2024-01-01T00:00:00Z",
                  "body": "old comment", "user": {"login": "alice"}}
        _NEW_C = {"id": 200, "created_at": "2025-06-01T00:00:00Z",
                  "body": "new comment", "user": {"login": "bob"}}

        # Кейс 1: новый PR + GhError при чтении комментариев →
        # поток несёт PR_OPENED И WATCH_ERROR (отказ громкий, а не молчание).
        _pr_k1 = [0]
        _api_k1 = [0]

        def gh_k1(args):
            if args[0] == "pr" and args[1] == "list":
                _pr_k1[0] += 1
                if _pr_k1[0] == 1:
                    return []
                return [{"number": 5, "title": "test-pr", "labels": []}]
            if args[0] == "api":
                _api_k1[0] += 1
                if _api_k1[0] == 1:
                    raise GhError("network timeout")
                return []
            return []

        lines_k1 = run_iterations(gh_k1)
        check("new-pr-comment-error-emits-watch-error",
              any(line == "PR_OPENED #5 | test-pr" for line in lines_k1)
              and any(line.startswith("WATCH_ERROR comments #5:")
                      for line in lines_k1))

        # Кейс 2: после двойного сбоя bootstrap (new-PR-block + comment-poll)
        # следующий успешный опрос НЕ воспроизводит историю комментариев.
        _pr_k2 = [0]
        _api_k2 = [0]

        def gh_k2(args):
            if args[0] == "pr" and args[1] == "list":
                _pr_k2[0] += 1
                if _pr_k2[0] == 1:
                    return []
                return [{"number": 6, "title": "test-pr2", "labels": []}]
            if args[0] == "api":
                _api_k2[0] += 1
                # Итерация 1: оба list_comments падают (new-PR-block и
                # comment-poll — по одному вызову issues/pulls каждый, но
                # GhError на issues обрывает pulls; итого два api-вызова).
                if _api_k2[0] <= 2:
                    raise GhError("timeout")
                # Итерация 2: возвращаем старый комментарий.
                if "issues" in args[-1]:
                    return [_OLD_C]
                return []
            return []

        lines_k2 = run_iterations(gh_k2, max_sleeps=1)
        check("new-pr-comment-error-no-history-replay",
              not any(line.startswith("PR_COMMENT #6") for line in lines_k2))

        # Кейс 3: после перенятия watermark новые комментарии доходят.
        # Итерация 1: оба list_comments падают — watermark остаётся None.
        # Итерация 2: успешный опрос → watermark перенят без эмита.
        # Итерация 3: новый комментарий → PR_COMMENT эмитится.
        _pr_k3 = [0]
        _api_k3 = [0]

        def gh_k3(args):
            if args[0] == "pr" and args[1] == "list":
                _pr_k3[0] += 1
                if _pr_k3[0] == 1:
                    return []
                return [{"number": 7, "title": "test-pr3", "labels": []}]
            if args[0] == "api":
                _api_k3[0] += 1
                if _api_k3[0] <= 2:
                    raise GhError("timeout")
                # Итерация 2 (вызовы 3, 4): старый комментарий.
                if _api_k3[0] == 3:
                    return [_OLD_C]
                if _api_k3[0] == 4:
                    return []
                # Итерация 3 (вызовы 5, 6): добавился новый.
                if _api_k3[0] == 5:
                    return [_OLD_C, _NEW_C]
                return []
            return []

        lines_k3 = run_iterations(gh_k3, max_sleeps=2)
        check("new-pr-comment-error-new-comment-arrives",
              any(line.startswith("PR_COMMENT #7") and "bob" in line
                  for line in lines_k3))

        # Кейс 4 (регресс): при успешном bootstrap нового PR прежнее поведение
        # не изменилось — старые комментарии не эмитятся как новые.
        _pr_k4 = [0]

        def gh_k4(args):
            if args[0] == "pr" and args[1] == "list":
                _pr_k4[0] += 1
                if _pr_k4[0] == 1:
                    return []
                return [{"number": 8, "title": "test-pr4", "labels": []}]
            if args[0] == "api":
                if "issues" in args[-1]:
                    return [_OLD_C]
                return []
            return []

        lines_k4 = run_iterations(gh_k4, max_sleeps=0)
        check("new-pr-success-no-old-comments-emitted",
              not any(line.startswith("PR_COMMENT #8") for line in lines_k4))

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

        # --- Определение репозитория предпочитает git remote (без GraphQL) ---
        # Если git remote get-url origin возвращает адрес github.com — gh
        # не должен вызываться вообще: ни сети, ни лимита.
        _git_cmds = []

        class _GitPreferSubprocess:
            def run(self, args, **_kw):
                _git_cmds.append(list(args))
                if args[0] == 'git':
                    return _Result(
                        0,
                        "git@github.com:einarluha-a11y/codex-for-code.git\n",
                        "",
                    )
                return _Result(1, "", "gh: not called")

        globals()["subprocess"] = _GitPreferSubprocess()
        _git_detected = None
        try:
            _git_detected = _detect_repo()
        except BaseException as _e:
            _git_detected = f"ошибка: {type(_e).__name__}"
        _gh_not_called = not any(c[0] == 'gh' for c in _git_cmds)
        check(
            "detect-repo-prefers-git-remote",
            _git_detected == "einarluha-a11y/codex-for-code" and _gh_not_called,
        )
        globals()["subprocess"] = original_subprocess

        # _repo_from_remote_url разбирает все допустимые формы адреса github.com
        # и отвергает всё остальное.
        check(
            "detect-repo-parses-url-forms",
            _repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("git@github.com:owner/repo") == "owner/repo"
            and _repo_from_remote_url("ssh://git@github.com/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("https://github.com/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("http://github.com/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("https://user@github.com/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("git://github.com/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("https://github.example.com/o/r.git") == ""
            and _repo_from_remote_url("") == ""
            and _repo_from_remote_url("ерунда") == "",
        )

        # Хост сравнивается без учёта регистра и порта: GitHub.com и
        # github.com:443 эквивалентны; github.com.evil.example — нет.
        check(
            "detect-repo-normalizes-host",
            _repo_from_remote_url("https://GitHub.com/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("https://github.com:443/owner/repo") == "owner/repo"
            and _repo_from_remote_url("ssh://git@GitHub.com:22/owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("git@GitHub.com:owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"
            and _repo_from_remote_url("https://gitlab.com/owner/repo.git") == ""
            and _repo_from_remote_url("https://github.com.evil.example/owner/repo") == "",
        )

        # git remote недоступен — функция обязана откатиться на gh.
        class _GitFailGhSuccessSubprocess:
            def run(self, args, **_kw):
                if args[0] == 'git':
                    return _Result(1, "", "fatal: no remote 'origin'")
                return _Result(0, "fallback/repo\n", "")

        globals()["subprocess"] = _GitFailGhSuccessSubprocess()
        _fb_detected = None
        try:
            _fb_detected = _detect_repo()
        except BaseException as _e:
            _fb_detected = f"ошибка: {type(_e).__name__}"
        check("detect-repo-falls-back-to-gh", _fb_detected == "fallback/repo")
        globals()["subprocess"] = original_subprocess

        # Оба источника недоступны — RuntimeError.
        class _BothFailSubprocess:
            def run(self, args, **_kw):
                return _Result(1, "", "error")

        globals()["subprocess"] = _BothFailSubprocess()
        _both_raised = None
        try:
            _detect_repo()
        except RuntimeError as _e:
            _both_raised = _e
        except BaseException:
            pass
        check("detect-repo-raises-when-both-fail", _both_raised is not None)
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

        # Обработчик печатает событие, даже когда stdout занят. Здесь
        # работает НАСТОЯЩИЙ guard CPython, а не рукодельный двойник:
        # запись в буферизованный stdout, внутри которой происходит
        # вложенная печать, поднимает RuntimeError о повторном входе —
        # ровно то, что делает сигнал, пришедший внутрь emit(). Двойник
        # такого класса врал бы (см. fake-signal-mimics-real-interface),
        # поэтому механизм берётся живой, а событие ловится с fd 1.
        globals()["_emit_raw"] = original_emit_raw
        globals()["signal"] = original_signal
        globals()["REPO"] = "owner/name"
        probe = {}

        class _BusyStdout(io.RawIOBase):
            def writable(self):
                return True

            def write(self, chunk):
                # Один раз: иначе поздний сброс буфера при сборке мусора
                # позовёт обработчик второй раз уже после проверки.
                if probe.get("fired"):
                    return len(chunk)
                probe["fired"] = True
                try:
                    print("вложенное событие", flush=True)
                    probe["nested"] = "печать удалась"
                except Exception as e:
                    probe["nested"] = type(e).__name__
                handle_signal(original_signal.SIGTERM, None)
                return len(chunk)

        saved_fd = os.dup(1)
        read_fd, write_fd = os.pipe()
        real_stdout = sys.stdout
        os.dup2(write_fd, 1)
        try:
            sys.stdout = io.TextIOWrapper(
                io.BufferedWriter(_BusyStdout()), encoding="utf-8", errors="replace"
            )
            try:
                print("основное событие", flush=True)
            except SystemExit:
                probe["exited"] = True
            except BaseException as e:
                probe["exited"] = type(e).__name__
        finally:
            sys.stdout = real_stdout
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            os.close(write_fd)
        payload = os.read(read_fd, 4096).decode("utf-8", "replace")
        os.close(read_fd)
        check(
            "signal-event-survives-busy-stdout",
            probe.get("nested") == "RuntimeError"
            and probe.get("exited") is True
            and payload == "WATCH_STOPPED repo=owner/name signal=SIGTERM\n",
        )
        globals()["_emit_raw"] = captured.append

        # Отказ от sys.__stdout__.fileno() в _emit_raw закреплён машинно.
        # Предложенная ревью форма при sys.__stdout__ is None подняла бы
        # AttributeError, тот погасился бы внутренним except — и ПОСЛЕДНЕЕ
        # событие пропало бы молча. Работает настоящий _emit_raw, приём
        # идёт с fd 1: проверяется доставка, а не отсутствие падения.
        globals()["_emit_raw"] = original_emit_raw
        saved_fd = os.dup(1)
        read_fd, write_fd = os.pipe()
        saved_original_stdout = sys.__stdout__
        os.dup2(write_fd, 1)
        try:
            sys.__stdout__ = None
            _emit_raw("WATCH_STOPPED repo=owner/name signal=SIGTERM")
        finally:
            sys.__stdout__ = saved_original_stdout
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            os.close(write_fd)
        payload = os.read(read_fd, 4096).decode("utf-8", "replace")
        os.close(read_fd)
        check(
            "emit-raw-survives-none-original-stdout",
            payload == "WATCH_STOPPED repo=owner/name signal=SIGTERM\n",
        )
        globals()["_emit_raw"] = captured.append

        # Ревью настаивало: при stdout-файле _emit_raw обязан уходить в
        # stderr, «потому что PIPE_BUF — про каналы». Тип дескриптора
        # разделителем не является (замер в docstring _emit_raw), а
        # предложенное лечение унесло бы ПОСЛЕДНЕЕ событие из ленты, которую
        # читает потребитель, ровно в сценарии `pr-watch.py > log`. Кейс
        # закрепляет обратное: stdout — обычный файл, событие в этом файле.
        globals()["_emit_raw"] = original_emit_raw
        import tempfile
        saved_fd = os.dup(1)
        tmp = tempfile.NamedTemporaryFile(prefix="pr-watch-fd1-", delete=False)
        try:
            os.dup2(tmp.fileno(), 1)
            _emit_raw("WATCH_STOPPED repo=owner/name signal=SIGTERM")
        finally:
            os.dup2(saved_fd, 1)
            os.close(saved_fd)
            tmp.close()
        with open(tmp.name, "rb") as fh:
            payload = fh.read().decode("utf-8", "replace")
        os.unlink(tmp.name)
        check(
            "emit-raw-writes-to-fd1-when-stdout-is-regular-file",
            payload == "WATCH_STOPPED repo=owner/name signal=SIGTERM\n",
        )
        globals()["_emit_raw"] = captured.append

        # Провал самой os.write(1, …) не имеет права быть молчаливым:
        # событие уходит в выживший fd 2 вместе с причиной. Работает
        # настоящий _emit_raw, fd 1 закрыт по-настоящему — приём идёт с
        # fd 2. Порядок операций важен: ВСЕ дескрипторы берутся ДО
        # закрытия fd 1, иначе os.pipe() займёт освободившийся номер.
        globals()["_emit_raw"] = original_emit_raw
        saved_out_fd = os.dup(1)
        read_err_fd, write_err_fd = os.pipe()
        saved_err_fd = os.dup(2)
        os.dup2(write_err_fd, 2)
        os.close(1)
        try:
            _emit_raw("WATCH_STOPPED repo=owner/name signal=SIGTERM")
        finally:
            os.dup2(saved_out_fd, 1)
            os.close(saved_out_fd)
            os.dup2(saved_err_fd, 2)
            os.close(saved_err_fd)
            os.close(write_err_fd)
        payload = os.read(read_err_fd, 4096).decode("utf-8", "replace")
        os.close(read_err_fd)
        check(
            "emit-raw-falls-back-to-stderr-when-fd1-broken",
            payload.startswith("PR_WATCH_EMIT_FALLBACK ")
            and "OSError" in payload
            and payload.rstrip("\n").endswith(
                "| WATCH_STOPPED repo=owner/name signal=SIGTERM"
            ),
        )
        globals()["_emit_raw"] = captured.append

        # Единственный продовый вызов _emit_raw — инвариант, на котором
        # стоит гарантия атомарности. Кейс доказывает его через AST по ВСЕМУ
        # файлу с вычетом тела self_test — прежний срез по "\ndef self_test("
        # разбирал только префикс, и вызов, добавленный ПОСЛЕ self_test,
        # ускользал бы от детекта (закреплено откатом C2).
        import ast as _ast
        with open(__file__, encoding="utf-8") as _fh:
            _file_src = _fh.read()
        _file_tree = _ast.parse(_file_src)
        # Диапазон строк self_test на уровне модуля — его тело из подсчёта
        # исключается (там подмены и заглушки, не продовые вызовы).
        _st_start = _st_end = None
        for _node in _ast.iter_child_nodes(_file_tree):
            if (isinstance(_node, _ast.FunctionDef)
                    and _node.name == "self_test"):
                _st_start, _st_end = _node.lineno, _node.end_lineno
                break
        _emit_raw_calls = [
            _node
            for _node in _ast.walk(_file_tree)
            if (isinstance(_node, _ast.Call)
                and isinstance(_node.func, _ast.Name)
                and _node.func.id == "_emit_raw"
                and not (_st_start is not None
                         and _st_start <= _node.lineno <= _st_end))
        ]
        _first_arg_ok = False
        if len(_emit_raw_calls) == 1:
            _arg0 = (_emit_raw_calls[0].args[0]
                     if _emit_raw_calls[0].args else None)
            if _arg0 is not None:
                try:
                    _arg_src = _ast.unparse(_arg0)  # Python 3.9+
                except AttributeError:
                    _arg_src = _ast.dump(_arg0)
                # После вынесения формата в _STOP_EVENT_FMT продовый вызов —
                # `_STOP_EVENT_FMT.format(...)`; принимаем и прямой литерал
                # WATCH_STOPPED на случай отката рефактора.
                #
                # Проверка идёт по НАЧАЛУ выражения, а не вхождением подстроки.
                # Вхождение пропускало бы чужое событие, внутри которого имя
                # формата упомянуто, — например
                # `_emit_raw(f"PR_COMMENT #1 {_STOP_EVENT_FMT}")`: единственный
                # вызов, подстрока на месте, а в fd 1 уходит тело комментария,
                # против чего инвариант и написан. Закреплено откатом 6.
                _stripped = _arg_src.lstrip("fF").lstrip("\"'")
                _first_arg_ok = (
                    _arg_src.startswith("_STOP_EVENT_FMT.format(")
                    or _stripped.startswith("WATCH_STOPPED")
                )
        check("emit-raw-has-single-production-call-site",
              len(_emit_raw_calls) == 1 and _first_arg_ok)

        # Худшая по длине строка события обязана помещаться в САМЫЙ тесный
        # PIPE_BUF (512 байт на macOS, минимум по POSIX) — иначе гарантия
        # атомарности записи из _emit_raw не держится на macOS. Строка
        # строится ТЕМ ЖЕ форматом, что и handle_signal (_STOP_EVENT_FMT),
        # поэтому смена формата валит этот кейс, а не расходится с ним молча.
        #
        # Длины взяты С ЗАПАСОМ к лимитам GitHub (owner 39, name 100), а не
        # по ним: лимит — чужая величина, она может вырасти молча, и кейс
        # остался бы зелёным, перестав описывать худший случай.
        #
        # Порог записан числом, а не os.fpathconf(1, "PC_PIPE_BUF"), как
        # предлагало ревью: fpathconf измерил бы stdout САМОГО прогона, то
        # есть в CI (ubuntu) вернул бы 4096 и ослабил бы приём ровно там,
        # где кейс исполняется, — при боевой платформе macOS с её 512.
        # 512 — минимум, гарантированный POSIX, ниже он не бывает нигде.
        worst = _STOP_EVENT_FMT.format(
            repo="o" * 200 + "/" + "n" * 200,
            sig="SIGTERM",  # самое длинное имя сигнала из устанавливаемых
        )
        check("emit-raw-line-fits-smallest-pipe-buf",
              len(worst.encode("utf-8")) < 512)

        # Ветки except в run() печатают обычным emit — и это верно. Кейсы
        # держат вывод машинно: исключение поднимается ИЗНУТРИ незавершённой
        # записи в буфер, раскручивает стек, и блок except печатает в тот же
        # буфер. Если бы класс совпадал с сигнальным, здесь был бы
        # RuntimeError о повторном входе; его нет. Событие ловится с fd 1,
        # поэтому проверяется и доставка, а не только отсутствие исключения.
        # _emit_raw при этом остаётся подменённым: подмена emit на него в
        # этих ветках уронит кейсы — выбор зафиксирован намеренно.
        globals()["emit"] = original_emit
        globals()["REPO"] = "owner/name"

        def _run_with_stdout(main_stub, encoding="utf-8", errors="replace"):
            """Прогон run() поверх подставного stdout.

            main_stub.error — исключение, которое поднимается ИЗНУТРИ
            первой записи в буфер; None означает «запись не ломать».
            encoding/errors задают обёртку: "ascii"/"strict" нужны, чтобы
            проверить, что run() сам переводит поток в utf-8.
            """
            state = {}

            class _StubRaw(io.RawIOBase):
                def writable(self):
                    return True

                def write(self, chunk):
                    err = state.get("error")
                    if err is not None and not state.get("fired"):
                        state["fired"] = True
                        raise err
                    os.write(1, chunk)
                    return len(chunk)

            state["error"] = main_stub.error
            globals()["main"] = main_stub
            saved_fd = os.dup(1)
            read_fd, write_fd = os.pipe()
            real_stdout = sys.stdout
            os.dup2(write_fd, 1)
            try:
                sys.stdout = io.TextIOWrapper(
                    io.BufferedWriter(_StubRaw()), encoding=encoding, errors=errors
                )
                try:
                    state["rc"] = run()
                except BaseException as e:
                    state["raised"] = type(e).__name__
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
            finally:
                sys.stdout = real_stdout
                os.dup2(saved_fd, 1)
                os.close(saved_fd)
                os.close(write_fd)
            state["payload"] = os.read(read_fd, 8192).decode("utf-8", "replace")
            os.close(read_fd)
            return state

        def _main_interrupted():
            print("основное событие", flush=True)
            return 0

        _main_interrupted.error = KeyboardInterrupt()
        ki = _run_with_stdout(_main_interrupted)
        check(
            "except-keyboardinterrupt-emits-after-unwound-write",
            ki.get("raised") == "KeyboardInterrupt"
            and "WATCH_STOPPED repo=owner/name signal=SIGINT\n" in ki["payload"],
        )

        def _main_crashed():
            print("основное событие", flush=True)
            return 0

        _main_crashed.error = ValueError("сбой записи")
        crash = _run_with_stdout(_main_crashed)
        check(
            "except-exception-emits-after-unwound-write",
            crash.get("raised") is None
            and crash.get("rc") == 1
            and "WATCH_ERROR fatal: ValueError: сбой записи" in crash["payload"]
            and "WATCH_STOPPED repo=owner/name signal=EXCEPTION\n" in crash["payload"],
        )

        # Поток намеренно создан в ascii — это воспроизводит среду, где
        # предохранители Python сняты. Без _force_utf8_stdout() print
        # русского заголовка падает с UnicodeEncodeError, run() уходит в
        # фатальную ветку, и вместо события приходит WATCH_ERROR.
        def _main_russian():
            print("PR_OPENED #1 | Русский заголовок", flush=True)
            return 0

        _main_russian.error = None
        utf8 = _run_with_stdout(_main_russian, encoding="ascii", errors="strict")
        check(
            "run-forces-utf8-stdout",
            utf8.get("raised") is None
            and utf8.get("rc") == 0
            and "PR_OPENED #1 | Русский заголовок" in utf8["payload"],
        )

        globals()["emit"] = captured.append
        globals()["main"] = original_main

        # Отказ _force_utf8_stdout больше не молчит: причина уходит в fd 2.
        class _FailingStdout:
            def reconfigure(self, *_a, **_k):
                raise OSError("no reconfigure")
        _saved_stdout_utf8 = sys.stdout
        _utf8_rd, _utf8_wr = os.pipe()
        _saved_err_utf8 = os.dup(2)
        os.dup2(_utf8_wr, 2)
        os.close(_utf8_wr)
        try:
            sys.stdout = _FailingStdout()
            _force_utf8_stdout()
        finally:
            sys.stdout = _saved_stdout_utf8
            os.dup2(_saved_err_utf8, 2)
            os.close(_saved_err_utf8)
        _utf8_fail_payload = os.read(_utf8_rd, 4096).decode("utf-8", "replace")
        os.close(_utf8_rd)
        check("utf8-failure-reports-cause-to-fd2",
              "PR_WATCH_UTF8_FAILED" in _utf8_fail_payload
              and "OSError" in _utf8_fail_payload
              and "no reconfigure" in _utf8_fail_payload)

        # Переменная окружения читается ВНУТРИ main(). Была раздвоенная
        # политика: переменная на импорте, авто-определение в main. Значение,
        # выставленное обёрткой после импорта, при этом молча терялось.
        detect_calls = []

        def _detect_probe():
            detect_calls.append(1)
            return "detected/repo"

        globals()["_repo_from_env"] = lambda env=None: "env/repo"
        globals()["_detect_repo"] = _detect_probe
        globals()["REPO"] = ""
        globals()["gh_json"] = lambda _args: []
        globals()["signal"] = _FakeSignal
        globals()["time"] = _FakeTime
        try:
            main()
        except _StopLoop:
            pass
        except BaseException as e:
            detect_calls.append(f"main упал: {type(e).__name__}")
        check(
            "repo-env-read-inside-main",
            globals()["REPO"] == "env/repo" and not detect_calls,
        )

        # Форма имени. Проверка живёт в main(), поэтому оба источника
        # отдают одно и то же кривое значение — кейс о валидации, а не о
        # том, какой из источников сработал.
        check(
            "repo-format-rejects-broken",
            not _repo_is_valid("")
            and not _repo_is_valid("owner")
            and not _repo_is_valid("owner/")
            and not _repo_is_valid("/name")
            and not _repo_is_valid("owner/name/extra")
            and not _repo_is_valid("owner name/repo"),
        )
        # И не строже GitHub: `.github` — существующее имя, а отказ
        # стартовать на живом репозитории хуже невнятной ошибки gh.
        check(
            "repo-format-accepts-live-names",
            _repo_is_valid("einarluha-a11y/codex-for-code")
            and _repo_is_valid("owner/.github")
            and _repo_is_valid("Owner_1/repo.name-2"),
        )
        globals()["_repo_from_env"] = lambda env=None: "не репозиторий"
        globals()["_detect_repo"] = lambda: "не репозиторий"
        globals()["REPO"] = ""
        start = len(captured)
        try:
            rc = main()
        except BaseException as e:
            rc = f"main упал: {type(e).__name__}"
        lines = captured[start:]
        check(
            "repo-format-checked-inside-main",
            rc == 1
            and any(line.startswith("WATCH_ERROR init: некорректный REPO")
                    for line in lines)
            and "WATCH_STOPPED repo=(unknown) signal=BAD_REPO" in lines,
        )
        globals()["_repo_from_env"] = original_repo_from_env
        globals()["_detect_repo"] = original_detect_repo
        globals()["REPO"] = original_repo

        # --- Разбор числовых переменных окружения (раунд 25) ---
        # _env_positive_int поднимает исключение вместо выхода из процесса
        # именно для того, чтобы форму можно было проверить здесь напрямую,
        # не убивая интерпретатор. Имя заведомо отсутствует в окружении, а
        # проверяемое значение подаётся как default — так один и тот же кейс
        # заодно доказывает, что при отсутствии переменной берётся default.
        _ABSENT = "_PRW_SELFTEST_ABSENT_VAR_"

        def _env_rejects(value):
            try:
                _env_positive_int(_ABSENT, value)
                return False
            except _EnvValueError:
                return True

        check(
            "env-parse-rejects-non-positive-int",
            _ABSENT not in os.environ
            and all(_env_rejects(v) for v in
                    ("", "abc", "30s", "3.5", "0", "-1", "0x10", "1_0", "+5")),
        )
        check(
            "env-parse-accepts-positive-int",
            _env_positive_int(_ABSENT, "1") == 1
            and _env_positive_int(_ABSENT, "30") == 30
            and _env_positive_int(_ABSENT, " 45 ") == 45
            and _env_positive_int(_ABSENT, "3600") == 3600,
        )
        # Значение по умолчанию берётся именно тогда, когда переменной в
        # окружении нет — отдельным утверждением, а не как побочный эффект.
        check(
            "env-parse-uses-default-when-absent",
            _ABSENT not in os.environ
            and _env_positive_int(_ABSENT, "30") == 30,
        )

        # Проводка, а не логика: сегодняшний дефект был в том, что причина
        # уходила не в тот поток. Кейс на разбор этого не доказал бы — нужен
        # запуск самого скрипта подпроцессом. Отказ случается на разборе
        # переменной уровня модуля (при импорте), поэтому процесс завершается
        # ещё до самопроверки. Три переменные покрыты по отдельности: дефект
        # был одинаков в трёх местах, и кейс на одно оставил бы два слепыми.
        # original_subprocess — настоящий модуль, независимо от подмен выше.
        # Ребёнок помечается, и по метке НЕ порождает собственных детей.
        # Иначе кейс безопасен только пока исправна сама проводка, которую он и
        # проверяет: сними её — и ребёнок доживает до самопроверки, порождает
        # трёх своих, те по трое, и так далее. Замерено откатом проводки:
        # 261 процесс за три секунды, самопроверка падает не красным кейсом, а
        # TimeoutExpired, обрывая всё, что шло после. Приём не имеет права
        # опираться на исправность того, что проверяет.
        _CHILD_MARK = "PR_WATCH_SELFTEST_CHILD"
        _wiring = (
            ("INTERVAL", "0"),
            ("GH_TIMEOUT", "abc"),
            ("GH_TIMEOUT_PAGINATED", "-5"),
        )
        if os.environ.get(_CHILD_MARK) == "1":
            _wiring = ()
        for _var, _bad in _wiring:
            _child_env = dict(os.environ)
            # Прочие числовые переменные — чистый default, чтобы отказ был
            # однозначно приписан именно _var, а не соседней переменной.
            for _other, _ in _wiring:
                _child_env.pop(_other, None)
            _child_env[_var] = _bad
            _child_env[_CHILD_MARK] = "1"
            _proc = original_subprocess.run(
                [sys.executable, __file__, "--self-test"],
                env=_child_env,
                capture_output=True,
                timeout=30,
            )
            _expected = (
                "WATCH_FATAL " + _var + "=" + _bad
                + " — ожидается целое число ≥ 1"
            )
            check(
                "env-wiring-subprocess-" + _var,
                _proc.returncode == 2
                and _proc.stdout.decode("utf-8", "replace").strip() == _expected
                and _proc.stderr == b"",
            )

        # Форма строки отказа: перевод строки внутри значения не разрывает
        # событие (у потребителя ровно одна строка), а длинное значение
        # обрезается до 80 символов — диагностики хватает, поток не забит.
        _nl_line = _watch_fatal_line("INTERVAL", "1\n2\r3", "r")
        check(
            "env-fatal-line-collapses-newlines",
            "\n" not in _nl_line
            and "\r" not in _nl_line
            and _nl_line == "WATCH_FATAL INTERVAL=1 2 3 — r",
        )
        _long_line = _watch_fatal_line("INTERVAL", "y" * 200, "r")
        check(
            "env-fatal-line-truncates-to-80",
            _long_line == "WATCH_FATAL INTERVAL=" + "y" * 80 + " — r",
        )
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
        globals()["_emit_raw"] = original_emit_raw
        globals()["_repo_from_env"] = original_repo_from_env
        globals()["_detect_repo"] = original_detect_repo
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
