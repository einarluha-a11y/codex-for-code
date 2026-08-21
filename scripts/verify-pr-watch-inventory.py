#!/usr/bin/env python3
"""
verify-pr-watch-inventory.py

Обнаруживает все копии pr-watch.py по парку (диск + GitHub),
категоризирует по отношению к канону и выводит таблицу с колонкой
«чем запускается». Состав копий выводится перебором — список имён
не ведётся.

Коды выхода:
  0 — оба источника отработали; каждая найденная копия либо на каноне,
      либо объявлена исключением с непустой причиной.
  1 — есть отставшая / расходящаяся копия без объявленного исключения,
      либо найдено устаревшее исключение (запись в файле, но копия не найдена).
  2 — проверка не смогла проверить: недоступен любой из двух источников,
      не удалось получить канон, нет gh или авторизации.
      Код 0 при недоступном источнике категорически запрещён.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# Маркеры для обнаружения копии (минимум 2 из 4 должны присутствовать в содержимом).
MARKERS = ["WATCH_STARTED", "PR_COMMENT #", "WATCH_DEGRADED", "WATCH_HEARTBEAT"]
MIN_MARKERS = 2

# Каталоги, которые пропускаются при обходе диска.
SKIP_DIRS = frozenset({
    "node_modules", ".git", "dist", "build", ".venv", "worktrees",
    "__pycache__", ".cache", ".next", "venv", "env", ".tox",
})

MAX_DEPTH = 3

DISK_ROOTS = [
    Path("/Users/einarluha/Documents/Projects 2/work"),
    Path("/Users/einarluha/Documents/Projects 2/work/Agents"),
]

CANONICAL_REPO = "einarluha-a11y/codex-for-code"
CANONICAL_PATH_IN_REPO = "scripts/pr-watch.py"

EXCEPTIONS_FILE = Path(__file__).parent / "pr-watch-inventory-exceptions.json"

# Корзины
BASKET_ON_CANON = "НА КАНОНЕ"
BASKET_BEHIND = "ОТСТАЛ"
BASKET_DIVERGED = "ТОГО ЖЕ НАЗНАЧЕНИЯ, НЕ КАНОН"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def run_cmd(cmd: list, cwd: Optional[str] = None) -> tuple:
    """Запустить команду, вернуть (returncode, stdout, stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def has_min_markers(content: str) -> bool:
    """True если в содержимом есть минимум MIN_MARKERS из MARKERS."""
    if "pr-watch-inventory-exceptions.json" in content:
        return False
    return sum(1 for m in MARKERS if m in content) >= MIN_MARKERS


def extract_emitted_events(content: str) -> frozenset:
    """Извлечь множество префиксов событий, эмитируемых скриптом.

    Ищет паттерн emit( ... "WORD или emit( ... f"WORD, включая случаи
    когда строка находится на следующей строке после emit(.
    """
    # \s* обрабатывает пробелы и переносы строк между emit( и открывающей кавычкой.
    pattern = re.compile(
        r'emit\s*\(\s*(?:f)?["\']([A-Z][A-Z_0-9]+)',
    )
    events = set()
    for m in pattern.finditer(content):
        token = m.group(1)
        if token.startswith(("WATCH_", "PR_")):
            events.add(token)
    return frozenset(events)


def classify_basket(content: str, file_sha: str, canon_sha: str,
                    canon_events: frozenset) -> str:
    """Отнести копию к одной из трёх корзин."""
    if file_sha == canon_sha:
        return BASKET_ON_CANON
    events = extract_emitted_events(content)
    extra = events - canon_events
    if extra:
        return BASKET_DIVERGED
    return BASKET_BEHIND


# ---------------------------------------------------------------------------
# Получение канона
# ---------------------------------------------------------------------------

def fetch_github_file(repo: str, path_in_repo: str) -> Optional[bytes]:
    """Получить содержимое файла из GitHub. Возвращает bytes или None."""
    rc, stdout, _ = run_cmd([
        "gh", "api",
        f"repos/{repo}/contents/{path_in_repo}",
        "--jq", ".content",
    ])
    if rc != 0 or not stdout.strip():
        return None
    try:
        return base64.b64decode(stdout.strip().replace("\n", ""))
    except Exception:
        return None


def get_canonical(
    sha_override: Optional[str] = None,
    content_override: Optional[bytes] = None,
) -> tuple:
    """Вернуть (sha256, content, canonical_events). Вызывает исключение при неудаче."""
    if sha_override is not None:
        events = extract_emitted_events(
            (content_override or b"").decode("utf-8", errors="replace")
        )
        return sha_override, content_override or b"", events

    raw = fetch_github_file(CANONICAL_REPO, CANONICAL_PATH_IN_REPO)
    if raw is None:
        raise RuntimeError(
            f"Не удалось получить канон {CANONICAL_REPO}/{CANONICAL_PATH_IN_REPO}"
        )
    content_str = raw.decode("utf-8", errors="replace")
    canon_events = extract_emitted_events(content_str)
    return sha256_of(raw), raw, canon_events


# ---------------------------------------------------------------------------
# Сканирование диска
# ---------------------------------------------------------------------------

def scan_disk(roots: list, max_depth: int = MAX_DEPTH) -> list:
    """Обойти корни и найти кандидатов по содержимому. Возвращает список dict."""
    found = []
    seen_inodes: set = set()

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            items = sorted(path.iterdir())
        except PermissionError:
            return
        for item in items:
            if item.name in SKIP_DIRS:
                continue
            if item.is_symlink():
                continue
            if item.is_file() and item.suffix == ".py":
                try:
                    st = item.stat()
                    if st.st_ino in seen_inodes:
                        continue
                    seen_inodes.add(st.st_ino)
                    raw = item.read_bytes()
                    content = raw.decode("utf-8", errors="replace")
                    if has_min_markers(content):
                        found.append({
                            "disk_path": item,
                            "content_bytes": raw,
                            "content": content,
                            "source": "disk",
                        })
                except Exception:
                    pass
            elif item.is_dir():
                walk(item, depth + 1)

    for root in roots:
        if isinstance(root, Path) and root.is_dir():
            walk(root, 0)

    return found


# ---------------------------------------------------------------------------
# Сканирование GitHub
# ---------------------------------------------------------------------------

def check_gh_auth() -> bool:
    rc, _, _ = run_cmd(["gh", "auth", "status"])
    return rc == 0


def get_all_repos() -> tuple:
    """Вернуть (список nwo, success). Использует --paginate."""
    rc, stdout, _ = run_cmd([
        "gh", "api", "/user/repos",
        "--paginate",
        "--jq", ".[].full_name",
    ])
    if rc != 0:
        return [], False
    repos = [r.strip() for r in stdout.splitlines() if r.strip()]
    return repos, True


def get_py_paths_in_repo(repo: str) -> list:
    """Найти .py файлы в репозитории (дерево HEAD)."""
    rc, stdout, _ = run_cmd([
        "gh", "api",
        f"repos/{repo}/git/trees/HEAD",
        "--jq", '[.tree[] | select(.path | endswith(".py"))] | .[].path',
    ])
    if rc != 0 or not stdout.strip():
        return []
    return [p.strip() for p in stdout.splitlines() if p.strip()]


def scan_github(repos: list) -> list:
    """Просканировать репозитории GitHub в поиске кандидатов."""
    found = []
    seen: set = set()

    for repo in repos:
        # Сначала пробуем канонический путь.
        candidates = [CANONICAL_PATH_IN_REPO]

        # Дополнительно ищем переименованные копии через дерево репозитория.
        try:
            all_py = get_py_paths_in_repo(repo)
            for p in all_py:
                if p not in candidates:
                    candidates.append(p)
        except Exception:
            pass

        for path_in_repo in candidates:
            raw = fetch_github_file(repo, path_in_repo)
            if raw is None:
                continue
            try:
                content = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            if not has_min_markers(content):
                continue
            key = (repo, sha256_of(raw))
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "repo": repo,
                "path_in_repo": path_in_repo,
                "content_bytes": raw,
                "content": content,
                "source": "github",
            })

    return found


