"""Арифметика денег: тариф MM-Express плюс наценка партнёра.

Модель трёхуровневая наценочная (раздел 1 мастера): MM-Express получает 30 ₽,
партнёр добавляет свою наценку 15 ₽, клиент платит 45 ₽. Это перепродажа услуги
дороже, а не комиссия из наших 30 — поэтому цена и наценка живут раздельно и
складываются здесь, а не хранятся одним числом.

    amount         = quantity × (unit_price + markup)     — что платит клиент
    partner_amount = quantity × markup                    — наценка партнёра
    net_amount     = amount − partner_amount              — выручка MM-Express

Всё в Decimal и с явным округлением: float здесь даёт 45.000000000000004 в
счёте клиенту, и спорить об этом придётся не с компьютером.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

from .domain import Charge, PartnerShare, Tier

CENTS = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    """Округление до копейки. Половина — вверх, как в бухгалтерии, не как в IEEE."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def pick_tier(tiers: Sequence[Tier], quantity: Decimal) -> Tier:
    """Ступень под объём.

    Ступени упорядочены по границе; строка без границы — последняя, «и далее».
    Пустой список ступеней — это версия тарифа без цены, и молча считать её
    нулём нельзя: счёт на ноль рублей клиент не оспорит, а выручка исчезнет.
    """
    bounded = sorted((tier for tier in tiers if tier.up_to is not None),
                     key=lambda tier: tier.up_to or Decimal(0))
    for tier in bounded:
        if tier.up_to is not None and quantity <= tier.up_to:
            return tier
    tail = [tier for tier in tiers if tier.up_to is None]
    if tail:
        return tail[0]
    if bounded:
        # Объём больше последней границы, а «и далее» не задано: считаем по
        # верхней ступени, а не отказываем — иначе крупный клиент не тарифицируется.
        return bounded[-1]
    raise ValueError("у версии тарифа нет ни одной ступени")


def split_markup(quantity: Decimal, shares: Iterable[PartnerShare],
                 partner_amount: Decimal) -> list[tuple[PartnerShare, Decimal]]:
    """Раскладывает наценку по партнёрам без потери копейки.

    Сумма долей обязана совпасть с partner_amount ровно: расхождение — это
    выплата партнёру из воздуха либо недоплата ему же, и триггер
    billing_commission_matches_accrual такую раскладку не пропустит.
    Остаток от округления достаётся тому, кто заработал больше всех.
    """
    listed = [share for share in shares if share.markup > 0]
    if not listed:
        return []
    split = [(share, money(quantity * share.markup)) for share in listed]
    drift = partner_amount - sum(amount for _, amount in split)
    if drift:
        largest = max(range(len(split)), key=lambda index: split[index][1])
        share, amount = split[largest]
        split[largest] = (share, amount + drift)
    return split


def charge(quantity: Decimal, tier: Tier,
           shares: Sequence[PartnerShare] = ()) -> Charge:
    """Считает начисление за операцию.

    Минимум ступени поднимает то, что платит клиент, но не долю партнёра:
    минимальная сумма — это наш порог рентабельности операции, а не повод
    заплатить партнёру больше его ставки. Разница уходит в net_amount.
    """
    if quantity <= 0:
        raise ValueError("количество для начисления должно быть больше нуля")

    markup = sum((share.markup for share in shares), Decimal("0"))
    partner_amount = money(quantity * markup)
    amount = money(quantity * (tier.unit_price + markup))
    if tier.minimum and amount < tier.minimum:
        amount = money(tier.minimum)
    if partner_amount > amount:
        # Наценка не может превышать то, что заплатил клиент (CHECK в схеме).
        # Такое возможно только при нулевой цене и минимуме ниже наценки —
        # тогда клиент платит ровно наценку, а MM-Express не зарабатывает.
        amount = partner_amount

    return Charge(
        quantity=quantity,
        unit_price=tier.unit_price,
        markup=markup,
        amount=amount,
        partner_amount=partner_amount,
        shares=tuple(shares),
    )
