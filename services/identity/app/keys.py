"""Откуда identity берёт ключ подписи.

Порядок ровно один и он fail-closed:

  1. `IDENTITY_SIGNING_KEY` (или `_FILE`) — так ключ приходит из
     секрет-провайдера в защищённых средах;
  2. действующая строка `identity_signing_key` в базе — так живёт стенд;
  3. в локальных средах — сгенерировать и записать в базу;
  4. вне локальных сред — отказать на старте.

Четвёртый пункт и есть смысл файла. Сгенерированный на лету ключ в бою
означает, что после перезапуска все выданные токены перестают приниматься, а
на складе это вставшая смена. Молча так делать нельзя.
"""
from __future__ import annotations

import logging

from .config import LOCAL_ENVIRONMENTS, app_environment, read_secret
from .db import Database
from .tokens import SigningKey, generate_key, key_from_pem

log = logging.getLogger("identity.keys")


def load_signing_key(database: Database) -> SigningKey:
    """Действующий ключ. Бросает на старте, если взять его неоткуда."""
    from_env = read_secret("IDENTITY_SIGNING_KEY", required=False)
    if from_env:
        # Переменная выигрывает у базы: в бою ключ приходит снаружи, и база
        # там только зеркало. Записывать его обратно не нужно.
        return key_from_pem(_unfold(from_env))

    stored = _stored(database)
    if stored is not None:
        return stored

    environment = app_environment()
    if environment not in LOCAL_ENVIRONMENTS:
        raise RuntimeError(
            "identity не может подписывать токены: IDENTITY_SIGNING_KEY не задан, "
            "а в базе нет действующего ключа. Сгенерировать его на лету нельзя — "
            "после перезапуска все выданные токены перестанут приниматься, и это "
            "встанет смена на складе. Ключ кладётся секрет-провайдером.")

    key = generate_key()
    _store(database, key)
    log.warning("сгенерирован новый ключ подписи %s: это допустимо только на стенде "
                "(APP_ENV=%s)", key.kid, environment)
    return key


def public_keys(database: Database, current: SigningKey) -> list[SigningKey]:
    """Всё, чем можно проверить живой токен: действующий ключ плюс отозванные,
    пока не истекли выданные ими токены. Иначе ротация выбивает людей из смены."""
    keys = {current.kid: current}
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT kid, private_pem, public_pem FROM identity_signing_key "
            " WHERE retired_at IS NULL OR retired_at > now() - interval '24 hours'")
        for row in cursor.fetchall():
            keys.setdefault(row["kid"], SigningKey(
                kid=row["kid"], private_pem=row["private_pem"], public_pem=row["public_pem"]))
    return list(keys.values())


def _stored(database: Database) -> SigningKey | None:
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT kid, private_pem, public_pem FROM identity_signing_key "
            " WHERE retired_at IS NULL LIMIT 1")
        row = cursor.fetchone()
    if row is None:
        return None
    return SigningKey(kid=row["kid"], private_pem=row["private_pem"],
                      public_pem=row["public_pem"])


def _store(database: Database, key: SigningKey) -> None:
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO identity_signing_key (kid, private_pem, public_pem) "
            "VALUES (%s, %s, %s) ON CONFLICT (kid) DO NOTHING",
            (key.kid, key.private_pem, key.public_pem))


def _unfold(value: str) -> str:
    """PEM в переменной окружения приходит одной строкой: `read_secret`
    запрещает переводы строк, потому что многострочный секрет в env — частый
    способ его исковеркать. Возвращаем форму, которую понимает cryptography."""
    return value.replace("\\n", "\n")