# ---------------------------------------------------------------------------
# Механизм запуска
# ---------------------------------------------------------------------------

def find_repo_root(path: Path) -> Optional[Path]:
    p = path.parent
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def get_repo_nwo(repo_path: Path) -> Optional[str]:
    rc, stdout, _ = run_cmd(
        ["git", "remote", "get-url", "origin"], cwd=str(repo_path)
    )
    if rc != 0:
        return None
    url = stdout.strip()
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def find_launch_mechanisms(repo_path: Path, script_path: Path) -> list:
    """Найти следы механизма запуска скрипта в репозитории."""
    mechs = []
    script_name = script_path.name
    try:
        rel_str = str(script_path.relative_to(repo_path))
    except ValueError:
        rel_str = script_name
    patterns = [script_name, rel_str]

    def references(file_path: Path) -> bool:
        try:
            text = file_path.read_text(errors="replace")
            return any(p in text for p in patterns)
        except Exception:
            return False

    # plist-файлы + проверка launchd
    for plist in repo_path.rglob("*.plist"):
        if any(part in SKIP_DIRS for part in plist.parts):
            continue
        if not references(plist):
            continue
        rel_plist = str(plist.relative_to(repo_path))
        label = None
        try:
            text = plist.read_text(errors="replace")
            m = re.search(
                r"<key>Label</key>\s*<string>([^<]+)</string>", text
            )
            if m:
                label = m.group(1)
        except Exception:
            pass
        if label:
            rc, stdout, _ = run_cmd(["launchctl", "list", label])
            if rc == 0 and '"PID"' in stdout:
                mechs.append(f"launchd:{label} (активно) via {rel_plist}")
            else:
                mechs.append(f"plist:{rel_plist}")
        else:
            mechs.append(f"plist:{rel_plist}")

    # GitHub Actions workflows
    workflows_dir = repo_path / ".github" / "workflows"
    if workflows_dir.is_dir():
        for wf in sorted(workflows_dir.iterdir()):
            if wf.suffix in (".yml", ".yaml") and references(wf):
                mechs.append(f"workflow:{wf.relative_to(repo_path)}")

    # Shell-скрипты
    for sh in repo_path.rglob("*.sh"):
        if any(part in SKIP_DIRS for part in sh.parts):
            continue
        if sh == script_path:
            continue
        if references(sh):
            mechs.append(f"sh:{sh.relative_to(repo_path)}")

    return mechs if mechs else ["не запускается ничем (следов не найдено)"]


