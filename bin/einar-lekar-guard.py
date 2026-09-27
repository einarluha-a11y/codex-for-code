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


def пути_диффа(base, head, cwd=None):
    """Изменённые пути ДОСЛОВНО, как они лежат в дереве.

    Без `-z` git печатает путь с не-ASCII или служебными символами в кавычках и
    восьмеричных escape (`".github/workflows/\\320\\277.yml"`), и такая строка не
    совпадает ни с одним судейским шаблоном — правка судьи проходила бы как чужая.
    `-z` отдаёт пути сырыми байтами через NUL; `--no-renames` называет при
    переименовании ОБА пути, иначе вывод судейского файла из-под шаблона
    («переименовал workflow в docs/») виден только по новому имени.
    Байты читаются без перевода строк: `\\r` в имени файла остаётся `\\r`."""
    r = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--name-only",
                        "-z", "--no-renames", f"{base}...{head}"],
                       cwd=cwd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git diff --name-only -z → код {r.returncode}: "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return [p.decode("utf-8", "surrogateescape")
            for p in r.stdout.split(b"\0") if p]


def строки_патча(патч):
    """Добавленные строки патча `--unified=0` (байты). Строка засчитывается только
    ВНУТРИ ханка: заголовок `+++ b/…` не попадает, а строка кода «++x» (в патче
    `+++x`) больше не теряется. Делится только по `\\n`: splitlines() рвал бы строку
    по U+2028 или `\\x0c`, и хвост с подавлением выпадал бы из проверки."""
    добавленные, в_ханке = [], False
    for сырая in патч.split(b"\n"):
        if сырая.startswith(b"diff --git "):
            в_ханке = False
        elif сырая.startswith(b"@@"):
            в_ханке = True
        elif в_ханке and сырая.startswith(b"+"):
            добавленные.append(сырая[1:].decode("utf-8", "replace"))
        elif в_ханке and сырая[:1] not in (b"-", b" ", b"\\", b""):
            в_ханке = False
    return добавленные


def патч(base, head, пути=None, cwd=None):
    """Сырой патч `--unified=0`; с `пути` — только по этим путям, взятым ДОСЛОВНО
    (`--literal-pathspecs`: `*` и `:(…)` в имени файла — не шаблоны)."""
    args = ["git", "--literal-pathspecs", "-c", "core.quotePath=false", "diff",
            "--unified=0", "--no-renames", "--no-color", "--no-ext-diff",
            "--no-textconv", f"{base}...{head}"]
    if пути is not None:
        args += ["--", *пути]
    r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git diff --unified=0 → код {r.returncode}: "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return r.stdout


