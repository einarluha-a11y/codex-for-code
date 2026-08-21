#!/usr/bin/env python3
"""Статический тест шаблона .github/workflows/codex-review.yml.

Инвариант (Pitfalls P1/P9): чек НЕ должен показывать успех, когда ревью не
выполнялось. Ложный зелёный хуже красного — он молча снимает сомнение.

Архитектура: job `preflight` проверяет секрет и (при отсутствии) публикует
neutral. Job `codex` запускается только при has_key=true — без ключа показывает
skipped (не success).

Проверяем:
  • codex job зависит от preflight и проверяет has_key (не зеленеет без ключа);
  • preflight публикует neutral check-run при отсутствии ключа;
  • docs-only (trivial) → conclusion=neutral, НЕ success;
  • ошибка ревью (else) → conclusion=failure;
  • guardSkip отсутствует в шаге Publish (только в preflight).

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

    # ── (a) job codex зависит от preflight и проверяет has_key ───────────────
    # Без этого codex может зеленеть без реального ревью (false green, P1/P9).
    if "needs.preflight.outputs.has_key" not in text:
        fail(
            "codex job не проверяет needs.preflight.outputs.has_key — "
            "job может зеленеть без ключа (false green, Pitfalls P1/P9)"
        )
    if not re.search(r"needs:\s*\[?\s*preflight", text):
        fail(
            "codex job не имеет 'needs: preflight' — "
            "зависимость от guard-job не установлена"
        )

    # ── (a2) preflight публикует neutral при отсутствии ключа ─────────────────
    # Единственный канал публикации check-run, когда codex job skipped.
    if "Publish neutral check-run" not in text:
        fail(
            "preflight job не имеет шага публикации neutral check-run — "
            "при отсутствии ключа check-run не будет опубликован"
        )

    # Вырезаем тело шага публикации исхода в job codex — единственное место,
    # где решается conclusion для выполнившегося ревью. Проверять весь файл
    # нельзя: слово neutral/success/failure встречается в комментариях.
    step = re.search(
        r"name:\s*Publish codex check-run conclusion(?P<body>.*?)(?:\Z)",
        text,
        re.DOTALL,
    )
    if not step:
        fail("шаг 'Publish codex check-run conclusion' не найден — исход не публикуется через Checks API")
    body = step.group("body")

    # ── (b) guardSkip убран из шага Publish — только в preflight ──────────────
    # Дублирование означало бы, что два места управляют одним исходом.
    if re.search(r"\bguardSkip\b", body):
        fail(
            "guardSkip обнаружен в шаге 'Publish codex check-run conclusion' — "
            "должен быть только в preflight (дублирование)"
        )

    # ── (c) Ветка docs-only (trivial) → conclusion='neutral', НЕ 'success' ───
    # Правило: «ревью не выполнялось → neutral» едино независимо от причины.
    m = re.search(r"if\s*\(\s*trivial\s*\)\s*\{(?P<blk>.*?)\}", body, re.DOTALL)
    if not m:
        fail("ветка trivial (docs-only) не найдена в шаге публикации")
    trivialblk = m.group("blk")
    mc = re.search(r"conclusion\s*=\s*'([a-z]+)'", trivialblk)
    if not mc:
        fail("в ветке trivial не присвоен conclusion")
    if mc.group(1) != "neutral":
        fail(
            f"ветка docs-only даёт conclusion='{mc.group(1)}', ожидается 'neutral' "
            f"(«ревью не выполнялось → neutral», Pitfalls P1/P9)"
        )
    mt = re.search(r"title\s*=\s*'([^']*)'", trivialblk)
    if not mt or not mt.group(1).strip():
        fail("в ветке trivial output.title пустой/отсутствует")
    trivial_title = mt.group(1)
    if "docs-only" not in trivial_title.lower() and "docs" not in trivial_title.lower():
        fail(f"в ветке trivial title не описывает причину docs-only: {trivial_title!r}")
    if "OPENAI_API_KEY" in trivial_title:
        fail(
            f"в ветке trivial title упоминает OPENAI_API_KEY — "
            f"нельзя спутать с отсутствием ключа: {trivial_title!r}"
        )

    # ── (d) Ветка ошибки ревью (финальный else) → conclusion='failure' ────────
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
    if not re.search(r"title\s*=\s*'[^']", errblk):
        fail("в ветке ошибки output.title пустой/отсутствует")

    # ── (e) Успешная ветка → conclusion='success', title непустой ────────────
    m = re.search(r"codexOutcome\s*===\s*'success'\s*\)\s*\{(?P<blk>.*?)\}\s*else", body, re.DOTALL)
    if not m:
        fail("ветка успешного ревью (codexOutcome==='success') не найдена")
    okblk = m.group("blk")
    if not re.search(r"conclusion\s*=\s*'success'", okblk):
        fail("ветка успешного ревью не даёт conclusion='success'")
    if not re.search(r"title\s*=\s*'[^']", okblk):
        fail("в успешной ветке output.title пустой/отсутствует")

    # ── (f) neutral ровно в одной ветке шага Publish (trivial) ───────────────
    if conclusions.count("neutral") != 1:
        fail(
            f"conclusion='neutral' должен быть ровно в одной ветке шага Publish (trivial/docs-only); "
            f"найдено {conclusions.count('neutral')}"
        )

    print("PASS: шаблон codex-review.yml — исход чека честен:")
    print("  • нет ключа  → preflight публикует neutral, codex job skipped")
    print("  • docs-only  → conclusion=neutral, title называет docs-only")
    print("  • ошибка     → conclusion=failure, title непустой")
    print("  • успех      → conclusion=success, title непустой")
    sys.exit(0)


if __name__ == "__main__":
    main()
