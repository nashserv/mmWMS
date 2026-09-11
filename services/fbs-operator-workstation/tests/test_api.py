"""Собственный API рабочего места и экраны.

Проверяется поведение, которое видит человек за ПК: отказ приезжает кодом, а
не полем в двухсотке; экран не пустеет от одного неудачного опроса; выключенная
шина не делает сервис неготовым.
"""
from __future__ import annotations

import base64
import hashlib

import pytest
from conftest import FakeStore, FakeWms, label_result, pull_result, task_projection
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, wms: FakeWms, store: FakeStore):
    """Приложение целиком, но с подставленными wms и базой.

    Подставляются именно фабрики в модуле: так проверяется настоящий lifespan,
    настоящий опросчик и настоящая маршрутизация, а не их пересборка в тесте.
    """
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WMS_BASE_URL", "http://wms.test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/none")
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    monkeypatch.setenv("WORKSTATION_POLL_INTERVAL_MS", "30000")

    from app import api

    monkeypatch.setattr(api, "WmsClient", lambda *args, **kwargs: wms.client())
    monkeypatch.setattr(api, "Store", lambda *args, **kwargs: store)
    application = api.create_app()
    with TestClient(application) as test_client:
        yield test_client


def test_health_and_readiness_answer_different_questions(client, wms: FakeWms):
    """Порт открыт и работа делается — не одно и то же (раздел 3.5 мастера)."""
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.json()["note"].startswith("готовность определяется опросом")


def test_a_disconnected_bus_does_not_make_the_service_unready(client):
    """Пункт 14 прогона: склад от шины не зависит."""
    body = client.get("/readyz").json()
    assert body["bus_connected"] is False
    assert body["status"] == "ready"


def test_queue_is_served_from_the_projection(client, wms: FakeWms):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    client.post("/api/workstation/v1/poll", json={})
    body = client.post("/api/workstation/v1/tasks", json={}).json()
    assert [task["task_id"] for task in body["tasks"]] == ["a"]
    assert body["full_path"]["on_screen"] == 1


def test_control_scan_mismatch_answers_with_a_refusal_status(client, wms: FakeWms):
    """Экран обязан показать красное и остановиться, а не «показать результат»."""
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    client.post("/api/workstation/v1/poll", json={})
    response = client.post("/api/workstation/v1/pack", json={
        "task_id": "a", "control_barcode": "9999999999999", "actor_id": "Иванов"})
    assert response.status_code == 409
    assert response.json()["stop"] is True


def test_cancel_without_a_comment_is_rejected_by_validation(client):
    response = client.post("/api/workstation/v1/cancel", json={
        "task_id": "a", "reason_code": "operator_short", "comment": "",
        "actor_id": "Иванов"})
    assert response.status_code == 422, "пустая причина не должна доходить до логики"


def test_agent_stats_expose_the_number_the_full_run_needs(client):
    """PRINT_AGENT_STATS_URL шага 10: {"task_id", "last_write_ms"}."""
    body = client.get("/agent/stats").json()
    assert "task_id" in body and "last_write_ms" in body


def test_metrics_carry_the_full_path_numbers(client, wms: FakeWms):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    client.post("/api/workstation/v1/poll", json={})
    payload = client.get("/metrics").text
    assert "mmx_workstation_tasks_on_screen" in payload
    assert "mmx_workstation_worker_processed_total" in payload
    assert "mmx_service_ready" in payload