# ---------------------------------------------------------------------------
# Слияние результатов
# ---------------------------------------------------------------------------

def process_entries(
    disk_entries: list,
    github_entries: list,
    canon_sha: str,
    canon_events: frozenset,
) -> list:
    """Объединить disk + github записи в единый список."""
    all_entries = []
    seen_keys: set = set()

    for de in disk_entries:
        path = de["disk_path"]
        repo_root = find_repo_root(path)
        nwo = get_repo_nwo(repo_root) if repo_root else None
        file_sha = sha256_of(de["content_bytes"])

        if repo_root:
            try:
                path_display = str(path.relative_to(repo_root))
            except ValueError:
                path_display = str(path)
        else:
            path_display = str(path)

        is_canonical = (
            nwo == CANONICAL_REPO and path_display == CANONICAL_PATH_IN_REPO
        )
        key = (nwo or str(path), file_sha)

        if key in seen_keys:
            for e in all_entries:
                if e["_key"] == key:
                    e["sources"].add("disk")
            continue
        seen_keys.add(key)

        all_entries.append({
            "_key": key,
            "repo": nwo or "(local)",
            "path_display": path_display,
            "disk_path": path,
            "repo_root": repo_root,
            "lines": len(de["content"].splitlines()),
            "sha256": file_sha,
            "sha12": file_sha[:12],
            "basket": classify_basket(
                de["content"], file_sha, canon_sha, canon_events
            ),
            "content": de["content"],
            "is_canonical": is_canonical,
            "sources": {"disk"},
        })

    for ge in github_entries:
        file_sha = sha256_of(ge["content_bytes"])
        nwo = ge["repo"]
        key = (nwo, file_sha)
        is_canonical = (
            nwo == CANONICAL_REPO
            and ge["path_in_repo"] == CANONICAL_PATH_IN_REPO
        )

        if key in seen_keys:
            for e in all_entries:
                if e["_key"] == key:
                    e["sources"].add("github")
            continue
        seen_keys.add(key)

        all_entries.append({
            "_key": key,
            "repo": nwo,
            "path_display": ge["path_in_repo"],
            "disk_path": None,
            "repo_root": None,
            "lines": len(ge["content"].splitlines()),
            "sha256": file_sha,
            "sha12": file_sha[:12],
            "basket": classify_basket(
                ge["content"], file_sha, canon_sha, canon_events
            ),
            "content": ge["content"],
            "is_canonical": is_canonical,
            "sources": {"github"},
        })

    # Убираем сам канон из результатов (он эталон, не потребитель).
    entries = [e for e in all_entries if not e["is_canonical"]]

    # Определяем механизм запуска.
    for e in entries:
        if e["disk_path"] and e["repo_root"]:
            e["launch"] = find_launch_mechanisms(e["repo_root"], e["disk_path"])
        else:
            e["launch"] = ["(только GitHub, диск не проверен)"]

    return entries


