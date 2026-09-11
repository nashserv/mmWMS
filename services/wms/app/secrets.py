"""Секрет-провайдер: `secret_ref` → токен Wildberries.

`wms` становится держателем токенов WB, и зона доверия расширяется на складской
сервис (раздел 12). Правило простое: в базе лежит ССЫЛКА, значение живёт в
провайдере и не попадает ни в события, ни в логи, ни в ответы API, ни в тесты
(инвариант 15).

Провайдер fail-closed. В защищённом окружении неразрешённая ссылка — это
ошибка, а не повод сходить в Wildberries без токена: молчаливый анонимный
вызов вернул бы 401, и разбирать пришлось бы не то.

На стенде живых токенов нет вовсе, и это проверяется при каждом старте
(раздел 12, ворота token-guard). Поэтому в локальных окружениях провайдер
выдаёт синтетическую заглушку, заведомо не похожую на JWT: симулятор токен не
проверяет, а перепутать заглушку с настоящим ключом невозможно.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .config import LOCAL_ENVIRONMENTS, app_environment

# Форма живого токена WB — JWT (приложение D). Значение такой формы не имеет
# права появиться там, где ожидается ссылка.
_JWT_SHAPE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.")
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")


class SecretUnavailable(RuntimeError):
    """Ссылку не удалось разрешить. Вызов в Wildberries не состоится."""


class SecretProvider:
    """Разрешает ссылки вида `vault://mmx/wb/<кабинет>` в значение токена.

    Источники по убыванию приоритета:
      1. файл в каталоге `WB_SECRET_DIR` с именем из ссылки;
      2. переменная окружения `WB_TOKEN__<слаг ссылки>`;
      3. синтетическая заглушка — только в test/local/development.
    """

    def __init__(self, *, directory: str | None = None, environment: str | None = None) -> None:
        self._directory = directory if directory is not None else os.getenv("WB_SECRET_DIR", "")
        self._environment = environment or app_environment()

    def resolve(self, secret_ref: str) -> str:
        ref = (secret_ref or "").strip()
        if not ref or not _SAFE_REF.match(ref):
            raise SecretUnavailable("ссылка на секрет пуста или имеет недопустимую форму")
        if _JWT_SHAPE.match(ref):
            # В колонке secret_ref оказалось значение токена. Это уже утечка:
            # база, дампы и бэкапы теперь содержат ключ от кабинета клиента.
            raise SecretUnavailable(
                "в secret_ref лежит значение токена, а не ссылка — вызов запрещён")

        value = self._from_file(ref) or self._from_env(ref)
        if value:
            return value
        if self._environment in LOCAL_ENVIRONMENTS:
            return self._stand_placeholder(ref)
        raise SecretUnavailable("секрет по ссылке не найден в провайдере")

    def _from_file(self, ref: str) -> str | None:
        if not self._directory:
            return None
        path = Path(self._directory) / _slug(ref)
        try:
            if not path.is_file() or path.stat().st_size > 65_536:
                return None
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _from_env(self, ref: str) -> str | None:
        return (os.getenv(f"WB_TOKEN__{_slug(ref).upper()}", "") or "").strip() or None

    @staticmethod
    def _stand_placeholder(ref: str) -> str:
        """Заглушка стенда. Не JWT и не может им стать.

        Симулятор токен не проверяет, различать кабинеты ему нужно заголовком.
        Значение детерминировано по ссылке — чтобы в логах стенда один кабинет
        выглядел одинаково от запуска к запуску.
        """
        digest = hashlib.sha256(f"stand:{ref}".encode()).hexdigest()[:32]
        return f"stand-not-a-token-{digest}"


def _slug(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", ref).strip("_")


_provider: SecretProvider | None = None


def provider() -> SecretProvider:
    global _provider
    if _provider is None:
        _provider = SecretProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None
