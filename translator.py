"""
translator.py — перевод текстовых полей результата process() (см. main.py)
на азербайджанский через Microsoft Azure AI Translator.

Почему Azure, а не Google/DeepL — см. обсуждение с заказчиком:
    - DeepL азербайджанский вообще не поддерживает.
    - Google Cloud Translation: бесплатно 500k симв./мес, нужна карта,
      на free tier текст может уходить на улучшение моделей.
    - Azure AI Translator (тариф F0): бесплатно 2 000 000 симв./мес,
      данные НЕ сохраняются и не используются для обучения (см.
      https://learn.microsoft.com/azure/ai-services/translator/data-privacy-security).
      Для накладных с именами компаний/адресами это существенно.

Установка:
    pip install requests

Переменные окружения (обязательны):
    AZURE_TRANSLATOR_KEY      — ключ ресурса Translator из портала Azure
    AZURE_TRANSLATOR_REGION   — регион ресурса, например "westeurope"
Необязательно:
    AZURE_TRANSLATOR_ENDPOINT — по умолчанию api.cognitive.microsofttranslator.com

Как получить ключ: portal.azure.com -> Create a resource -> Translator ->
Pricing tier: Free F0 -> Keys and Endpoint.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Iterable

import requests

ENDPOINT = os.environ.get(
    "AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com")
KEY = os.environ.get("AZURE_TRANSLATOR_KEY")
REGION = os.environ.get("AZURE_TRANSLATOR_REGION")

# Какие поля схемы (см. SCHEMA в main.py) вообще имеет смысл переводить.
# Коды, номера, ISO-значения сюда сознательно НЕ включены — перевод испортит
# данные, а не улучшит их (invoiceNumber, qnqCode, productWeight,
# numberOfPlace, containerNumber, customsClearance, vehicleNumber,
# routeOutCode, routeInCode, destinationDysCode остаются как есть всегда).
#
# exporterName/importerName (юр.названия, адреса) ПО УМОЛЧАНИЮ ТОЖЕ
# переводятся, по явному запросу заказчика — но машинный перевод
# собственных имён/адресов ненадёжен, поэтому такие поля стоит сверять
# вручную, а лучше подключить refs/parties.json (см. main.py) с уже
# готовым каноничным написанием — тогда переводить их не придётся вовсе.
DEFAULT_TRANSLATABLE_FIELDS: tuple[str, ...] = (
    "exporterName", "importerName", "goodsDescription", "destinationDys")


class TranslatorError(RuntimeError):
    """Ошибка обращения к Azure Translator (нет ключа, сеть, ответ не 200)."""


def _check_config() -> None:
    if not KEY or not REGION:
        raise TranslatorError(
            "Не заданы переменные окружения AZURE_TRANSLATOR_KEY / "
            "AZURE_TRANSLATOR_REGION. См. docstring translator.py.")


def translate_batch(texts: list[str], target: str = "az", source: str = "ru") -> list[str]:
    """Переводит список строк ОДНИМ HTTP-запросом (дешевле по квоте и быстрее,
    чем дёргать API по одной строке на каждое поле каждой накладной)."""
    _check_config()
    if not texts:
        return []
    resp = requests.post(
        f"{ENDPOINT}/translate",
        params={"api-version": "3.0", "from": source, "to": target},
        headers={
            "Ocp-Apim-Subscription-Key": KEY,
            "Ocp-Apim-Subscription-Region": REGION,
            "Content-Type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4()),
        },
        json=[{"text": t} for t in texts],
        timeout=15,
    )
    if resp.status_code != 200:
        raise TranslatorError(f"Azure Translator ответил {resp.status_code}: {resp.text}")
    return [item["translations"][0]["text"] for item in resp.json()]


def translate_result(
    data: dict[str, Any],
    target: str = "az",
    fields: Iterable[str] = DEFAULT_TRANSLATABLE_FIELDS,
) -> dict[str, Any]:
    """Возвращает КОПИЮ data (см. result["data"] из process() в main.py) с
    переведёнными указанными полями. Поля с None/пустой строкой пропускаются
    (нечего переводить), остальные ключи остаются как были."""
    out = dict(data)
    keys = [k for k in fields if out.get(k)]
    if not keys:
        return out
    translated = translate_batch([str(out[k]) for k in keys], target=target)
    for k, v in zip(keys, translated):
        out[k] = v
    return out


if __name__ == "__main__":
    # Быстрая проверка: python translator.py "текст для перевода"
    import sys
    if len(sys.argv) < 2:
        print("Usage: python translator.py \"текст\" [target_lang]", file=sys.stderr)
        raise SystemExit(2)
    lang = sys.argv[2] if len(sys.argv) > 2 else "az"
    print(translate_batch([sys.argv[1]], target=lang)[0])