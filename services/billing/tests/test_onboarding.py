"""Онбординг клиента одним потоком — без единого ручного запроса в базу.

Сегодня 21 кабинет WB заведён при 4 записях в sellers (раздел 3.6): это следы
ручной заводки в трёх местах, которая и должна исчезнуть.
"""
from __future__ import annotations

import base64

import uuid
from datetime import date
from typing import Any

import pytest

from app.admin import Admin, OnboardingError, check_secret_ref
from app.db import Database

from conftest import rows

# Строка формы живого JWT — нужна, чтобы проверить, что биллинг такую не примет.
# Строка формы JWT собирается здесь, а не лежит в файле литералом.
#
# Раньше литерал был, а ворота check-no-live-tokens.sh пропускали его из-за
# слова `placeholder` в комментарии на той же строке. Это и была дыра: ровно
# так же прошёл бы НАСТОЯЩИЙ токен, если рядом окажется любое из слов-заглушек.
# Ворота теперь смотрят на значение целиком, и правильный ответ — не подбирать
# комментарий, а не держать в репозитории строк, похожих на живой секрет.
def _shaped_like_a_token() -> str:
    """Три сегмента base64url через точку — форма JWT, но не токен."""
    parts = (b'{"alg":"HS256"}', b'{"sub":"synthetic"}', b"not-a-signature")
    return ".".join(base64.urlsafe_b64encode(part).decode().rstrip("=") for part in parts)


SHAPED_LIKE_A_TOKEN = _shaped_like_a_token()


class FakeWms:
    """Поток A ещё не готов — работаем против контракта (правило 9.5.4)."""

    def __init__(self, owner_id: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.owner_id = owner_id or str(uuid.uuid4())

    def ensure_owner(self, seller_external_id: str, name: str, inn: str | None) -> dict[str, Any]:
        self.calls.append(("ensure_owner", {"seller": seller_external_id, "name": name}))
        return {"seller": {"owner_id": self.owner_id, "seller_external_id": seller_external_id}}

    def connect_wb_account(self, external_id: str, seller_external_id: str,
                           display_name: str, secret_ref: str) -> dict[str, Any]:
        self.calls.append(("connect_wb_account", {"external_id": external_id,
                                                  "secret_ref": secret_ref}))
        return {"status": "ok"}

    def ensure_product(self, seller_external_id: str, barcode: str,
                       seller_sku: str | None = None, name: str | None = None) -> dict[str, Any]:
        self.calls.append(("ensure_product", {"barcode": barcode}))
        return {"barcode": barcode}

    def load_opening_stock(self, seller_external_id: str, reference: str,
                           lines: list[dict[str, Any]], warehouse_code: str = "RUM",
                           comment: str = "") -> dict[str, Any]:
        self.calls.append(("load_opening_stock", {"reference": reference, "lines": len(lines)}))
        return {"state": "applied", "reference": reference}


def test_a_secret_ref_is_a_link_to_a_secret_and_never_the_secret() -> None:
    """Инвариант 15: токен, доехавший до базы, уже утёк, даже если его удалить."""
    assert check_secret_ref("vault://mmx/wb/stand-001") == "vault://mmx/wb/stand-001"
    with pytest.raises(OnboardingError, match="живой токен"):
        check_secret_ref(SHAPED_LIKE_A_TOKEN)
    with pytest.raises(OnboardingError):
        check_secret_ref("")
    with pytest.raises(OnboardingError, match="не похож на ссылку"):
        check_secret_ref("просто-строка-без-схемы-провайдера")


def test_onboarding_runs_the_whole_chain_in_one_call(database: Database) -> None:
    """Договор → тариф → партнёр → кабинет WB → владелец в wms → начальный остаток."""
    partner_id = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, name) VALUES (%s, 'Зардал')", (partner_id,))
        cursor.execute(
            "INSERT INTO billing_tariff (id, code, service, name, unit, is_default) "
            "VALUES (gen_random_uuid(), 'packing-default', 'packing', 'Упаковка', 'шт', true) "
            "RETURNING id")
        tariff_id = str(cursor.fetchone()["id"])

    wms = FakeWms()
    result = Admin(database, wms=wms).onboard(  # type: ignore[arg-type]
        seller_external_id="stand-seller-001", name="ИП Тест", inn="9990000001",
        contract_reference="ДГ-2026-001", partner_id=partner_id,
        wb_account_external_id="stand-wb-001", secret_ref="vault://mmx/wb/stand-001",
        tariffs={"packing": tariff_id},
        opening_stock=[{"barcode": "4600000000017", "quantity": 10, "cell_address": "FR-01-01",
                        "state": "good"}],
        from_date=date(2026, 9, 1))

    assert result["state"] == "ok", result["steps"]
    steps = {step["step"]: step for step in result["steps"]}
    assert set(steps) == {"договор", "тарифы", "партнёр", "владелец в wms", "кабинет WB",
                          "начальный остаток"}
    assert steps["кабинет WB"]["token"] == "не передавался"

    cabinet = rows(database, "SELECT * FROM cabinet")[0]
    assert cabinet["needs_onboarding"] is False
    assert str(cabinet["wms_owner_id"]) == wms.owner_id, (
        "кабинет не запомнил владельца склада — события физического действия "
        "его не найдут")
    assert rows(database, "SELECT * FROM billing_contract")[0]["reference"] == "ДГ-2026-001"
    assert rows(database, "SELECT * FROM cabinet_assignment")[0]["role"] == "account_manager"
    assert rows(database, "SELECT * FROM billing_tariff_assignment")[0]["service"] == "packing"

    account = rows(database, "SELECT * FROM cabinet_wb_account")[0]
    assert account["secret_ref"] == "vault://mmx/wb/stand-001"
    assert [name for name, _ in wms.calls] == [
        "ensure_owner", "connect_wb_account", "ensure_product", "load_opening_stock"]


