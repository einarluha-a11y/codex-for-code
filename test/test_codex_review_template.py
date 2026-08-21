#!/usr/bin/env python3
"""Статический тест шаблона .github/workflows/codex-review.yml.

Инвариант (Pitfalls P1/P9): чек НЕ должен показывать успех, когда ревью не
выполнялось. Ложный зелёный хуже красного — он молча снимает сомнение.

Проверяем ветки шага «Publish codex check-run conclusion»:
  • нет OPENAI_API_KEY (guardSkip) → conclusion=neutral (НЕ success);
  • ошибка ревью (else, codexOutcome≠success) → conclusion=failure;
  • output.title непустой во всех ветках.

Только stdlib, Python 3.9+. Regex по сырому тексту — надёжнее yaml.safe_load
для встроенного в github-script JS. Exit 0 = PASS, ≠0 = FAIL.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / ".github" / "workflows" / "codex-review.yml"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    if not TEMPLATE.exists():
        fail(f"шаблон не найден: {TEMPLATE}")

    text = TEMPLATE.read_text(encoding="utf-8")

    # Вырезаем тело шага публикации исхода — единственное место, где решается
    # conclusion. Проверять весь файл нельзя: слово neutral/success/failure
    # встречается в комментариях, что дало бы ложный проход.
    step = re.search(
        r"name:\s*Publish codex check-run conclusion(?P<body>.*?)(?:\Z)",
        text,
        re.DOTALL,
    )
    if not step:
        fail("шаг 'Publish codex check-run conclusion' не найден — исход не публикуется через Checks API")
    body = step.group("body")

    # (b) Ветка «нет ключа» (guardSkip) → conclusion='neutral', НЕ 'success'.
    m = re.search(r"if\s*\(\s*guardSkip\s*\)\s*\{(?P<blk>.*?)\}", body, re.DOTALL)
    if not m:
        fail("ветка guardSkip (нет OPENAI_API_KEY) не найдена")
    nokey = m.group("blk")
    mc = re.search(r"conclusion\s*=\s*'([a-z]+)'", nokey)
    if not mc:
        fail("в ветке guardSkip не присвоен conclusion")
    if mc.group(1) != "neutral":
        fail(f"ветка «нет ключа» даёт conclusion='{mc.group(1)}', ожидается 'neutral' "
             f"(false green — Pitfalls P1/P9)")
    mt = re.search(r"title\s*=\s*'([^']*)'", nokey)
    if not mt or not mt.group(1).strip():
        fail("в ветке guardSkip output.title пустой/отсутствует")
    if "OPENAI_API_KEY not set in repository secrets" not in mt.group(1):
        fail(f"в ветке guardSkip title не описывает причину: {mt.group(1)!r}")

    # (c) Ветка ошибки ревью (финальный else) → conclusion='failure'.
    #     Берём последнее присвоение conclusion в теле (ветка else идёт последней).
    conclusions = re.findall(r"conclusion\s*=\s*'([a-z]+)'", body)
    if "failure" not in conclusions:
        fail(f"ни одна ветка не даёт conclusion='failure'; найдено: {conclusions}")
    m = re.search(r"\}\s*else\s*\{(?P<blk>.*?)await\s+github", body, re.DOTALL)
    if not m:
        fail("финальная ветка else (ошибка ревью) не найдена")
    errblk = m.group("blk")
    mc = re.search(r"conclusion\s*=\s*(?:'([a-z]+)'|([A-Za-z_]\w*))", errblk)
    if not mc:
        fail("в ветке ошибки не присвоен conclusion")
    err_conc = mc.group(1)
    if err_conc != "failure":
        fail(f"ветка ошибки ревью даёт conclusion='{err_conc}', ожидается 'failure'")
    # (d) output.title непустой в ветке ошибки (собирается из литерала + переменной).
    if not re.search(r"title\s*=\s*'[^']", errblk):
        fail("в ветке ошибки output.title пустой/отсутствует")

    # (d) Успешная ветка тоже несёт непустой title и conclusion='success'.
    m = re.search(r"codexOutcome\s*===\s*'success'\s*\)\s*\{(?P<blk>.*?)\}\s*else", body, re.DOTALL)
    if not m:
        fail("ветка успешного ревью (codexOutcome==='success') не найдена")
    okblk = m.group("blk")
    if not re.search(r"conclusion\s*=\s*'success'", okblk):
        fail("ветка успешного ревью не даёт conclusion='success'")
    if not re.search(r"title\s*=\s*'[^']", okblk):
        fail("в успешной ветке output.title пустой/отсутствует")

    # Проверяем, что каждая ветка (кроме trivial) присваивает conclusion,
    # и что neutral появляется ровно в guardSkip.
    if conclusions.count("neutral") != 1:
        fail(f"conclusion='neutral' должен быть ровно в одной ветке (guardSkip); найдено {conclusions.count('neutral')}")

    print("PASS: шаблон codex-review.yml — исход чека честен:")
    print("  • нет ключа  → conclusion=neutral, title непустой")
    print("  • ошибка     → conclusion=failure, title непустой")
    print("  • успех      → conclusion=success, title непустой")
    sys.exit(0)


if __name__ == "__main__":
    main()