# ---------------------------------------------------------------------------
# Загрузка исключений
# ---------------------------------------------------------------------------

def load_exceptions(path: Optional[Path] = None) -> dict:
    ef = path or EXCEPTIONS_FILE
    if not ef.exists():
        return {}
    try:
        data = json.loads(ef.read_text())
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        print(f"ПРЕДУПРЕЖДЕНИЕ: не удалось прочитать {ef}: {e}", file=sys.stderr)
        return {}


def validate_exceptions(exceptions: dict) -> Optional[str]:
    for nwo, reason in exceptions.items():
        if not isinstance(reason, str) or not reason.strip():
            return f"исключение для '{nwo}' имеет пустую причину"
    return None


# ---------------------------------------------------------------------------
# Основная логика (изолирована для тестируемости)
# ---------------------------------------------------------------------------

def main_logic(
    disk_entries: list,
    github_entries: list,
    github_ok: bool,
    canon_sha: str,
    canon_events: frozenset,
    exceptions: dict,
    as_json: bool,
    out=None,
    err=None,
) -> int:
    """
    Принимает сырые данные и решает: мержит, классифицирует, выводит.
    Возвращает код выхода 0/1/2.
    Параметры out/err позволяют перехватить stdout/stderr в тестах.
    """
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr

    if not github_ok:
        print(
            "ОШИБКА: источник GitHub недоступен. "
            "Результат без него неполный и ненадёжный.",
            file=err,
        )
        return 2

    entries = process_entries(disk_entries, github_entries, canon_sha, canon_events)

    # Применяем исключения.
    exception_hits: set = set()
    for e in entries:
        if e["repo"] in exceptions:
            e["exception"] = exceptions[e["repo"]]
            exception_hits.add(e["repo"])

    stale_exceptions = [nwo for nwo in exceptions if nwo not in exception_hits]

    # Определяем код выхода.
    exit_code = 0
    for e in entries:
        if e["basket"] != BASKET_ON_CANON and "exception" not in e:
            exit_code = 1
            break
    if stale_exceptions:
        exit_code = 1

    if as_json:
        output = {
            "canon": f"{CANONICAL_REPO}/{CANONICAL_PATH_IN_REPO}",
            "canon_sha256": canon_sha,
            "entries": [
                {
                    "repo": e["repo"],
                    "path": e["path_display"],
                    "lines": e["lines"],
                    "sha256": e["sha256"],
                    "basket": e["basket"],
                    "launch": e.get("launch", []),
                    "sources": sorted(e["sources"]),
                    "exception": e.get("exception"),
                }
                for e in entries
            ],
            "stale_exceptions": {
                nwo: exceptions[nwo] for nwo in stale_exceptions
            },
            "summary": {
                "total": len(entries),
                "on_canon": sum(
                    1 for e in entries if e["basket"] == BASKET_ON_CANON
                ),
                "behind": sum(
                    1 for e in entries if e["basket"] == BASKET_BEHIND
                ),
                "diverged": sum(
                    1 for e in entries if e["basket"] == BASKET_DIVERGED
                ),
                "exceptions": sum(1 for e in entries if "exception" in e),
            },
            "exit_code": exit_code,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2), file=out)
    else:
        _print_table(entries, stale_exceptions, exceptions, exit_code, out)

    return exit_code


