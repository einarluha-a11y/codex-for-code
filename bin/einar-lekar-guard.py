#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Инвариант B «Лекаря» на сервере.

Барьер исполняется из БАЗОВОЙ ветки, поэтому правка этого файла внутри PR не
меняет то, что запускается. Код PR здесь не выкачивается и не исполняется:
нужен только дифф между двумя хешами, которые проставил GitHub.

Умолчание — ОТКАЗ. Любая осечка git, недостижимый объект, пустой дифф при
непустом наборе коммитов — выход 1, а не «нечего проверять».
"""
import json
import os
import re
import subprocess
import sys
import unicodedata

# Пути судьи: их правка на ветке петли означает, что исполнитель переписывает
# того, кто его оценивает. Само задание барьера лежит под .github/workflows/.
JUDGE_PATHS = [
    r"\.semgrep", r"semgrep\.ya?ml", r"ruff\.toml", r"pyproject\.toml",
    r"^\.github/workflows/", r"^\.gitmodules$", r"^bin/einar-runner-guard\.py$",
    r"^bin/einar-lekar-guard\.py$", r"^config/lekar-principals\.json$",
    r"package\.json$", r"package-lock\.json$", r"requirements.*\.txt$",
    r"^Makefile$", r"Dockerfile",
]
SUPPRESSIONS = [
    r"#\s*noqa", r"#\s*nosemgrep", r"#\s*type:\s*ignore",
    r"shellcheck\s+disable", r"eslint-disable", r"ruff:\s*noqa",
]
LOOP_PREFIXES = ("einar-fix/", "einar-step/", "lekar/")
ПРИНЦИПАЛЫ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "config", "lekar-principals.json")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Файлы, где не-ASCII буква в добавленной строке — норма (тексты, а не код).
TEXT_SUFFIXES = (".md", ".txt", ".rst", ".po", ".json")


def принципалы(путь=ПРИНЦИПАЛЫ):
    """Кого барьер считает петлёй. Читается из БАЗОВОЙ ветки — PR это не подменит.
    Файла нет или он битый — не «человек», а «принципал неизвестен»: остаётся имя ветки."""
    try:
        with open(путь, encoding="utf-8") as f:
            д = json.load(f)
    except (OSError, ValueError):
        return set(), ""
    ids = {str(x) for x in д.get("author_ids", []) if str(x).strip()}
    return ids, str(д.get("app_login") or "")


def это_петля(ветка, автор_id, ids):
    """Петля = ЛИБО PR открыт принципалом петли, ЛИБО ветка с её префиксом.

    Объединение только ДОБАВЛЯЕТ проверки и никогда не выдаёт доверие: fork с веткой
    «lekar/» и человек, назвавший ветку так же, получают лишние ограничения, а не обход.
    Числовой id, а не логин: переименование аккаунта не снимает опознание."""
    if автор_id and str(автор_id) in ids:
        return True, "PR открыт принципалом петли"
    if ветка.startswith(LOOP_PREFIXES):
        return True, "ветка с префиксом петли"
    return False, "человек"


def git(*args, cwd=None):
    """Команда git. Ненулевой код — отказ, а не пустой результат."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} → код {r.returncode}: "
                           f"{(r.stderr or '').strip()[:300]}")
    return r.stdout


def проверить_хеш(имя, значение):
    if not значение or not SHA_RE.match(значение):
        raise RuntimeError(f"{имя} не похож на полный хеш коммита")
    return значение


def подготовить(base, head, cwd=None):
    """Перенести объекты и убедиться, что это именно те коммиты. По хешам, не по именам."""
    git("fetch", "--no-tags", "--no-write-fetch-head", "origin", base, head, cwd=cwd)
    for sha in (base, head):
        git("cat-file", "-e", f"{sha}^{{commit}}", cwd=cwd)
        видимый = git("rev-parse", f"{sha}^{{commit}}", cwd=cwd).strip()
        if видимый != sha:
            raise RuntimeError(f"хеш {sha} разрешился в другой объект {видимый}")


