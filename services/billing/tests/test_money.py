"""Арифметика 30/45: клиент платит 45, партнёр получает 15, MM-Express 30."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain import PartnerShare, Tier
from app.money import charge, money, pick_tier, split_markup

ZARDAL = "11111111-1111-4111-8111-111111111111"
MANAGER = "22222222-2222-4222-8222-222222222222"


def test_one_operation_costs_45_of_which_15_is_the_partner() -> None:
    """Раздел 1 мастера, дословно: 30 наших + 15 партнёра = 45 у клиента."""
    result = charge(Decimal("1"), Tier(unit_price=Decimal("30")),
                    [PartnerShare(ZARDAL, Decimal("15"))])
    assert (result.amount, result.partner_amount, result.net_amount) == (
        Decimal("45.00"), Decimal("15.00"), Decimal("30.00"))


def test_quantity_multiplies_both_halves() -> None:
    """Поставка из пяти заказов — пять операций, а не одна."""
    result = charge(Decimal("5"), Tier(unit_price=Decimal("30")),
                    [PartnerShare(ZARDAL, Decimal("15"))])
    assert result.amount == Decimal("225.00")
    assert result.partner_amount == Decimal("75.00")
    assert result.net_amount == Decimal("150.00")


def test_without_a_partner_the_client_pays_only_our_tariff() -> None:
    """Наценка — свойство партнёра. Нет партнёра — нет и наценки в счёте."""
    result = charge(Decimal("1"), Tier(unit_price=Decimal("30")))
    assert (result.amount, result.partner_amount) == (Decimal("30.00"), Decimal("0"))


def test_minimum_raises_the_bill_but_not_the_partner_share() -> None:
    """Минимум — наш порог рентабельности, а не повод заплатить партнёру больше."""
    result = charge(Decimal("1"), Tier(unit_price=Decimal("5"), minimum=Decimal("100")),
                    [PartnerShare(ZARDAL, Decimal("15"))])
    assert result.amount == Decimal("100.00")
    assert result.partner_amount == Decimal("15.00")
    assert result.net_amount == Decimal("85.00")


def test_tier_is_chosen_by_volume_and_the_tail_covers_the_rest() -> None:
    tiers = [Tier(unit_price=Decimal("30"), up_to=Decimal("100")),
             Tier(unit_price=Decimal("25"), up_to=Decimal("1000")),
             Tier(unit_price=Decimal("20"))]
    assert pick_tier(tiers, Decimal("50")).unit_price == Decimal("30")
    assert pick_tier(tiers, Decimal("500")).unit_price == Decimal("25")
    assert pick_tier(tiers, Decimal("5000")).unit_price == Decimal("20")


def test_a_version_without_tiers_refuses_instead_of_billing_zero() -> None:
    """Счёт на ноль рублей клиент не оспорит, а выручка исчезнет."""
    with pytest.raises(ValueError):
        pick_tier([], Decimal("1"))


def test_rounding_is_half_up_to_the_kopek() -> None:
    assert money(Decimal("45.005")) == Decimal("45.01")
    assert money(Decimal("0.334")) == Decimal("0.33")


def test_the_split_between_partners_never_loses_a_kopek() -> None:
    """Раскладка обязана сойтись с наценкой: иначе выплата из воздуха.

    Три копейки на двоих не делятся ровно — остаток достаётся тому, кто
    заработал больше, а не теряется в округлении.
    """
    shares = [PartnerShare(ZARDAL, Decimal("0.10")), PartnerShare(MANAGER, Decimal("0.05"))]
    result = charge(Decimal("3"), Tier(unit_price=Decimal("30")), shares)
    split = split_markup(Decimal("3"), shares, result.partner_amount)
    assert sum(amount for _, amount in split) == result.partner_amount


def test_the_manager_can_add_a_markup_of_their_own() -> None:
    """Раздел 13, вопрос 6: допущение «без своей», но модель обязана уметь обе."""
    result = charge(Decimal("1"), Tier(unit_price=Decimal("30")),
                    [PartnerShare(ZARDAL, Decimal("15")), PartnerShare(MANAGER, Decimal("5"))])
    assert result.amount == Decimal("50.00")
    assert result.partner_amount == Decimal("20.00")
    assert result.net_amount == Decimal("30.00")