def test_onboarding_with_a_live_token_stops_before_touching_anything(
        database: Database) -> None:
    """Отказ до записи: половина заведённого клиента хуже, чем ничего."""
    with pytest.raises(OnboardingError, match="живой токен"):
        Admin(database, wms=FakeWms()).onboard(  # type: ignore[arg-type]
            seller_external_id="stand-seller-002", name="ИП Тест 2", inn=None,
            contract_reference="ДГ-2026-002", partner_id=None,
            wb_account_external_id="stand-wb-002", secret_ref=SHAPED_LIKE_A_TOKEN)
    assert rows(database, "SELECT * FROM cabinet") == []


def test_when_wms_is_down_onboarding_says_which_step_to_repeat(database: Database) -> None:
    """Вызовы в wms идут вне транзакции (инвариант 2), поэтому откатить их нечем.

    Значит, честный ответ — «сделано столько-то, повторить с такого шага», а не
    молчаливый успех и не потерянный клиент.
    """
    from app.wms_client import WmsUnavailable

    class DeadWms(FakeWms):
        def ensure_owner(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise WmsUnavailable("/sellers: connection refused")

    result = Admin(database, wms=DeadWms()).onboard(  # type: ignore[arg-type]
        seller_external_id="stand-seller-003", name="ИП Тест 3", inn=None,
        contract_reference="ДГ-2026-003", partner_id=None)

    assert result["state"] == "incomplete"
    failed = [step for step in result["steps"] if step["state"] == "ошибка"]
    assert failed and failed[0]["step"] == "владелец в wms"
    # Клиент в биллинге уже есть: повтор онбординга идемпотентен по договору.
    assert rows(database, "SELECT * FROM cabinet")[0]["seller_external_id"] == "stand-seller-003"


def test_onboarding_twice_does_not_duplicate_the_client(database: Database) -> None:
    """Повтор — обычное дело: сеть моргнула, человек нажал второй раз.

    С партнёром, а не без: закрепление кабинета — как раз то место, где
    повтор в тот же день упирается в ограничение «два партнёра на один день».
    """
    partner_id = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, name) VALUES (%s, 'Зардал')", (partner_id,))

    admin = Admin(database, wms=FakeWms())  # type: ignore[arg-type]
    for _ in range(2):
        result = admin.onboard(seller_external_id="stand-seller-004", name="ИП Тест 4", inn=None,
                               contract_reference="ДГ-2026-004", partner_id=partner_id)
        assert result["state"] == "ok", result["steps"]

    assert len(rows(database, "SELECT * FROM cabinet")) == 1
    assert len(rows(database, "SELECT * FROM billing_contract")) == 1
    assert len(rows(database, "SELECT * FROM cabinet_assignment")) == 1