def дифф(base, head, cwd=None):
    """(список файлов, добавленные строки). Пустой дифф при коммитах — отказ."""
    файлы = [f for f in git("diff", "--name-only", f"{base}...{head}",
                            cwd=cwd).splitlines() if f.strip()]
    коммитов = git("rev-list", "--count", f"{base}..{head}", cwd=cwd).strip()
    if коммитов != "0" and not файлы:
        raise RuntimeError(f"коммитов {коммитов}, а файлов в диффе ноль — дифф неполон")
    добавленные = []
    for строка in git("diff", "--unified=0", f"{base}...{head}", cwd=cwd).splitlines():
        if строка.startswith("+") and not строка.startswith("+++"):
            добавленные.append(строка[1:])
    return файлы, добавленные


def подавление(строка):
    """Подавление после нормализации Unicode: омоглиф не должен проходить."""
    н = unicodedata.normalize("NFKC", строка)
    for шаблон in SUPPRESSIONS:
        if re.search(шаблон, н, re.I):
            return шаблон
    return None


def судейский(путь):
    for шаблон in JUDGE_PATHS:
        if re.search(шаблон, путь):
            return шаблон
    return None


def не_ascii_в_коде(путь, строка):
    """Не-ASCII буква в добавленной строке кода. В текстах и в комментариях
    русского языка это норма, поэтому проверяются только НЕтекстовые файлы и
    только буквы вне кириллицы и латиницы — ими и подделывают `noqa`."""
    if путь.endswith(TEXT_SUFFIXES):
        return None
    for символ in строка:
        if ord(символ) < 128:
            continue
        if not unicodedata.category(символ).startswith("L"):
            continue
        имя = unicodedata.name(символ, "")
        if имя.startswith("CYRILLIC"):
            continue
        return символ
    return None


def проверить(файлы, добавленные, ветка_петли, файлы_добавленных=None,
              принципалы_известны=True):
    """(код возврата, строки отчёта). Барьер применяется только к ветке петли.

    Особый случай — принципалы не объявлены. Раньше барьер молча откатывался на
    имена веток, а они обходятся переименованием: получалась видимость защиты.
    Теперь отказ, но УЗКИЙ — только на судейских путях. Обычная работа людей не
    краснеет, а починка конфига остаётся действием владельца и она громкая.
    """
    отчёт = []
    барьер = [f for f in файлы if re.search(r"lekar-guard", f)]
    if барьер:
        отчёт.append("⚠️ ЭТОТ PR МЕНЯЕТ САМ БАРЬЕР: " + ", ".join(барьер))
    if not принципалы_известны:
        судейские = [p for p in файлы if судейский(p)]
        if судейские:
            отчёт.append("принципалы не объявлены — отличить петлю от человека нечем, "
                         "правка судейских путей отклоняется:")
            отчёт += [f"  • {p}" for p in судейские]
            return 1, отчёт
    if not ветка_петли:
        отчёт.append("ветка не принадлежит петле — инвариант B не применяется")
        return 0, отчёт
    плохо = []
    for путь in файлы:
        шаблон = судейский(путь)
        if шаблон:
            плохо.append(f"судейский путь {путь} (правило {шаблон})")
    for путь, строка in (файлы_добавленных or [(None, s) for s in добавленные]):
        шаблон = подавление(строка)
        if шаблон:
            плохо.append(f"подавление {шаблон} в добавленной строке")
        if путь:
            символ = не_ascii_в_коде(путь, строка)
            if символ:
                плохо.append(f"не-ASCII буква {символ!r} в добавленной строке кода {путь}")
    if плохо:
        отчёт.append("инвариант B НАРУШЕН:")
        отчёт += [f"  • {p}" for p in плохо]
        return 1, отчёт
    отчёт.append(f"инвариант B соблюдён: файлов в диффе {len(файлы)}, судья не тронут")
    return 0, отчёт