def дифф(base, head, cwd=None):
    """(файлы, добавленные строки, пары (путь, строка)). Пустой дифф при коммитах — отказ.

    Путь каждой строки берётся не из заголовка `+++ b/…` (его git экранирует для
    не-ASCII имён), а отдельным патчем по каждому пути из `пути_диффа`. Сумма строк
    по путям обязана совпасть с общим диффом — иначе привязка неполна, и это отказ:
    без пути проверка не-ASCII в коде молча не работала бы."""
    файлы = пути_диффа(base, head, cwd=cwd)
    коммитов = git("rev-list", "--count", f"{base}..{head}", cwd=cwd).strip()
    if коммитов != "0" and not файлы:
        raise RuntimeError(f"коммитов {коммитов}, а файлов в диффе ноль — дифф неполон")
    добавленные = строки_патча(патч(base, head, cwd=cwd))
    файлы_добавленных = []
    for путь in файлы:
        файлы_добавленных += [(путь, с) for с in
                              строки_патча(патч(base, head, [путь], cwd=cwd))]
    if len(файлы_добавленных) != len(добавленные):
        raise RuntimeError(f"строк по путям {len(файлы_добавленных)}, в общем диффе "
                           f"{len(добавленные)} — привязка строк к файлам неполна")
    return файлы, добавленные, файлы_добавленных


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
        файлы, добавленные, файлы_добавленных = дифф(base, head)
    except Exception as e:                                   # отказ по умолчанию
        print(f"барьер НЕ состоялся: {e}", file=sys.stderr)
        return 1
    петля, почему = это_петля(ветка, автор_id, ids)
    print(f"автор PR: {автор} (id {автор_id or '—'}), ветка {ветка!r} → {почему}")
    if not ids:
        print("принципал петли НЕ объявлен: судейские пути закрыты для всех, "
              "остальное опознаётся только по имени ветки")
    код, отчёт = проверить(файлы, добавленные, петля,
                           файлы_добавленных=файлы_добавленных,
                           принципалы_известны=bool(ids))
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
        файлы, добавленные, _ = дифф(base, head, cwd=_раб)
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

        # Путь с не-ASCII именем под судейским каталогом: без `-z` git отдавал его
        # в кавычках с восьмеричными escape, и шаблон `^\.github/workflows/` молчал.
        subprocess.run(["git", "config", "core.quotePath", "true"], cwd=_раб, check=True)
        os.makedirs(os.path.join(_раб, ".github", "workflows"), exist_ok=True)
        кавычки = os.path.join(".github", "workflows", "проверка\tвкладка.yml")
        open(os.path.join(_раб, кавычки), "w").write("on: push\n")
        subprocess.run(["git", "add", "-A"], cwd=_раб, check=True)
        subprocess.run(["git", "commit", "-qm", "судья под кириллицей"], cwd=_раб, check=True)
        head2 = git("rev-parse", "HEAD", cwd=_раб).strip()
        сырой = git("diff", "--name-only", f"{head}...{head2}", cwd=_раб).strip()
        check("фикстура честная: без -z git правда печатает путь в кавычках",
              сырой.startswith('"') and судейский(сырой) is None)
        файлы, добавленные, _ = дифф(head, head2, cwd=_раб)
        check("путь с кириллицей и табуляцией читается дословно",
              файлы == [".github/workflows/проверка\tвкладка.yml"])
        код, _ = проверить(файлы, добавленные, True)
        check("правка судьи под не-ASCII именем на ветке петли отвергается", код == 1)

        # Переименование уводит судейский файл из-под шаблона: оба имени обязаны
        # попасть в список, иначе видно только безобидное новое.
        subprocess.run(["git", "mv", кавычки, "docs-вынесено.yml"], cwd=_раб, check=True)
        subprocess.run(["git", "commit", "-qm", "вынос"], cwd=_раб, check=True)
        head3 = git("rev-parse", "HEAD", cwd=_раб).strip()
        файлы, добавленные, _ = дифф(head2, head3, cwd=_раб)
        check("переименование называет и старый, и новый путь",
              sorted(файлы) == sorted([".github/workflows/проверка\tвкладка.yml",
                                       "docs-вынесено.yml"]))
        код, _ = проверить(файлы, добавленные, True)
        check("вынос судейского файла переименованием на ветке петли отвергается",
              код == 1)

        # Сквозной прогон main(): раньше main() не передавал проверить() пути строк,
        # и проверка не-ASCII буквы в коде в бою не исполнялась вовсе — её звал
        # только самотест со списком, собранным руками.
        open(os.path.join(_раб, "омоглиф.py"), "w", encoding="utf-8").write("x = ο\n")
        open(os.path.join(_раб, "плюсы.txt"), "w").write("++x\n")
        subprocess.run(["git", "add", "-A"], cwd=_раб, check=True)
        subprocess.run(["git", "commit", "-qm", "омоглиф"], cwd=_раб, check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD:тема-омоглиф"], cwd=_раб,
                       check=True)
        head4 = git("rev-parse", "HEAD", cwd=_раб).strip()
        файлы, добавленные, пары = дифф(head3, head4, cwd=_раб)
        check("дифф привязывает строку к её файлу", ("омоглиф.py", "x = ο") in пары)
        check("строка «++x» не теряется как заголовок", "++x" in добавленные)
        окружение = dict(os.environ, BASE_SHA=head3, HEAD_SHA=head4,
                         HEAD_REF="einar-fix/омоглиф", PR_AUTHOR_ID="", PR_AUTHOR_LOGIN="t")
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], cwd=_раб,
                           env=окружение, capture_output=True, text=True, timeout=120)
        check("main() ловит не-ASCII букву в коде на ветке петли (выход 1)",
              r.returncode == 1 and "не-ASCII буква" in r.stdout)
        окружение["HEAD_REF"] = "человек/правка"
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], cwd=_раб,
                           env=окружение, capture_output=True, text=True, timeout=120)
        check("тот же дифф вне петли main() пропускает (выход 0)", r.returncode == 0)
    finally:
        _sh.rmtree(_корень, ignore_errors=True)

    print("ИТОГ: " + ("самопроверка пройдена" if ok else "самопроверка ПРОВАЛЕНА"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--self-test" in sys.argv else main())