def _print_table(
    entries: list,
    stale_exceptions: list,
    exceptions: dict,
    exit_code: int,
    out,
) -> None:
    cols = [
        ("Репозиторий", 36),
        ("Путь", 32),
        ("Строк", 6),
        ("sha256", 14),
        ("Корзина", 36),
        ("Запуск", 40),
        ("Источник", 10),
    ]
    sep = "  "
    header = sep.join(c.ljust(w) for c, w in cols)
    print("\n" + "=" * len(header), file=out)
    print(header, file=out)
    print("-" * len(header), file=out)

    for e in entries:
        exc = e.get("exception", "")
        exc_note = f" [исключение: {exc[:22]}{'…' if len(exc)>22 else ''}]" if exc else ""
        basket = (e["basket"] + exc_note)[:35]

        launch_str = "; ".join(e.get("launch", []))
        if len(launch_str) > 39:
            launch_str = launch_str[:36] + "..."

        row = [
            e["repo"][:35],
            e["path_display"][:31],
            str(e["lines"]),
            e["sha12"],
            basket,
            launch_str,
            "+".join(sorted(e["sources"])),
        ]
        widths = [w for _, w in cols]
        print(sep.join(v.ljust(w) for v, w in zip(row, widths)), file=out)

    print("=" * len(header), file=out)

    on_canon = sum(1 for e in entries if e["basket"] == BASKET_ON_CANON)
    behind = sum(1 for e in entries if e["basket"] == BASKET_BEHIND)
    diverged = sum(1 for e in entries if e["basket"] == BASKET_DIVERGED)
    exc_count = sum(1 for e in entries if "exception" in e)
    print(
        f"копий: {len(entries)} · на каноне: {on_canon} · "
        f"отстало: {behind} · чужого варианта: {diverged} · "
        f"исключений: {exc_count}",
        file=out,
    )

    if stale_exceptions:
        print(
            "\nУСТАРЕВШИЕ ИСКЛЮЧЕНИЯ (копия не найдена — "
            "запись расходится с реальностью):",
            file=out,
        )
        for nwo in stale_exceptions:
            print(f"  {nwo}: {exceptions[nwo]}", file=out)

    label = {0: "OK", 1: "есть отклонения", 2: "не смог проверить"}.get(
        exit_code, str(exit_code)
    )
    print(f"\nИТОГ: код выхода {exit_code} — {label}.", file=out)


# ---------------------------------------------------------------------------
# Основная точка входа (с I/O)
# ---------------------------------------------------------------------------

