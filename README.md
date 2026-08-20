# OCR_task — SMGS qaiməsindən JSON formatında məlumat çıxarılması

Skript SMGS dəmir yolu qaiməsinin (PDF skan) məlumatlarını Gömrük üçün lazım
olan sahələrlə strukturlaşdırılmış JSON-a çevirir. Layihə həm CLI skript
(`main.py`), həm də HTTP xidməti (`api.py`, FastAPI) kimi mövcuddur, mətn
sahələrinin Azərbaycan dilinə tərcüməsi isə `translator.py` vasitəsilə
opsional olaraq edilir.

Əsas ideya: sahələr blankın sabit koordinatları üzrə deyil, **lövbərlər**
(qrafaların başlıqları — «Вагон», «К-во мест», «ГНГ» və s.) üzrə axtarılır.
Bu başlıqlar SMGS formasının bütün variantlarında («Дорожная ведомость» /
«Оригинал накладной» və s.) eynidir, ona görə də konkret blankın tərtibatı
əhəmiyyət kəsb etmir. Tapılan qiymətlər əlavə olaraq nəzarət rəqəmləri
(vaqon, ЕСР stansiya kodu, ISO 6346 konteyner) ilə yoxlanılır — OCR
nəticəsinə kor-koranə etibar edilmir.

## İmkanlar

- Tam səhifəli OCR (Tesseract, `rus+eng`), sözlərin koordinatları ilə birgə,
  səhifə zolaqlara bölünərək çoxaxınlı emal.
- OCR-dən əvvəl skanın əyriliyinin düzəldilməsi (deskew) və miqyasın
  normallaşdırılması.
- Nəzarət rəqəmlərinin yoxlanması: vaqon nömrəsi, ЕСР stansiya kodu, konteyner
  nömrəsi (ISO 6346) — səhvən tanınmış bir rəqəm düzgün nəzarət cəmini
  bərpa edirsə, avtomatik "təmir" edilir.
- Çıxarılan sahələr (bax `main.py`-də `SCHEMA`):
  `invoiceNumber, exporterName, importerName, qnqCode, goodsDescription,
  productWeight, numberOfPlace, containerNumber, customsClearance,
  vehicleNumber, routeOutCode, routeInCode, destinationDys,
  destinationDysCode`.
- Hər sahə üçün etibarlılıq dərəcəsi (`_meta[field].conf`) və mənbə
  (`_meta[field].src`) verilir, həmçinin əl ilə yoxlama tələb edən sahələrin
  siyahısı (`_review`) — sahə tapılmadıqda və ya etibarlılıq 0.85-dən aşağı
  olduqda.
- Bir neçə faylın toplu (batch) emalı, CSV-yə ixrac.
- HTTP API (FastAPI): tək fayl və ya batch, mətn sahələrinin Azure AI
  Translator vasitəsilə opsional tərcüməsi.

## Repozitoriyanın strukturu

```
main.py                  CLI: OCR skripti, sahələrin çıxarılması, CSV-yə ixrac
api.py                   main.py üzərində FastAPI qatı (/extract, /extract/batch endpointləri)
translator.py            Azure AI Translator vasitəsilə mətn sahələrinin tərcüməsi
requirements-api.txt     API qatının asılılıqları + main.py-nin asılılıqları
refs/                    Sifarişçinin sorğu kitabçaları (aşağıya bax) — .gitignore-dadır, repoda saxlanmır
```

## Quraşdırma

Python 3.10+ və rus dili paketi olan sistem Tesseract OCR tələb olunur.

```bash
# sistem Tesseract
# Linux (Debian/Ubuntu):
sudo apt install tesseract-ocr tesseract-ocr-rus
# Windows: Tesseract UB Mannheim (quraşdırarkən Russian seçilməlidir)

# Python asılılıqları
pip install -r requirements-api.txt
```

Yalnız API qatı olmadan CLI skriptini işlətmək üçün kifayətdir:

```bash
pip install pytesseract opencv-python numpy pymupdf rapidfuzz
```

## Sorğu kitabçaları (`refs/`)

Bütün biznesə xas məlumatlar (ЕСР stansiya kodları, kontragentlər reyestri,
konkret nomenklaturaya aid ГНГ kodları) qəsdən koddan çıxarılıb `./refs/*.json`
fayllarına köçürülüb və repozitoriyaya daxil edilmir (`.gitignore`). Bu
fayllar olmadan skript işləməyə davam edir, lakin onlardan asılı sahələr boş
qalır — bu gözlənilən davranışdır, `_review` vasitəsilə görünür, gizli xəta
deyil.

| Fayl                 | Təyinatı                                                          | Təsir etdiyi sahələr |
|----------------------|---------------------------------------------------------------------|----------------|
| `refs/esr.json`      | ЕСР kodu → `[stansiyanın adı, ölkənin ISO alpha-2 kodu]` (~12 min kod, Tarif Rəhbərliyi №4) | `routeOutCode`, `routeInCode`, `destinationDys(Code)` |
| `refs/parties.json`  | kontragentin normallaşdırılmış adı → kanonik ad/hüquqi ünvan       | `exporterName`, `importerName` |
| `refs/gng_az.json`   | ГНГ kodu → yükün adının Azərbaycan dilinə tərcüməsi                | `goodsDescription` (tərcümə zamanı) |

