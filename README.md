# OCR_task — извлечение данных СМГС-накладной в JSON

Скрипт распознаёт железнодорожную накладную СМГС (PDF-скан) и превращает её
в структурированный JSON с полями, нужными для декларирования в Gömrük
(азербайджанской таможне). Доступен как CLI-скрипт (`main.py`) и как
HTTP-сервис (`api.py`, FastAPI) с опциональным переводом текстовых полей на
азербайджанский (`translator.py`).

Ключевая идея: поля ищутся не по фиксированным координатам бланка, а по
**якорям** — подписям граф («Вагон», «К-во мест», «ГНГ» и т.д.), которые
одинаковы во всех вариантах формы СМГС («Дорожная ведомость» / «Оригинал
накладной» и др.). Поэтому вёрстка конкретного бланка не имеет значения.
Найденные значения дополнительно проверяются контрольными разрядами
(вагон, код ЕСР, контейнер ISO 6346), а не берутся на веру от OCR.

## Возможности

- Полностраничный OCR (Tesseract, `rus+eng`) с координатами слов, обработка
  постранично полосами в несколько потоков.
- Деskew и нормализация масштаба скана перед распознаванием.
- Проверка контрольных разрядов: номер вагона, код станции ЕСР, номер
  контейнера (ISO 6346) — с автоматическим «ремонтом» одной ошибочно
  распознанной цифры, если это восстанавливает верную контрольную сумму.
- Извлекаемые поля (см. `SCHEMA` в `main.py`):
  `invoiceNumber, exporterName, importerName, qnqCode, goodsDescription,
  productWeight, numberOfPlace, containerNumber, customsClearance,
  vehicleNumber, routeOutCode, routeInCode, destinationDys,
  destinationDysCode`.
- Для каждого поля отдаётся уверенность (`_meta[field].conf`) и источник
  (`_meta[field].src`), а также список полей, требующих ручной проверки
  (`_review`) — если поле не найдено или уверенность ниже 0.85.
- Пакетная обработка нескольких файлов, экспорт в CSV.
- HTTP API (FastAPI): один файл или батч, опциональный перевод
  текстовых полей через Azure AI Translator.

## Структура репозитория

```
main.py                  CLI: OCR-скрипт, извлечение полей, экспорт в CSV
api.py                   FastAPI-обёртка над main.py (эндпоинты /extract, /extract/batch)
translator.py            Перевод текстовых полей результата через Azure AI Translator
requirements-api.txt     Зависимости API-слоя + зависимости main.py
refs/                    Справочники заказчика (см. ниже) — в .gitignore, не хранятся в репо
```

## Установка

Требуется Python 3.10+ и системный Tesseract OCR с русским языковым пакетом.

```bash
# системный Tesseract
# Linux (Debian/Ubuntu):
sudo apt install tesseract-ocr tesseract-ocr-rus
# Windows: Tesseract UB Mannheim (при установке отметить Russian)

# Python-зависимости
pip install -r requirements-api.txt
```

Для запуска только CLI-скрипта без API-слоя достаточно:

```bash
pip install pytesseract opencv-python numpy pymupdf rapidfuzz
```

## Справочники (`refs/`)

Все бизнес-специфичные данные (коды станций ЕСР, реестр контрагентов, коды
ГНГ конкретной номенклатуры) намеренно вынесены из кода в файлы
`./refs/*.json`, которые не попадают в репозиторий (`.gitignore`). Без них
скрипт продолжает работать, но зависящие поля останутся пустыми — это
ожидаемое поведение, видимое через `_review`, а не тихий баг.

| Файл                 | Назначение                                                       | Влияет на поля |
|----------------------|-------------------------------------------------------------------|----------------|
| `refs/esr.json`      | код ЕСР → `[название станции, ISO alpha-2 страны]` (~12k кодов, Тарифное руководство №4) | `routeOutCode`, `routeInCode`, `destinationDys(Code)` |
| `refs/parties.json`  | нормализованное имя контрагента → каноничное имя/юр. адрес       | `exporterName`, `importerName` |
| `refs/gng_az.json`   | код ГНГ → перевод наименования груза на азербайджанский          | `goodsDescription` (при переводе) |

Путь к каталогу справочников можно переопределить переменной окружения
`SMGS_REFS_DIR` (по умолчанию — `./refs` рядом с `main.py`).