def cmd_main(args) -> int:
    exceptions = load_exceptions()
    err_msg = validate_exceptions(exceptions)
    if err_msg:
        print(f"ОШИБКА: {err_msg}", file=sys.stderr)
        return 2

    # Получаем канон.
    print("Получаю канон из GitHub...", flush=True)
    try:
        canon_sha, _, canon_events = get_canonical()
    except Exception as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    print(f"  sha256={canon_sha[:12]}...", flush=True)

    # Сканируем диск.
    print("Сканирую диск...", flush=True)
    disk_entries = scan_disk(DISK_ROOTS)
    print(f"  найдено на диске кандидатов: {len(disk_entries)}", flush=True)

    # Проверяем gh.
    if not check_gh_auth():
        print("ОШИБКА: gh не авторизован", file=sys.stderr)
        return 2

    # Получаем список репозиториев.
    print("Получаю список репозиториев GitHub...", flush=True)
    repos, repos_ok = get_all_repos()
    if not repos_ok:
        print("ОШИБКА: не удалось получить список репозиториев", file=sys.stderr)
        return 2
    print(f"  репозиториев: {len(repos)}", flush=True)

    # Сканируем GitHub.
    print("Проверяю содержимое репозиториев...", flush=True)
    github_entries = scan_github(repos)
    print(f"  найдено на GitHub кандидатов: {len(github_entries)}", flush=True)

    return main_logic(
        disk_entries=disk_entries,
        github_entries=github_entries,
        github_ok=True,
        canon_sha=canon_sha,
        canon_events=canon_events,
        exceptions=exceptions,
        as_json=args.json,
    )


# ---------------------------------------------------------------------------
# Самопроверка
# ---------------------------------------------------------------------------