Sorğu kitabçaları qovluğunun yolu `SMGS_REFS_DIR` mühit dəyişəni ilə
dəyişdirilə bilər (defolt olaraq `main.py` ilə yanaşı `./refs`).

`destinationDys`-in müəyyən edilməsi üçün "ev ölkəsi" `SMGS_HOME_COUNTRY`
dəyişəni ilə verilir (defolt `AZ`).

## İstifadə: CLI

```bash
python main.py qaimə.pdf
python main.py *.pdf --csv out.csv
python main.py qaimə.pdf --debug   # hər sahə üzrə confidence/mənbənin stderr-ə çap edilməsi
```

Hər fayl üçün stdout-a tanınmış sahələrlə JSON, stderr-ə isə emal vaxtı və
əl ilə yoxlanılmalı sahələrin siyahısı çıxarılır. `--csv` bayrağı ilə bütün
nəticələr əlavə olaraq bir CSV faylına yazılır (o cümlədən `_review` sütunu
ilə).

## İstifadə: HTTP API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Swagger UI: `http://localhost:8000/docs`

### `POST /extract`

Bir PDF fayl → Gömrük sxeminə uyğun bir JSON obyekti.

```bash
curl -X POST "http://localhost:8000/extract" \
  -F "file=@qaimə.pdf"
```

Sorğu parametrləri:

- `include_meta` (bool, defolt `false`) — cavaba `_meta` (hər sahə üzrə
  etibarlılıq/mənbə), `_review` və `_seconds` əlavə edir.
- `translate` (sətir, məs. `az`) — mətn sahələrini (`exporterName`,
  `importerName`, `goodsDescription`, `destinationDys`) Azure AI Translator
  vasitəsilə tərcümə edir. Rəqəm və kod sahələri tərcümə olunmur, olduğu
  kimi qaytarılır.

### `POST /extract/batch`

Bir sorğuda bir neçə fayl → obyektlərin siyahısı (sıra fayl sırasına
uyğundur). PDF olmayan fayllar və emal xətası olan fayllar `error` sahəsi
ilə qeyd olunur, digərlərinin emalını dayandırmır.

```bash
curl -X POST "http://localhost:8000/extract/batch" \
  -F "files=@1.pdf" -F "files=@2.pdf"
```

### `GET /health`

Xidmətin işlək olduğunun yoxlanması.

## Mətn sahələrinin tərcüməsi (`translator.py`)

Tərcümə **Azure AI Translator** (F0 tarifi) vasitəsilə edilir: ayda
2 000 000 simvol pulsuz, məlumatlar saxlanmır və modellərin öyrədilməsi
üçün istifadə edilmir — bu, şirkət adları və ünvanları olan qaimələr üçün
əhəmiyyətlidir. DeepL istisna edilib — Azərbaycan dilini dəstəkləmir; Google
Cloud Translation isə kart bağlanmasını tələb edir və pulsuz tarifdə mətn
modellərin təkmilləşdirilməsi üçün istifadə oluna bilər.

Mühit dəyişənləri:

```bash
export AZURE_TRANSLATOR_KEY=<Translator resursunun açarı>
export AZURE_TRANSLATOR_REGION=<resursun regionu, məs. westeurope>
# opsional:
export AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
```

Açar [portal.azure.com](https://portal.azure.com) saytında verilir: Create a
resource → Translator → Pricing tier: Free F0 → Keys and Endpoint.

API-siz nöqtəvi yoxlama:

```bash
python translator.py "tərcümə ediləcək mətn" az
```

Qeyd: `exporterName`/`importerName` — hüquqi adlar və ünvanlardır, maşın
tərcüməsi etibarlı deyil; belə sahələri əl ilə yoxlamaq, ya da (daha yaxşısı)
artıq hazır kanonik yazılışı olan `refs/parties.json` faylını qoşmaq
lazımdır — bu halda onları tərcümə etməyə ehtiyac qalmır.

## Cavabın formatı

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

`include_meta=true` (API) və ya `--debug` (CLI) ilə əlavə olaraq mövcuddur:

- `_meta.<field>.conf` — qiymətə etibarlılıq dərəcəsi (0–1);
- `_meta.<field>.src` — sahənin necə tapıldığı (hansı lövbər/yoxlama);
- `_review` — əl ilə yoxlanılmalı sahələrin siyahısı (tapılmayıb və ya
  `conf < 0.85`). Adətən bu, OCR xətasından çox, `refs/`-də lazımi sorğu
  kitabçasının doldurulmamasının göstəricisidir (yuxarıya bax).

## Məlum məhdudiyyətlər

- Skanın keyfiyyətindən asılıdır: mətn üzərinə düşən bulanıq və ya əyri
  möhürlər/imzalar ayrı-ayrı sahələrin etibarlılığını azalda bilər.
- `refs/esr.json` olmadan `routeOutCode`, `routeInCode`,
  `destinationDys(Code)` doldurulmayacaq.
- `refs/parties.json` olmadan `exporterName`/`importerName` kanonik ad
  əvəzinə aşağı etibarlılıqlı xam OCR mətni kimi qaytarılır.