Домашняя страна для определения `destinationDys` задаётся переменной
`SMGS_HOME_COUNTRY` (по умолчанию `AZ`).

## Использование: CLI

```bash
python main.py накладная.pdf
python main.py *.pdf --csv out.csv
python main.py накладная.pdf --debug   # печать confidence/источника по каждому полю в stderr
```

Для каждого файла в stdout выводится JSON с распознанными полями, в stderr —
время обработки и список полей на ручную проверку. С флагом `--csv` все
результаты дополнительно сохраняются в один CSV-файл (включая столбец
`_review`).

## Использование: HTTP API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Swagger UI: `http://localhost:8000/docs`

### `POST /extract`

Один PDF-файл → один JSON-объект по схеме Gömrük.

```bash
curl -X POST "http://localhost:8000/extract" \
  -F "file=@накладная.pdf"
```

Параметры запроса:

- `include_meta` (bool, по умолчанию `false`) — добавить в ответ `_meta`
  (уверенность/источник по каждому полю), `_review` и `_seconds`.
- `translate` (строка, напр. `az`) — перевести текстовые поля
  (`exporterName`, `importerName`, `goodsDescription`, `destinationDys`)
  через Azure AI Translator. Числовые и кодовые поля переводу не подлежат
  и возвращаются как есть.

### `POST /extract/batch`

Несколько файлов за один запрос → список объектов (порядок соответствует
порядку файлов). Не-PDF файлы и файлы с ошибкой обработки помечаются полем
`error`, не прерывая обработку остальных.

```bash
curl -X POST "http://localhost:8000/extract/batch" \
  -F "files=@1.pdf" -F "files=@2.pdf"
```

### `GET /health`

Проверка живости сервиса.

## Перевод текстовых полей (`translator.py`)

Перевод выполняется через **Azure AI Translator** (тариф F0): бесплатно
2 000 000 символов/мес, данные не сохраняются и не используются для
обучения моделей — это существенно для накладных с именами компаний и
адресами. DeepL исключён — не поддерживает азербайджанский; Google Cloud
Translation требует привязку карты и на бесплатном тарифе может
использовать текст для улучшения моделей.

Переменные окружения:

```bash
export AZURE_TRANSLATOR_KEY=<ключ ресурса Translator>
export AZURE_TRANSLATOR_REGION=<регион ресурса, напр. westeurope>
# опционально:
export AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
```

Ключ выдаётся на [portal.azure.com](https://portal.azure.com): Create a
resource → Translator → Pricing tier: Free F0 → Keys and Endpoint.

Точечная проверка без API:

```bash
python translator.py "текст для перевода" az
```

Примечание: `exporterName`/`importerName` — юридические названия и адреса,
машинный перевод которых ненадёжен; такие поля стоит сверять вручную или
(лучше) подключить `refs/parties.json` с уже готовым каноничным
написанием — тогда переводить их не потребуется вовсе.

## Формат ответа

```json
{
  "invoiceNumber": 1118325,
  "exporterName": "...",
  "importerName": "...",
  "qnqCode": 73051100,
  "goodsDescription": "...",
  "productWeight": 42257,
  "numberOfPlace": 4,
  "containerNumber": null,
  "customsClearance": 4,
  "vehicleNumber": 66142530,
  "routeOutCode": null,
  "routeInCode": null,
  "destinationDys": null,
  "destinationDysCode": null
}
```

При `include_meta=true` (API) или `--debug` (CLI) дополнительно доступны:

- `_meta.<field>.conf` — уверенность в значении (0–1);
- `_meta.<field>.src` — как именно поле было найдено (какой якорь/проверка);
- `_review` — список полей, требующих ручной проверки (не найдены или
  `conf < 0.85`). Обычно это признак того, что не заполнен нужный
  справочник в `refs/` (см. выше), а не ошибка OCR.

## Известные ограничения

- Полагается на качество скана: сильно смазанные или наклонённые печати
  поверх текста (штампы, подписи) могут снижать уверенность отдельных полей.
- `routeOutCode`, `routeInCode`, `destinationDys(Code)` не заполнятся без
  `refs/esr.json`.
- `exporterName`/`importerName` без `refs/parties.json` возвращаются как
  сырой текст OCR (с низкой уверенностью) вместо каноничного названия.