def main():
    try:
        base = проверить_хеш("BASE_SHA", os.environ.get("BASE_SHA"))
        head = проверить_хеш("HEAD_SHA", os.environ.get("HEAD_SHA"))
        ветка = os.environ.get("HEAD_REF") or ""
        автор_id = os.environ.get("PR_AUTHOR_ID") or ""
        автор = os.environ.get("PR_AUTHOR_LOGIN") or "?"
        ids, _ = принципалы()
        подготовить(base, head)
        файлы, добавленные = дифф(base, head)
    except Exception as e:                                   # отказ по умолчанию
        print(f"барьер НЕ состоялся: {e}", file=sys.stderr)
        return 1
    петля, почему = это_петля(ветка, автор_id, ids)
    print(f"автор PR: {автор} (id {автор_id or '—'}), ветка {ветка!r} → {почему}")
    if not ids:
        print("принципал петли НЕ объявлен: судейские пути закрыты для всех, "
              "остальное опознаётся только по имени ветки")
    код, отчёт = проверить(файлы, добавленные, петля, принципалы_известны=bool(ids))
    for строка in отчёт:
        print(строка)
    return код


def selftest():
    ok = True

    def check(имя, условие):
        nonlocal ok
        print(("  PASS " if условие else "  FAIL ") + имя)
        ok = ok and bool(условие)

    check("судья защищает сам себя",
          судейский("bin/einar-lekar-guard.py") is not None)
    check("список принципалов судейский",
          судейский("config/lekar-principals.json") is not None)

    # Принципалы не объявлены: раньше барьер тихо переходил на имена веток, а они
    # обходятся переименованием. Отказ должен быть УЗКИМ — иначе красным станет
    # всё подряд, и лечение окажется хуже болезни.
    check("принципалы неизвестны → судейский путь отклонён даже у «человека»",
          проверить(["ruff.toml"], [], False, принципалы_известны=False)[0] == 1)
    check("принципалы неизвестны → обычная правка людей НЕ краснеет",
          проверить(["src/app.py"], ["x = 1"], False, принципалы_известны=False)[0] == 0)
    check("принципалы известны → судейский путь у человека по-прежнему разрешён",
          проверить(["ruff.toml"], [], False, принципалы_известны=True)[0] == 0)
    check("принципалы неизвестны → отчёт называет причину, а не молчит",
          any("принципалы не объявлены" in с
              for с in проверить(["ruff.toml"], [], False,
                                 принципалы_известны=False)[1]))

    import tempfile
    with tempfile.TemporaryDirectory() as д:
        нет = os.path.join(д, "нет.json")
        check("файла принципалов нет — не «человек», а «неизвестен»",
              принципалы(нет) == (set(), ""))
        битый = os.path.join(д, "битый.json")
        open(битый, "w").write("{не json")
        check("битый файл принципалов — тоже «неизвестен»",
              принципалы(битый) == (set(), ""))
        живой = os.path.join(д, "живой.json")
        open(живой, "w", encoding="utf-8").write(
            '{"app_login": "lekar[bot]", "author_ids": [12345]}')
        ids, логин = принципалы(живой)
        check("принципал читается", ids == {"12345"} and логин == "lekar[bot]")

    check("PR принципала — петля, даже если ветка обычная",
          это_петля("feature/x", "12345", {"12345"})[0] is True)
    check("переименование логина не спасает: сверка по числовому id",
          это_петля("feature/x", 12345, {"12345"})[0] is True)
    check("ветка с префиксом — петля, даже если автор человек",
          это_петля("lekar/x", "999", {"12345"})[0] is True)
    check("человек с обычной веткой — не петля",
          это_петля("feature/x", "999", {"12345"})[0] is False)
    check("принципал не объявлен — остаётся имя ветки",
          это_петля("lekar/x", "999", set())[0] is True and
          это_петля("feature/x", "999", set())[0] is False)

    check("судейский путь ловится", судейский(".github/workflows/x.yml") is not None)
    check("подмодули в судейских путях", судейский(".gitmodules") is not None)
    check("обычный файл не судейский", судейский("bin/einar-step.py") is None)
    check("подавление ловится", подавление("x = 1  # noqa: F401") is not None)
    check("омоглиф не спасает подавление",
          подавление("x = 1  # ｎoqa") is not None or
          подавление(unicodedata.normalize("NFKC", "x = 1  # ｎoqa")) is not None)
    check("обычная строка не подавление", подавление("x = 1") is None)
    check("русский комментарий в коде допустим", не_ascii_в_коде("a.py", "# привет") is None)
    check("греческая буква в коде отвергается", не_ascii_в_коде("a.py", "x = ο") is not None)
    check("не-ASCII в тексте допустим", не_ascii_в_коде("a.md", "x = ο") is None)
    check("хеш не хеш — отказ", not SHA_RE.match("main"))
    code, rep = проверить(["bin/a.py"], ["x = 1"], True)
    check("чистый дифф на ветке петли проходит", code == 0 and "соблюдён" in rep[-1])
    code, rep = проверить([".github/workflows/x.yml"], [], True)
    check("правка задания на ветке петли отвергается", code == 1)
    code, rep = проверить([".github/workflows/x.yml"], [], False)
    check("та же правка вне петли проходит", code == 0)
    code, rep = проверить(["bin/lekar-guard.py"], [], False)
    check("правка барьера видна человеку и вне петли",
          code == 0 and any("МЕНЯЕТ САМ БАРЬЕР" in s for s in rep))
    code, rep = проверить(["bin/a.py"], ["y = 2  # noqa"], True)
    check("подавление на ветке петли отвергается", code == 1)
    code, rep = проверить(["bin/a.py"], [], True, файлы_добавленных=[("bin/a.py", "x = ο")])
    check("омоглиф в коде на ветке петли отвергается", code == 1)
    # Сквозной прогон на настоящем git: своя пара «origin + рабочая копия».
    import shutil as _sh, tempfile as _tf
    _корень = _tf.mkdtemp(prefix="лекарь-барьер-")
    try:
        _голый = os.path.join(_корень, "origin.git")
        _раб = os.path.join(_корень, "раб")
        subprocess.run(["git", "init", "-q", "--bare", _голый], check=True)
        subprocess.run(["git", "clone", "-q", _голый, _раб], check=True)
        for имя, знач in (("user.email", "лекарь@самотест"), ("user.name", "лекарь")):
            subprocess.run(["git", "config", имя, знач], cwd=_раб, check=True)
        open(os.path.join(_раб, "a.py"), "w").write("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=_раб, check=True)
        subprocess.run(["git", "commit", "-qm", "база"], cwd=_раб, check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=_раб, check=True)
        base = git("rev-parse", "HEAD", cwd=_раб).strip()
        open(os.path.join(_раб, "b.py"), "w").write("y = 2  # noqa\n")
        subprocess.run(["git", "add", "-A"], cwd=_раб, check=True)
        subprocess.run(["git", "commit", "-qm", "правка"], cwd=_раб, check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD:тема"], cwd=_раб, check=True)
        head = git("rev-parse", "HEAD", cwd=_раб).strip()

        подготовить(base, head, cwd=_раб)
        check("объекты по хешам достижимы", True)
        файлы, добавленные = дифф(base, head, cwd=_раб)
        check("дифф видит добавленный файл", файлы == ["b.py"])
        check("дифф видит добавленную строку", any("noqa" in s2 for s2 in добавленные))
        код, _ = проверить(файлы, добавленные, True)
        check("сквозной прогон ловит подавление на ветке петли", код == 1)
        код, _ = проверить(файлы, добавленные, False)
        check("сквозной прогон вне петли пропускает", код == 0)
        try:
            подготовить(base, "0" * 40, cwd=_раб)
            check("несуществующий хеш даёт отказ", False)
        except RuntimeError:
            check("несуществующий хеш даёт отказ", True)
        try:
            проверить_хеш("HEAD_SHA", "main")
            check("имя ветки вместо хеша отвергается", False)
        except RuntimeError:
            check("имя ветки вместо хеша отвергается", True)
        try:
            git("rev-parse", "нетакого", cwd=_раб)
            check("ненулевой код git — отказ, а не пустой ответ", False)
        except RuntimeError:
            check("ненулевой код git — отказ, а не пустой ответ", True)
    finally:
        _sh.rmtree(_корень, ignore_errors=True)

    print("ИТОГ: " + ("самопроверка пройдена" if ok else "самопроверка ПРОВАЛЕНА"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--self-test" in sys.argv else main())