def test_every_screen_renders(client):
    for path in ("/", "/receiving", "/putaway", "/inventory", "/shipping", "/supervisor"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "<!doctype html>" in response.text.lower()


def test_picklist_page_carries_a_scannable_barcode(client, wms: FakeWms,
                                                   store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picking")))
    session = client.post("/api/workstation/v1/sessions",
                          json={"actor_id": "Иванов", "limit": 5}).json()
    page = client.get(f"/picklist/{session['session_id']}")
    assert page.status_code == 200
    assert "<svg" in page.text, "лист без штрихкода не вернуть к своей сессии"
    assert session["picklist_barcode"] in page.text
    assert "A-01-01" in page.text, "адрес обязан быть напечатан"


def test_an_unknown_picklist_says_so_instead_of_showing_an_empty_sheet(client):
    assert client.get("/picklist/нет-такой").status_code == 404


def test_supervisor_screen_reads_the_valve_instead_of_saying_it_cannot(
        client, wms: FakeWms):
    """Клапан «собрать без остатка» читается маршрутом, а не объясняется словами.

    Раньше блок честно говорил «прочитать нечем»: `ledger_short` рождается на
    резерве, `receipt_id` у него пуст, и в `/receipts/screen` он не попадал
    никогда. Маршрут `/discrepancies` появился в потоке 0 — и блок обязан
    показывать случаи, а не заявку на маршрут.
    """
    wms.on("/receipts/screen", lambda params: {"receipts": [],
                                               "generated_at": "2026-09-10T10:00:00+00:00"})
    wms.on("/discrepancies", lambda params: {
        "discrepancies": [{"discrepancy_id": "d-1", "kind": "ledger_short",
                           "owner_external_id": "seller-1", "qty": 2,
                           "decision": "pending"}],
        "generated_at": "2026-09-10T10:00:00+00:00"})

    body = client.post("/api/workstation/v1/supervisor/screen", json={}).json()

    ledger = body["ledger_short"]
    assert ledger["available"] is True, (
        f"клапан по-прежнему не читается: {ledger.get('reason')}")
    assert [item["discrepancy_id"] for item in ledger["items"]] == ["d-1"]
    assert body["cancellations"]["without_reason"] == 0

    asked = [params for route, params in wms.calls if route == "/discrepancies"]
    assert asked and asked[0]["kinds"] == ["ledger_short"], (
        "экран просит не тот вид расхождений")


def test_a_silent_wms_does_not_look_like_a_quiet_shift(client, wms: FakeWms):
    """Ложное «всё хорошо» опаснее честного «проверить нечем» (инвариант 12)."""
    wms.on("/receipts/screen", lambda params: {"receipts": [],
                                               "generated_at": "2026-09-10T10:00:00+00:00"})
    wms.status_code = 503

    body = client.post("/api/workstation/v1/supervisor/screen", json={}).json()

    ledger = body["ledger_short"]
    assert ledger["available"] is False
    assert ledger["items"] == []
    assert "wms" in ledger["reason"].lower(), (
        "пустой блок без объяснения читается как «таких случаев не было»")


def test_probe_confirmation_is_recorded_with_who_saw_what(client, store: FakeStore):
    response = client.post("/api/workstation/v1/print/probe/confirm", json={
        "station_id": "11111111-1111-4111-8111-111111111111", "confirmed_format": "zplv",
        "note": "из XP-420B вышла этикетка ZPL OK"})
    assert response.status_code == 200
    assert store.printers_by_id["11111111-1111-4111-8111-111111111111"]["confirmed_format"] == "zplv"
    assert store.printers_by_id["11111111-1111-4111-8111-111111111111"]["probed_at"]


def test_probe_confirmation_refuses_an_invented_format(client):
    response = client.post("/api/workstation/v1/print/probe/confirm", json={
        "station_id": "11111111-1111-4111-8111-111111111111", "confirmed_format": "лазерный"})
    assert response.status_code == 422


def test_print_agent_socket_registers_and_reports_a_write(client, wms: FakeWms,
                                                          store: FakeStore):
    """Полный путь push: агент подключился, получил этикетку, отчитался о записи."""
    payload = b"^XA^FDlabel^FS^XZ"
    wms.on("/labels/a/print", lambda params: dict(
        label_result(payload), task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    client.post("/api/workstation/v1/poll", json={})

    station = "11111111-1111-4111-8111-111111111111"
    with client.websocket_connect("/api/workstation/v1/agent") as socket:
        socket.send_json({"type": "hello", "station_id": station,
                          "station_name": "Станция 1", "printer_name": "XP-420B"})
        assert socket.receive_json()["type"] == "welcome"

        import threading

        answer: dict = {}

        def do_print() -> None:
            answer["response"] = client.post("/api/workstation/v1/print", json={
                "task_id": "a", "station_id": station, "actor_id": "Иванов"})

        worker = threading.Thread(target=do_print)
        worker.start()
        job = socket.receive_json()
        assert job["type"] == "print"
        assert base64.b64decode(job["payload_b64"]) == payload
        socket.send_json({"type": "result", "job_id": job["job_id"], "task_id": "a",
                          "ok": True, "write_ms": 4.2, "format": "zplv"})
        worker.join(timeout=10)

    result = answer["response"].json()
    assert result["ok"] is True and result["agent_write_ms"] == 4.2
    assert client.get("/agent/stats").json()["last_write_ms"] == 4.2
    assert store.printers_by_id[station]["station_name"] == "Станция 1"


def test_an_agent_without_a_station_id_is_turned_away(client):
    with client.websocket_connect("/api/workstation/v1/agent") as socket:
        socket.send_json({"type": "hello", "station_name": "ничья станция"})
        assert socket.receive_json()["type"] == "error"


def test_status_names_the_contract_violation_when_it_happens(client, wms: FakeWms):
    from app.puller import SCREEN_ASSIGNEE

    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee=SCREEN_ASSIGNEE, state="picking")))
    client.post("/api/workstation/v1/poll", json={})
    status = client.post("/api/workstation/v1/status", json={}).json()
    assert status["poller"]["claim_ignored"] is True
    assert "claim: false" in status["poller"]["claim_ignored_note"]


def test_checksum_helper_is_used_on_the_print_path():
    payload = b"^XA^XZ"
    from app.wms_client import decode_label_payload
    body, _how = decode_label_payload(base64.b64encode(payload).decode(),
                                      hashlib.sha256(payload).hexdigest())
    assert body == payload


def test_a_found_paper_sheet_returns_to_its_session(client, wms: FakeWms):
    """Лист со склада ищется по штрихкоду, а не по идентификатору сессии.

    Маршрут объявлен до `/sessions/{session_id}`: шаблон с параметром съедал
    «by-barcode» как идентификатор, и сканер листа переставал работать.
    """
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picking")))
    session = client.post("/api/workstation/v1/sessions",
                          json={"actor_id": "Иванов", "limit": 5}).json()
    found = client.post("/api/workstation/v1/sessions/by-barcode",
                        json={"picklist_barcode": session["picklist_barcode"]})
    assert found.status_code == 200
    assert found.json()["session"]["id"] == session["session_id"]


def test_an_unknown_paper_sheet_says_so(client):
    response = client.post("/api/workstation/v1/sessions/by-barcode",
                           json={"picklist_barcode": "PL-нет-такого"})
    assert response.status_code == 404


# --------------------------------------------------- название товара как код

def test_a_product_name_cannot_become_code_on_the_screen() -> None:
    """Название товара пишет продавец, а экран подставляет его в разметку.

    Товар с именем `<img src=x onerror=...>` выполнял бы этот код в браузере
    сборщика — в браузере, у которого открыт весь склад: выдача заданий,
    отмена, печать.
    """
    from app import screens

    source = screens.PICKING_JS + screens.RECEIVING_JS + screens.PUTAWAY_JS

    assert "const esc = (value) =>" in screens.BASE_JS, (
        "в экранах нет экранирования вовсе")
    assert "esc(value)" in screens.BASE_JS, "text() не экранирует значение"

    # Атрибут, собранный конкатенацией, — то же отверстие, что и innerHTML:
    # кавычка в значении закрывает атрибут, и остальное становится разметкой.
    assert 'value="' + "' + " not in source, (
        "значение подставляется в атрибут value строкой — кавычка в названии "
        "товара закроет атрибут")
    assert "onclick=\"pick(" not in source, (
        "объект товара уезжает в атрибут onclick: его поля становятся кодом")


def test_escaping_covers_the_characters_that_matter() -> None:
    """Экранируются и кавычки: значения подставляются и в текст, и в атрибуты."""
    from app import screens

    body = screens.BASE_JS
    for character in ("'&'", "'<'", "'>'", "'\"'", '"\'"'):
        assert character in body, f"не экранируется {character}"


# --- кто на самом деле берёт задания ----------------------------------------
#
# Раздел 13, пункт 13. Имя с экрана разворачивается в постоянный uuid5:
# «за кем задание» воспроизводимо, но ни с кем в identity это значение не
# связано — по нему нельзя ни спросить человека, ни отозвать ему доступ.
# Токен сборщика отвечает на тот же вопрос по-настоящему.

def test_a_pickers_token_beats_the_name_typed_on_the_screen(client, monkeypatch) -> None:
    """Пришёл со своим токеном — задание записано на него, а не на набранное.

    До фикса `actor_id` брался из тела запроса всегда, и человек мог назваться
    кем угодно, включая коллегу.
    """
    from app import api as api_module

    monkeypatch.setattr(api_module.auth, "caller_from",
                        lambda headers, allow_service=False: api_module.auth.Caller(
                            subject="11111111-2222-3333-4444-555555555555", roles=()))

    seen: dict[str, str] = {}

    async def remember(**kwargs):
        seen.update({"actor": kwargs.get("actor_id")})
        return {"session_id": "s-1", "tasks": []}

    monkeypatch.setattr(api_module.state.picks, "start_session", remember)

    answer = client.post("/api/workstation/v1/sessions",
                         json={"actor_id": "Вася с экрана", "limit": 5},
                         headers={"authorization": "Bearer a-pickers-token"})

    assert answer.status_code == 200, answer.text[:200]
    assert seen["actor"] == "11111111-2222-3333-4444-555555555555", (
        "задание записано на имя с экрана при наличии токена сборщика — "
        "«кто это сделал» по-прежнему без ответа")


def test_without_a_token_the_typed_name_is_used_and_counted(client, monkeypatch) -> None:
    """Без токена имя с экрана по-прежнему работает, но считается отдельно.

    Смена не должна вставать из-за того, что входа ещё нет. Но доля таких смен
    обязана быть видна: это доля работы, про которую нельзя сказать, кто её
    сделал.
    """
    from app import api as api_module

    def refuse(headers, allow_service=False):
        raise api_module.auth.AuthError("токена нет")

    monkeypatch.setattr(api_module.auth, "caller_from", refuse)

    seen: dict[str, str] = {}

    async def remember(**kwargs):
        seen.update({"actor": kwargs.get("actor_id")})
        return {"session_id": "s-2", "tasks": []}

    monkeypatch.setattr(api_module.state.picks, "start_session", remember)

    answer = client.post("/api/workstation/v1/sessions",
                         json={"actor_id": "Вася с экрана", "limit": 5})

    assert answer.status_code == 200, answer.text[:200]
    assert seen["actor"] == "Вася с экрана"
    # Метрика обязана различать два пути: без неё «все смены анонимны» и «все
    # смены подписаны» выглядят одинаково.
    assert api_module.metrics.PICKER_IDENTIFIED.labels(how="имя с экрана")._value.get() > 0