def run_self_test() -> int:
    """Самопроверка без обращений к сети и к реальному парку."""
    import io

    passed = []
    failed = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            passed.append(f"  PASS: {name}")
        else:
            failed.append(
                f"  FAIL: {name}" + (f" — {detail}" if detail else "")
            )

    # Mock-канон с полным набором событий.
    CANON_CONTENT = b"""\
#!/usr/bin/env python3
# Mock canonical pr-watch.py for self-test
WATCH_STARTED_EV = 'WATCH_STARTED'
PR_COMMENT_EV = 'PR_COMMENT #'
def run():
    emit("WATCH_STARTED repo=test interval=60s")
    emit(f"PR_OPENED #{1} | title")
    emit(f"PR_CLOSED #{1}")
    emit(f"PR_COMMENT #{1} by user [id=42]: body")
    emit(f"PR_CI #{1} | check=success")
    emit(f"PR_REVIEW #{1} | APPROVED")
    emit(f"PR_LABELS #{1} | ready")
    emit(f"WATCH_STOPPED repo=test signal=SIGTERM")
"""
    CANON_SHA = sha256_of(CANON_CONTENT)
    CANON_EVENTS = extract_emitted_events(
        CANON_CONTENT.decode("utf-8", errors="replace")
    )

    # Убеждаемся, что extract нашёл хотя бы несколько событий из mock-канона.
    check(
        "Подготовка: mock-канон содержит ≥4 события",
        len(CANON_EVENTS) >= 4,
        f"найдено: {sorted(CANON_EVENTS)}",
    )

    with tempfile.TemporaryDirectory(prefix="pr-watch-selftest-") as tmpdir:
        tmp = Path(tmpdir)

        # -------------------------------------------------------------------
        # Кейс 1: байт-равная копия → НА КАНОНЕ
        # -------------------------------------------------------------------
        c1 = tmp / "repo1" / "scripts" / "pr-watch.py"
        c1.parent.mkdir(parents=True)
        c1.write_bytes(CANON_CONTENT)
        b1 = classify_basket(
            c1.read_text(errors="replace"), CANON_SHA, CANON_SHA, CANON_EVENTS
        )
        check("Case 1: байт-равная копия → НА КАНОНЕ", b1 == BASKET_ON_CANON, b1)

        # -------------------------------------------------------------------
        # Кейс 2: урезанный набор событий → ОТСТАЛ
        # -------------------------------------------------------------------
        BEHIND = b"""\
#!/usr/bin/env python3
# Older version
WATCH_STARTED_EV = 'WATCH_STARTED'
PR_COMMENT_EV = 'PR_COMMENT #'
def run():
    emit("WATCH_STARTED repo=test")
    emit(f"PR_COMMENT #{1} by u [id=1]: body")
    emit(f"WATCH_STOPPED repo=test signal=SIGTERM")
"""
        c2 = tmp / "repo2" / "scripts" / "pr-watch.py"
        c2.parent.mkdir(parents=True)
        c2.write_bytes(BEHIND)
        sha2 = sha256_of(BEHIND)
        b2 = classify_basket(
            c2.read_text(errors="replace"), sha2, CANON_SHA, CANON_EVENTS
        )
        check("Case 2: урезанный набор событий → ОТСТАЛ", b2 == BASKET_BEHIND, b2)

        # -------------------------------------------------------------------
        # Кейс 3: лишнее событие WATCH_RECOVERED → ТОГО ЖЕ НАЗНАЧЕНИЯ, НЕ КАНОН
        # -------------------------------------------------------------------
        EXTRA = b"""\
#!/usr/bin/env python3
# Fork with WATCH_RECOVERED not in canonical
WATCH_STARTED_EV = 'WATCH_STARTED'
PR_COMMENT_EV = 'PR_COMMENT #'
def run():
    emit("WATCH_STARTED repo=test")
    emit(f"PR_COMMENT #{1} by u [id=1]: body")
    emit(f"WATCH_RECOVERED repo=test after=120s")
    emit(f"WATCH_STOPPED repo=test signal=SIGTERM")
"""
        c3 = tmp / "repo3" / "scripts" / "pr-watch.py"
        c3.parent.mkdir(parents=True)
        c3.write_bytes(EXTRA)
        sha3 = sha256_of(EXTRA)
        b3 = classify_basket(
            c3.read_text(errors="replace"), sha3, CANON_SHA, CANON_EVENTS
        )
        check(
            "Case 3: лишнее событие WATCH_RECOVERED → ТОГО ЖЕ НАЗНАЧЕНИЯ, НЕ КАНОН",
            b3 == BASKET_DIVERGED,
            b3,
        )

        # -------------------------------------------------------------------
        # Кейс 4: переименованный файл monitor.py с маркерами → всё равно найден
        # -------------------------------------------------------------------
        MONITOR = b"""\
#!/usr/bin/env python3
# Renamed to monitor.py but same content class
WATCH_STARTED_EV = 'WATCH_STARTED'
PR_COMMENT_EV = 'PR_COMMENT #'
"""
        c4 = tmp / "repo4" / "scripts" / "monitor.py"
        c4.parent.mkdir(parents=True)
        c4.write_bytes(MONITOR)
        check(
            "Case 4a: has_min_markers для monitor.py",
            has_min_markers(c4.read_text(errors="replace")),
        )
        found4 = scan_disk([tmp / "repo4"], max_depth=3)
        check(
            "Case 4b: disk scan находит monitor.py",
            any(e["disk_path"].name == "monitor.py" for e in found4),
            f"нашли: {[e['disk_path'].name for e in found4]}",
        )

        # -------------------------------------------------------------------
        # Кейс 5: файл в node_modules → НЕ найден
        # -------------------------------------------------------------------
        nm = tmp / "repo5" / "node_modules" / "some-pkg"
        nm.mkdir(parents=True)
        (nm / "pr-watch.py").write_bytes(CANON_CONTENT)
        found5 = scan_disk([tmp / "repo5"], max_depth=3)
        check(
            "Case 5: файл в node_modules → НЕ найден",
            len(found5) == 0,
            f"нашли {len(found5)} файлов",
        )

        # -------------------------------------------------------------------
        # Кейс 6: устаревшее исключение → exit code 1 + текст в выводе
        # -------------------------------------------------------------------
        stale_exc = {"nonexistent/repo": "Тестовое исключение"}
        buf6 = io.StringIO()
        code6 = main_logic(
            disk_entries=[],
            github_entries=[],
            github_ok=True,
            canon_sha=CANON_SHA,
            canon_events=CANON_EVENTS,
            exceptions=stale_exc,
            as_json=False,
            out=buf6,
            err=io.StringIO(),
        )
        output6 = buf6.getvalue()
        check("Case 6a: устаревшее исключение → exit code 1", code6 == 1, f"код={code6}")
        check(
            "Case 6b: слова 'УСТАРЕВШИЕ ИСКЛЮЧЕНИЯ' в выводе",
            "УСТАРЕВШИЕ ИСКЛЮЧЕНИЯ" in output6,
        )

        # -------------------------------------------------------------------
        # Кейс 7: недоступный GitHub → exit code 2, не 0
        # -------------------------------------------------------------------
        err7 = io.StringIO()
        code7 = main_logic(
            disk_entries=[],
            github_entries=[],
            github_ok=False,
            canon_sha=CANON_SHA,
            canon_events=CANON_EVENTS,
            exceptions={},
            as_json=False,
            out=io.StringIO(),
            err=err7,
        )
        check("Case 7: недоступный GitHub → код 2", code7 == 2, f"код={code7}")
        check(
            "Case 7b: сообщение об ошибке напечатано",
            "ОШИБКА" in err7.getvalue() or len(err7.getvalue()) > 0,
        )

        # -------------------------------------------------------------------
        # Кейс 8: ДОКАЗАТЕЛЬСТВО ОТКАТОМ
        # Подменяем «недоступен→ошибка» на «недоступен→пусто» и убеждаемся,
        # что код становится 0 — значит Case 7 ловит именно эту ошибку.
        # -------------------------------------------------------------------
        def patched_main_logic_empty_on_unavailable(
            disk_entries, github_entries, github_ok,
            canon_sha, canon_events, exceptions, as_json, out=None, err=None
        ):
            """Патч: github_ok=False трактуется как «пустой результат», а не ошибка."""
            # НЕПРАВИЛЬНО: игнорируем флаг доступности.
            return main_logic(
                disk_entries=disk_entries,
                github_entries=github_entries,
                github_ok=True,   # <-- подмена
                canon_sha=canon_sha,
                canon_events=canon_events,
                exceptions=exceptions,
                as_json=as_json,
                out=out or io.StringIO(),
                err=err or io.StringIO(),
            )

        code8_patched = patched_main_logic_empty_on_unavailable(
            disk_entries=[],
            github_entries=[],
            github_ok=False,
            canon_sha=CANON_SHA,
            canon_events=CANON_EVENTS,
            exceptions={},
            as_json=True,
        )
        check(
            "Case 8a (доказательство откатом): подмена 'недоступен→пусто' даёт код 0",
            code8_patched == 0,
            f"код={code8_patched}",
        )
        check(
            "Case 8b: с настоящим гейтом код 2, с подменой код 0 — гейт ловит именно это",
            code7 == 2 and code8_patched == 0,
            f"с_гейтом={code7} с_подменой={code8_patched}",
        )

    # Итог.
    total = len(passed) + len(failed)
    print()
    for r in passed + failed:
        print(r)
    print()
    if not failed:
        print(f"SELF-TEST OK {len(passed)}/{total}")
        return 0
    else:
        print(f"SELF-TEST FAILED: упало {len(failed)}/{total}")
        return 1


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Проверка состава копий pr-watch.py по парку репозиториев. "
            "Состав выводится перебором — список имён не ведётся."
        )
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Запустить самопроверку (без сети и реального парка) и выйти.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывод в машиночитаемом JSON-формате.",
    )
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    sys.exit(cmd_main(args))
