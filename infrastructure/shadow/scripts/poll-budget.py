#!/usr/bin/env python3
"""Сколько теневому опросу можно тратить из лимита кабинета.

Раздел 11 прямо предупреждает: в shadow два опросчика бьют в один кабинет, а
лимит Wildberries — 300 запросов в минуту на кабинет, общий (приложение D).
Упереться в него на проде из-за теневого опроса недопустимо: пострадает
боевая работа клиента, а не наша проверка.

Чтобы поделить бюджет, нужно знать долю боевого шлюза. Это **вопрос 11
раздела 13, и он до сих пор открыт**. Скрипт не угадывает её — он считает от
названного числа и показывает, что получится.

    ./poll-budget.py --prod-rpm 120
    ./poll-budget.py --prod-rpm 120 --cabinets 1 --margin 60
"""
from __future__ import annotations

import argparse

# Приложение D: 300 запросов в минуту на кабинет.
WB_LIMIT_RPM = 300

# Критерий раздела 10: задержка «WB → доступность в /tasks/pull» меньше 2 с по
# p99. Опрос реже этого смысла не имеет — проверять будет нечего.
MAX_USEFUL_INTERVAL_S = 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prod-rpm", type=float, required=True,
                        help="сколько запросов в минуту тратит на кабинет БОЕВОЙ шлюз "
                             "(вопрос 11 раздела 13 — спросить владельца или замерить)")
    parser.add_argument("--margin", type=float, default=60.0,
                        help="запас, который не тратит никто (по умолчанию 60/мин). "
                             "Нужен: у WB есть burst и штраф — 409 стоит десяти запросов")
    parser.add_argument("--cabinets", type=int, default=1,
                        help="сколько кабинетов опрашивает shadow (раздел 11: на шаге 2 — один)")
    args = parser.parse_args()

    share = WB_LIMIT_RPM - args.prod_rpm - args.margin
    print(f"Лимит кабинета:            {WB_LIMIT_RPM:6.0f} запросов/мин (приложение D)")
    print(f"Тратит боевой шлюз:        {args.prod_rpm:6.0f}")
    print(f"Запас, не трогает никто:   {args.margin:6.0f}")
    print(f"Остаётся теневому опросу:  {share:6.0f}")
    print()

    if share <= 0:
        print("НЕЛЬЗЯ: боевой шлюз с запасом уже выбирает лимит целиком.")
        print("Теневой опрос отнимет запросы у работы клиента. Прежде чем поднимать")
        print("shadow, придётся либо снизить частоту боевого опроса, либо согласовать")
        print("с владельцем, что он временно станет реже.")
        return 1

    per_cabinet = share / max(1, args.cabinets)
    interval = 60.0 / per_cabinet
    print(f"Кабинетов в shadow:        {args.cabinets:6d}")
    print(f"На кабинет:                {per_cabinet:6.1f} запросов/мин")
    print()
    print(f"  SHADOW_POLL_INTERVAL_SECONDS={interval:.1f}")
    print()

    if interval > MAX_USEFUL_INTERVAL_S:
        print(f"ВНИМАНИЕ: опрос раз в {interval:.1f} с не укладывается в критерий")
        print(f"раздела 10 («WB → доступность меньше {MAX_USEFUL_INTERVAL_S:.0f} с, p99»).")
        print("Само по себе это не запрет: на шаге 2 мы ничего не отгружаем и меряем")
        print("не задержку, а сходятся ли таблицы заданий. Но знать об этом надо —")
        print("и не переносить это число на шаг 5, где задержка уже стоит смены.")
        return 0

    print(f"Укладывается в критерий раздела 10 (меньше {MAX_USEFUL_INTERVAL_S:.0f} с).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
