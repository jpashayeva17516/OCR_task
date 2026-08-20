"""
СМГС -> JSON (Gömrük). Версия 4: независимость от вёрстки бланка.

Раньше поля читались из зон с фиксированными координатами — это ломалось на
другом варианте бланка («Дорожная ведомость» против «Оригинала накладной»).
Теперь один полностраничный проход OCR со словами и их координатами, а поля
находятся по ЯКОРЯМ — подписям граф («Вагон», «К-во мест», «ГНГ», ...) — и
подтверждаются контрольными разрядами. Подписи одинаковы во всех вариантах
СМГС, поэтому вёрстка перестаёт иметь значение.

Установка:
    pip install pytesseract opencv-python numpy pymupdf rapidfuzz
    Windows: Tesseract UB Mannheim (+ Russian)   Linux: tesseract-ocr-rus

Запуск:
    python main.py 1.pdf [2.pdf ...] [--csv out.csv] [--debug]
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

import pytesseract
from pytesseract import Output

try:
    from rapidfuzz import fuzz, process as rf_process
    HAVE_FUZZ = True
except ImportError:
    HAVE_FUZZ = False

if os.name == "nt":
    for _d in (r"C:\Program Files\Tesseract-OCR",
               r"C:\Program Files (x86)\Tesseract-OCR",
               os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR"),
               r"C:\msys64\ucrt64\share"):
        _exe, _data = os.path.join(_d, "tesseract.exe"), os.path.join(_d, "tessdata")
        if os.path.exists(_exe) and os.path.isdir(_data) and os.listdir(_data):
            pytesseract.pytesseract.tesseract_cmd = _exe
            os.environ["TESSDATA_PREFIX"] = _data
            break
    else:
        print("ВНИМАНИЕ: Tesseract с языковыми данными не найден.", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════
# 1. СПРАВОЧНИКИ — ТОЛЬКО из ./refs/*.json, никаких реальных бизнес-данных
#    (номера станций, названия/адреса контрагентов, коды ГНГ конкретных
#    грузов) в самом коде. Нет файла -> справочник пустой -> зависящее
#    поле не заполнится, это видно в _review, а не тихий баг.
#
#    ./refs/ (кроме *.example.json) в .gitignore — см. refs/.gitignore.
# ══════════════════════════════════════════════════════════════════════

REFS_DIR = os.environ.get(
    "SMGS_REFS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs"))


def _load(name: str, default: dict) -> dict:
    p = os.path.join(REFS_DIR, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


# код ЕСР -> [название, ISO alpha-2]. Тарифное руководство №4 (~12k кодов).
# Без refs/esr.json маршрутные поля (routeOutCode/routeInCode/destinationDys)
# останутся пустыми — это ожидаемо, а не баг.
ESR: dict[str, list] = _load("esr.json", {})

# ISO 3166-1 alpha-2 -> numeric. Публичный неизменный стандарт, не бизнес-
# данные заказчика, поэтому небольшой встроенный набор — нормальная заглушка.
ISO_NUM: dict[str, str] = _load("iso3166.json", {
    "GE": "268", "UZ": "860", "AZ": "031", "TM": "795", "TR": "792",
    "KZ": "398", "RU": "643", "IR": "364", "GB": "826", "UA": "804",
})

# код ГНГ -> перевод наименования груза на AZ. Специфично для номенклатуры
# конкретного заказчика — только из refs/gng_az.json.
GNG_AZ: dict[str, str] = _load("gng_az.json", {})

# нормализованное имя контрагента -> полное имя/адрес для вставки в ответ.
# Реестр контрагентов заказчика (юр. адреса, ИНН) — только из refs/parties.json.
PARTIES: dict[str, str] = _load("parties.json", {})

HOME_COUNTRY = os.environ.get("SMGS_HOME_COUNTRY", "AZ")
PARTY_MIN_SCORE = 60

for _name, _dict in (("esr.json", ESR), ("gng_az.json", GNG_AZ),
                      ("parties.json", PARTIES)):
    if not _dict:
        print(f"ВНИМАНИЕ: refs/{_name} не найден или пуст — "
              f"зависящие от него поля не заполнятся.", file=sys.stderr)

# Упаковка (графа 16) — по этим меткам находятся строки таблицы мест/массы.
PACKAGING = ("пакет", "единиц", "поддон", "неупакован", "ящик", "мешок",
             "бочк", "паллет", "коробк", "связк", "рулон", "кипа", "контейнер")


# ══════════════════════════════════════════════════════════════════════
# 2. Рендер и нормализация
# ══════════════════════════════════════════════════════════════════════

REF_WIDTH = 2480      # A4 @ 300 dpi


def render(pdf_path: str, dpi: int = 300, page: int = 0) -> np.ndarray:
    doc = pymupdf.open(pdf_path)
    pix = doc[page].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    img = np.ascontiguousarray(
        np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width))
    doc.close()
    return img


def normalize_scale(img: np.ndarray) -> np.ndarray:
    if abs(img.shape[1] - REF_WIDTH) / REF_WIDTH < 0.05:
        return img
    k = REF_WIDTH / img.shape[1]
    return cv2.resize(img, None, fx=k, fy=k,
                      interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)


def deskew(img: np.ndarray, probe_width: int = 1200) -> np.ndarray:
    s = probe_width / img.shape[1]
    probe = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img
    lines = cv2.HoughLinesP(cv2.Canny(probe, 50, 150), 1, np.pi / 720, 120,
                            minLineLength=probe.shape[1] // 4, maxLineGap=12)
    if lines is None:
        return img
    ang = [a for a in (math.degrees(math.atan2(y2 - y1, x2 - x1))
                       for x1, y1, x2, y2 in lines.reshape(-1, 4)) if abs(a) < 15]
    if not ang:
        return img
    a = float(np.median(ang))
    if abs(a) < 0.05:
        return img
    h, w = img.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


# ══════════════════════════════════════════════════════════════════════
# 3. Полностраничный OCR со словами (полосами, параллельно)
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Word:
    text: str
    conf: float
    x0: float; y0: float; x1: float; y1: float

    @property
    def x(self): return (self.x0 + self.x1) / 2
    @property
    def y(self): return (self.y0 + self.y1) / 2
    @property
    def digits(self): return re.sub(r"\D", "", self.text)


def page_words(img: np.ndarray, bands: int = 6, overlap: int = 60) -> list[Word]:
    """Tesseract — внешний процесс, GIL не держит: полосы читаем параллельно."""
    h, w = img.shape
    step = max(1, h // bands)
    tasks = []
    for i in range(bands):
        y0 = max(0, i * step - (overlap if i else 0))
        y1 = h if i == bands - 1 else min(h, (i + 1) * step + overlap)
        tasks.append((y0, img[y0:y1]))

    def run(t):
        off, strip = t
        d = pytesseract.image_to_data(strip, lang="rus+eng",
                                      config="--oem 1 --psm 6",
                                      output_type=Output.DICT)
        out = []
        for txt, cf, x, y, ww, hh in zip(d["text"], d["conf"], d["left"],
                                         d["top"], d["width"], d["height"]):
            txt = txt.strip()
            if txt and float(cf) >= 0:
                out.append(Word(txt, float(cf) / 100, x / w, (off + y) / h,
                                (x + ww) / w, (off + y + hh) / h))
        return out

    with ThreadPoolExecutor(max_workers=min(bands, 8)) as ex:
        words = [wd for chunk in ex.map(run, tasks) for wd in chunk]

    seen, out = set(), []
    for wd in sorted(words, key=lambda z: (z.y, z.x)):
        key = (wd.text, round(wd.x, 2), round(wd.y, 2))
        if key not in seen:
            seen.add(key)
            out.append(wd)
    return out


def build_lines(words: list[Word], tol: float = 0.006) -> list[tuple[float, str, list[Word]]]:
    lines, cur, cy = [], [], None
    for wd in sorted(words, key=lambda z: (z.y, z.x)):
        if cy is None or abs(wd.y - cy) <= tol:
            cur.append(wd)
            cy = wd.y if cy is None else (cy * (len(cur) - 1) + wd.y) / len(cur)
        else:
            cur.sort(key=lambda z: z.x)
            lines.append((cy, " ".join(z.text for z in cur), cur))
            cur, cy = [wd], wd.y
    if cur:
        cur.sort(key=lambda z: z.x)
        lines.append((cy, " ".join(z.text for z in cur), cur))
    return lines


def reocr_digits(img: np.ndarray, wd: Word, pad: float = 0.004) -> str:
    """Точечное перечитывание рамки слова с whitelist цифр."""
    h, w = img.shape
    c = img[max(0, int((wd.y0 - pad) * h)):int((wd.y1 + pad) * h),
            max(0, int((wd.x0 - pad) * w)):int((wd.x1 + pad) * w)]
    if c.size == 0:
        return wd.digits
    if c.shape[0] < 60:
        k = 60 / c.shape[0]
        c = cv2.resize(c, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    c = cv2.copyMakeBorder(c, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    t = pytesseract.image_to_string(
        c, lang="eng", config="--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789")
    d = re.sub(r"\D", "", t)
    return d or wd.digits


# ══════════════════════════════════════════════════════════════════════
# 4. Контрольные разряды
# ══════════════════════════════════════════════════════════════════════

def wagon_ok(n: str) -> bool:
    if not re.fullmatch(r"\d{8}", n):
        return False
    s = 0
    for i, d in enumerate(n[:7]):
        v = int(d) * (2 if i % 2 == 0 else 1)
        s += v // 10 + v % 10
    return (10 - s % 10) % 10 == int(n[7])


def esr_ok(c: str) -> bool:
    if not re.fullmatch(r"\d{6}", c):
        return False
    r = sum(int(d) * (i + 1) for i, d in enumerate(c[:5])) % 11
    if r == 10:
        r = sum(int(d) * (i + 3) for i, d in enumerate(c[:5])) % 11
        r = 0 if r == 10 else r
    return r == int(c[5])


_ISO_LETTER = {ch: v for ch, v in zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    [10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 27, 28,
     29, 30, 31, 32, 34, 35, 36, 37, 38])}


def iso6346_ok(code: str) -> bool:
    """Контейнер ISO 6346: 4 буквы + 7 цифр, контрольный разряд по весам 2^i."""
    if not re.fullmatch(r"[A-Z]{4}\d{7}", code):
        return False
    s = sum((_ISO_LETTER[ch] if ch.isalpha() else int(ch)) * (1 << i)
            for i, ch in enumerate(code[:10]))
    return s % 11 % 10 == int(code[10])


def repair(raw: str, ok, length: int) -> Optional[str]:
    c = re.sub(r"\D", "", raw or "")
    if len(c) != length:
        return None
    if ok(c):
        return c
    for i in range(length):
        for alt in "0123456789":
            if alt != c[i] and ok(c[:i] + alt + c[i + 1:]):
                return c[:i] + alt + c[i + 1:]
    return None


# ══════════════════════════════════════════════════════════════════════
# 5. Извлечение по якорям
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Val:
    value: Any = None
    conf: float = 0.0
    src: str = ""


def norm(t: str) -> str:
    return re.sub(r"[^а-яёa-z0-9]", "", t.lower())


def anchors(words: list[Word], *keys: str, xmax=1.0, ymax=1.0):
    out = []
    for wd in words:
        t = norm(wd.text)
        if len(t) >= 3 and any(k in t for k in keys) \
                and wd.x0 < xmax and wd.y0 < ymax:
            out.append(wd)
    return sorted(out, key=lambda z: z.y)


def match_party(raw: str) -> tuple[Optional[str], float]:
    key = re.sub(r"[^a-zа-яё0-9 ]", " ", raw.lower())
    key = re.sub(r"\s{2,}", " ", key).strip()
    if not key:
        return None, 0.0
    if HAVE_FUZZ:
        hit = rf_process.extractOne(key, list(PARTIES.keys()),
                                    scorer=fuzz.token_set_ratio)
        if hit and hit[1] >= PARTY_MIN_SCORE:
            return PARTIES[hit[0]], round(hit[1] / 100, 2)
    else:
        for k, v in PARTIES.items():
            if all(t in key for t in k.split()[:3]):
                return v, 0.90
    return None, 0.0


def extract(img: np.ndarray, W: list[Word], debug=False) -> dict[str, Val]:
    LINES = build_lines(W)
    FULL = "\n".join(t for _, t, _ in LINES)
    F: dict[str, Val] = {}
    put = lambda k, v, c, s: F.__setitem__(k, Val(v, round(float(c), 3), s))

    # ---- № отправки: правый верхний угол, два прохода OCR.
    # Грузинский штамп поверх строки «GR ...» убивает проход с whitelist,
    # а чистый проход хуже держит цифры — поэтому оба, приоритет контексту GR.
    h, w = img.shape
    corner = cv2.copyMakeBorder(
        img[int(0.005 * h):int(0.105 * h), int(0.68 * w):],
        10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    inv, inv_ctx = None, False
    for cfg in ("--oem 1 --psm 6",
                "--oem 1 --psm 6 -c tessedit_char_whitelist=0123456789GRCB "):
        t = pytesseract.image_to_string(corner, lang="eng", config=cfg)
        for line in t.splitlines():
            m = re.search(r"(?<!\d)(\d{7})(?!\d)", line)
            if not m:
                continue
            ctx = bool(re.search(r"G\s?R|C\s?B|№", line, re.I))
            if inv is None or (ctx and not inv_ctx):
                inv, inv_ctx = m.group(1), ctx
        if inv_ctx:
            break
    put("invoiceNumber", int(inv) if inv else None,
        0.95 if inv else 0.0, "правый верхний угол, re-OCR")

    # ---- ГНГ + наименование груза --------------------------------------------
    # Считаем этот блок ДО вагона: код ГНГ — тоже 8-значное число и иногда
    # тоже проходит checksum вагона (совпадение), поэтому его нужно знать
    # заранее, чтобы явно исключить из кандидатов на номер вагона ниже.
    # Правая граница графы 15 («Наименование груза»): дальше начинаются
    # графы 16-19 (упаковка/места/масса/пломбы). Их заголовки иногда
    # попадают в ту же строку OCR, что и текст описания — без этого
    # ограничения они приклеиваются к goodsDescription («...mds Род
    # упаков К-во мест Масса (в кг)»).
    GOODS_X_MAX = 0.56
    HEADER_NOISE = re.compile(
        r"\b(масс[аы]?|мест\w*|упаков\w*|род\b|ед\w*\.?ниц\w*|штук\w*)\b", re.I)

    qnq, desc_ru = None, ""
    for ly, ltxt, lw in LINES:
        m = re.search(r"[гтп]\s?н\s?[гт]\W{0,3}(\d{8})", ltxt, re.I)
        if m:
            qnq = m.group(1)
            words_after = [wd for wd in lw if wd.x0 < GOODS_X_MAX]
            # найдём слово(-а), содержащее сам код ГНГ, и берём только то,
            # что физически лежит правее него в графе 15
            code_x1 = next((wd.x1 for wd in words_after if qnq in wd.digits), None)
            if code_x1 is not None:
                words_after = [wd for wd in words_after if wd.x0 >= code_x1]
            desc_ru = " ".join(wd.text for wd in words_after).strip(" .,-—")
            desc_ru = HEADER_NOISE.split(desc_ru)[0].strip(" .,-—")
            nxt = [
                " ".join(wd.text for wd in nw if wd.x0 < GOODS_X_MAX)
                for y, _, nw in LINES if ly < y <= ly + 0.014
            ]
            nxt = [HEADER_NOISE.split(t)[0].strip(" .,-—") for t in nxt if t]
            if len(desc_ru) < 25 and nxt and nxt[0]:
                desc_ru = (desc_ru + " " + nxt[0]).strip()
            break
    if not qnq:
        # Запасной путь: код ГНГ — первый 8-значный токен у левого края графы
        # 15 («Наименование груза»). Раньше окно поиска (x0<0.35, y 0.30–0.60)
        # было слишком широким и на бланках, где подпись «ГНГ» OCR читает
        # латиницей («THI»/«THE»/«THY» вместо «ГНГ»primary-regex не matches),
        # проваливалось в графу 7 и подхватывало номер вагона — тот тоже
        # 8-значный и оказывался в той же зоне. По всем образцам бланков код
        # ГНГ стабильно лежит у самого левого края (x0<0.10) на y 0.38–0.47,
        # тогда как графа 7 (вагон) заметно выше — сужаем окно под это.
        for wd in W:
            if len(wd.digits) == 8 and wd.x0 < 0.10 and 0.38 < wd.y < 0.47:
                qnq = wd.digits
                break
    put("qnqCode", int(qnq) if qnq else None, 0.95 if qnq else 0.0, "якорь ГНГ")
    if qnq and qnq in GNG_AZ:
        put("goodsDescription", GNG_AZ[qnq], 1.0, "номенклатура ГНГ")
    else:
        put("goodsDescription", re.sub(r"\s{2,}", " ", desc_ru)[:120] or None,
            0.50 if desc_ru else 0.0,
            f"ГНГ {qnq} нет в справочнике — текст с бланка" if qnq else "—")

    # ---- вагон: 8 цифр с контрольным разрядом около якоря «Вагон» ----------
    # ВАЖНО: графа 8 бланка называется «Вагон предоставлен» и тоже содержит
    # слово «вагон» — якорный поиск иногда цепляется за неё вместо графы 7
    # и промахивается мимо настоящего номера. Оба пути ниже явно исключают
    # код ГНГ (qnq) из кандидатов, а не полагаются только на порядок сортировки.
    wagon, wsrc = None, ""
    for a in anchors(W, "вагон", "baron", "barou"):
        near = [wd for wd in W
                if a.y - 0.005 <= wd.y <= a.y + 0.045
                and a.x0 - 0.06 <= wd.x0 <= a.x1 + 0.30
                and len(wd.digits) == 8
                and wd.digits != qnq]
        for wd in sorted(near, key=lambda z: (z.y, z.x)):
            fixed = repair(reocr_digits(img, wd), wagon_ok, 8)
            if fixed and fixed != qnq:
                wagon, wsrc = fixed, "якорь «Вагон» + checksum"
                break
        if wagon:
            break
    def date_like(d: str) -> bool:
        m = re.fullmatch(r"(\d{2})(\d{2})(20\d{2})", d)
        return bool(m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31)

    if not wagon:
        # Запасной путь: номер вагона печатается крупно в верхней половине.
        # Даты вида 17072026 тоже проходят контроль — отсекаем их отдельно.
        # Код ГНГ (графа 15, обычно y>0.35) тоже иногда проходит checksum —
        # исключаем его явно по значению, а не только по высоте шрифта,
        # потому что на разных OCR-прогонах порядок по высоте не стабилен.
        cands8 = [wd for wd in W
                  if len(wd.digits) == 8 and wagon_ok(wd.digits)
                  and wd.y < 0.55 and wd.x0 < 0.65
                  and not date_like(wd.digits)
                  and wd.digits != qnq]
        cands8.sort(key=lambda z: (-(z.y1 - z.y0), z.y))
        if cands8:
            wagon, wsrc = cands8[0].digits, "checksum + крупный кегль"
    put("vehicleNumber", int(wagon) if wagon else None,
        0.99 if wagon else 0.0, wsrc or "не найден")

    # ---- контейнер ISO 6346 -------------------------------------------------
    _LAT = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")   # кир. двойники
    cont = None
    for wd in W:
        txt = wd.text.upper().translate(_LAT).replace(" ", "")
        m = re.fullmatch(r"([A-Z]{3}[UJZ])(\d{7})", txt)
        if m and iso6346_ok(m.group(1) + m.group(2)):
            cont = m.group(1) + m.group(2)
            break
        if re.fullmatch(r"[A-Z]{3}[UJZ]", txt):
            mates = [z for z in W if abs(z.y - wd.y) < 0.012
                     and 0 < z.x0 - wd.x1 < 0.10 and len(z.digits) == 7]
            for z in sorted(mates, key=lambda q: q.x0):
                if iso6346_ok(txt + z.digits):
                    cont = txt + z.digits
                    break
        if cont:
            break
    put("containerNumber", cont or "-", 0.99 if cont else 0.9,
        "ISO 6346 + checksum" if cont else "контейнер не найден")

    # ---- места и масса: строки таблицы по меткам упаковки -------------------
    # Заголовки граф 17 и 18 стоят рядом — берём пару «мест»+«масса»
    # с минимальным разрывом по вертикали, а не первый попавшийся «масса»
    # (иначе ловится «Масса тары» из графы 11).
    hdr_m = [a for a in anchors(W, "масса") if 0.30 < a.y < 0.60 and a.x0 > 0.55]
    hdr_p = [a for a in anchors(W, "мест") if 0.30 < a.y < 0.60 and 0.45 < a.x0 < 0.75]
    hdr_m18 = [a for a in hdr_m if 0.60 < a.x < 0.80]     # «Масса (в кг)»
    if hdr_p and hdr_m:
        pa = hdr_p[0]
        ma = min(hdr_m, key=lambda a: abs(a.y - pa.y))
        px, mx, hy = pa.x, ma.x, max(pa.y, ma.y)
    elif hdr_m18:                       # якорь «мест» не распознался:
        ma = hdr_m18[0]                 # колонка 17 всегда левее 18 на ~0.085
        px, mx, hy = ma.x - 0.085, ma.x, ma.y
    elif hdr_p:
        px, mx, hy = hdr_p[0].x, hdr_p[0].x + 0.085, hdr_p[0].y
    else:
        px, mx, hy = 0.62, 0.705, 0.40

    # Полностраничный psm 6 теряет одиночные числа в ячейках таблицы,
    # поэтому область под заголовками перечитываем отдельно с whitelist цифр.
    def harvest_table() -> list[Word]:
        hh, ww = img.shape
        x0, x1 = max(0, px - 0.07), min(1.0, mx + 0.07)
        y0, y1 = hy + 0.003, min(1.0, hy + 0.115)
        c = img[int(y0 * hh):int(y1 * hh), int(x0 * ww):int(x1 * ww)]
        if c.size == 0:
            return []
        k = max(1.0, 110 / max(1, c.shape[0] // 8))
        # Рамки ячеек валят Tesseract даже на идеально чётких цифрах —
        # стираем горизонтальные и вертикальные линии морфологией.
        b = cv2.adaptiveThreshold(255 - c, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                  cv2.THRESH_BINARY, 15, -2)
        grid = cv2.morphologyEx(b, cv2.MORPH_OPEN, cv2.getStructuringElement(
                   cv2.MORPH_RECT, (60, 1))) \
             | cv2.morphologyEx(b, cv2.MORPH_OPEN, cv2.getStructuringElement(
                   cv2.MORPH_RECT, (1, 45)))
        c = c.copy()
        c[grid > 0] = 255
        # Просвечивающая с оборота печать — светло-серая; отбеливаем её,
        # оставляя только настоящие чернила.
        c[c > 170] = 255
        if c.shape[0] < 350:
            c = cv2.resize(c, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
            k = 1.6
        else:
            k = 1.0
        c = cv2.copyMakeBorder(c, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
        d = pytesseract.image_to_data(
            c, lang="eng",
            config="--oem 1 --psm 6 -c tessedit_char_whitelist=0123456789",
            output_type=Output.DICT)
        out = []
        for txt, cf, x, y, tw, th in zip(d["text"], d["conf"], d["left"],
                                         d["top"], d["width"], d["height"]):
            txt = txt.strip()
            if txt and float(cf) >= 0:
                gx0 = x0 + (x - 12) / k / ww
                gy0 = y0 + (y - 12) / k / hh
                out.append(Word(txt, float(cf) / 100, gx0, gy0,
                                gx0 + tw / k / ww, gy0 + th / k / hh))
        return out

    Wt = W + harvest_table()

    col_p = sorted((wd for wd in Wt
                    if wd.digits and len(wd.digits) <= 6
                    and wd.conf >= 0.30
                    and abs(wd.x - px) < 0.055
                    and hy + 0.004 < wd.y < hy + 0.11
                    and not re.search(r"[^\d.,]", wd.text)),
                   key=lambda z: z.y)
    col_m = sorted((wd for wd in Wt
                    if wd.digits and len(wd.digits) <= 7
                    and wd.conf >= 0.30
                    and abs(wd.x - mx) < 0.055
                    and hy + 0.004 < wd.y < hy + 0.11
                    and not re.search(r"[^\d.,]", wd.text)),
                   key=lambda z: z.y)
    # Строка таблицы = число мест и число массы на одной высоте. Итог массы
    # в теле бланка стоит без пары в колонке мест и сюда не попадает.
    rows, used, seen_y = [], set(), []
    for pw in col_p:
        if any(abs(pw.y - sy) < 0.006 for sy in seen_y):
            continue                        # дубль строки из второго прохода
        mates = [mw for mw in col_m if id(mw) not in used
                 and abs(mw.y - pw.y) < 0.008]
        mates.sort(key=lambda mw: (-mw.conf, abs(mw.y - pw.y)))
        mate = mates[0] if mates else None
        if mate:
            used.add(id(mate))
            seen_y.append(pw.y)
            rows.append((int(pw.digits), int(mate.digits)))
    if os.environ.get("SMGS_DEBUG_ROWS"):
        print("ROWS:", rows, file=sys.stderr)
        print("COLP:", [(z.text, round(z.x, 3), round(z.y, 3), round(z.conf, 2))
                        for z in col_p], file=sys.stderr)
        print("COLM:", [(z.text, round(z.x, 3), round(z.y, 3), round(z.conf, 2))
                        for z in col_m], file=sys.stderr)
    places = sum(r[0] for r in rows)
    weight = sum(r[1] for r in rows)

    # Перекрёстная проверка: масса напечатана и построчно, и итогом в теле.
    totals = [int(wd.digits) for wd in W
              if 0.55 < wd.x < 0.82 and hy + 0.03 < wd.y < hy + 0.22
              and 3 <= len(wd.digits) <= 7
              and not re.search(r"[^\d.,]", wd.text)]
    totals = [t for t in totals if t not in {r[1] for r in rows}]
    if rows and weight and weight in totals:
        put("productWeight", weight, 0.99, f"{len(rows)} строк, итог сходится")
    elif rows and totals:
        near_t = min(totals, key=lambda t: abs(t - weight))
        if abs(near_t - weight) <= max(200, weight * 0.02):
            # строки и итог почти совпали: итог — одно чтение, строки — три,
            # но расходятся они из-за потерянной цифры в строке; берём итог
            put("productWeight", near_t, 0.75, "итог тела бланка, строки разошлись")
        else:
            put("productWeight", weight, 0.70, f"{len(rows)} строк, итог не подтверждён")
    elif rows and weight:
        put("productWeight", weight, 0.80, f"{len(rows)} строк, итога нет")
    elif totals:
        put("productWeight", max(totals), 0.60, "только итог в теле бланка")
    else:
        put("productWeight", None, 0.0, "не найдено")
    put("numberOfPlace", places if rows else None,
        (0.95 if len(rows) > 1 or places else 0.85) if rows else 0.0,
        f"{len(rows)} строк таблицы" if rows else "строки упаковки не найдены")

    # ---- маршрут -------------------------------------------------------------
    def esr_from(wd: Word) -> Optional[str]:
        """Код ЕСР из токена. Код часто слипается с текстом («...БАШИ 1 548803»
        -> «1548803»), поэтому сканируем окна, предпочитая известные станции
        и позицию ближе к концу токена."""
        d = wd.digits
        if not (6 <= len(d) <= 9):
            return None
        best = None                        # (в справочнике?, позиция окна), код
        for i in range(len(d) - 5):
            c = d[i:i + 6]
            if esr_ok(c):
                score = (c in ESR, i)
                if best is None or score > best[0]:
                    best = (score, c)
        return best[1] if best else None

    borders: list[str] = []               # графы 6 и 22 в порядке следования
    for wd in sorted(W, key=lambda z: (z.y, z.x)):
        if wd.y < 0.12:                    # шапка: там код станции отправления
            continue
        if not (wd.x0 > 0.84 or 0.20 < wd.x0 < 0.45):
            continue
        c = esr_from(wd)
        if c and c not in borders:
            borders.append(c)
    known = [c for c in borders if c in ESR]

    # станция отправления — код в шапке (графа 2, правый верх)
    # № отправки (7 цифр) случайным окном тоже проходит контроль ЕСР,
    # поэтому для станции отправления требуем попадание в справочник.
    origin = next((esr_from(wd) for wd in W
                   if wd.y < 0.14 and wd.x0 > 0.55
                   and esr_from(wd) in ESR), None)
    if not origin and known:
        origin = known[0]

    # станция назначения — «NN CCCCCC» у якоря «назначения», иначе конец списка
    dest = None
    for a in anchors(W, "назначен"):
        near = sorted((wd for wd in W if abs(wd.y - a.y) < 0.02 and wd.x0 > a.x1),
                      key=lambda z: z.x)
        for wd in near:
            c = esr_from(wd) or (repair(wd.digits[-6:], esr_ok, 6)
                                 if len(wd.digits) >= 6 else None)
            if c:
                dest = c
                break
        if dest:
            break
    if not dest and known:
        dest = known[-1]

    out_c = ESR.get(origin, [None, None])[1] if origin else None
    in_c = ESR.get(dest, [None, None])[1] if dest else None
    put("routeOutCode", ISO_NUM.get(out_c), 0.95 if out_c else 0.0,
        f"страна отправления {out_c or origin or '?'}")
    put("routeInCode", ISO_NUM.get(in_c), 0.95 if in_c else 0.0,
        f"страна назначения {in_c or dest or '?'}")

    # destinationDys: станция, где груз покидает зону ответственности AZ —
    # само назначение, если оно в AZ, иначе последний AZ-переход по маршруту.
    if dest and ESR.get(dest, [None, None])[1] == HOME_COUNTRY:
        dys = dest
    else:
        az = [c for c in known if ESR[c][1] == HOME_COUNTRY]
        dys = az[-1] if az else None
    if dys:
        put("destinationDys", ESR[dys][0], 0.97, "маршрут + checksum")
        put("destinationDysCode", dys, 0.99, "маршрут + checksum")
    else:
        put("destinationDys", None, 0.0, f"станций {HOME_COUNTRY} не найдено")
        put("destinationDysCode", None, 0.0, "—")

    # ---- отправитель / получатель: блоки между якорями граф 1, 4, 5 ---------
    a_snd = anchors(W, "отправител", xmax=0.45)
    a_rcv = anchors(W, "получател", xmax=0.45)
    a_dst = anchors(W, "назначен", xmax=0.45)
    # Якоря обязаны идти по порядку: заголовок «...(для получателя)» в шапке
    # иначе притворяется графой 4 и рушит разметку блоков.
    y_snd = a_snd[0].y if a_snd else 0.07
    y_rcv = next((a.y for a in a_rcv if a.y > y_snd + 0.01), y_snd + 0.08)
    y_dst = next((a.y for a in a_dst if a.y > y_rcv + 0.01), y_rcv + 0.07)

    def block(y_from, y_to):
        ws = [wd for wd in W
              if y_from + 0.004 < wd.y < y_to - 0.003 and 0.05 < wd.x0 < 0.52]
        ws.sort(key=lambda z: (round(z.y, 3), z.x))
        return re.sub(r"\s{2,}", " ", " ".join(z.text for z in ws)).strip()

    for (yf, yt), fld in (((y_snd, y_rcv), "exporterName"),
                          ((y_rcv, y_dst), "importerName")):
        raw = block(yf, yt)
        hit, sc = match_party(raw)
        put(fld, hit or raw or None, sc if hit else (0.40 if raw else 0.0),
            f"реестр ({sc:.2f})" if hit else "OCR, нет в реестре контрагентов")

    # ---- customsClearance: «... ведомость N листов/шт» -----------------------
    cc = None
    for _, ltxt, _ in LINES:               # «шт»/«листа» бывают на другой строке
        m = re.search(r"[вб]едомост\w*\W{0,15}(\d{1,2})\b", ltxt, re.I) \
            or re.search(r"(\d{1,2})\s*лист", ltxt, re.I)
        if m:
            cc = int(m.group(1))
            break
    put("customsClearance", cc, 0.90 if cc is not None else 0.0,
        "графа 3, листы ведомости" if cc is not None else "не найдено")

    if debug:
        for k, v in F.items():
            print(f"  {k:20s} = {v.value!r}  conf={v.conf} [{v.src}]", file=sys.stderr)
    return F


SCHEMA = ["invoiceNumber", "exporterName", "importerName", "qnqCode",
          "goodsDescription", "productWeight", "numberOfPlace", "containerNumber",
          "customsClearance", "vehicleNumber", "routeOutCode", "routeInCode",
          "destinationDys", "destinationDysCode"]


def process(pdf: str, debug: bool = False) -> dict:
    t0 = time.time()
    img = normalize_scale(deskew(render(pdf)))
    W = page_words(img)
    F = extract(img, W, debug)
    return {"file": os.path.basename(pdf),
            "data": {k: (F[k].value if k in F else None) for k in SCHEMA},
            "_meta": {k: {"conf": v.conf, "src": v.src} for k, v in F.items()},
            "_review": [k for k in SCHEMA
                        if k not in F or F[k].value in (None, "") or F[k].conf < 0.85],
            "_seconds": round(time.time() - t0, 2)}


def main(argv: list[str]) -> int:
    debug = "--debug" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    csv_out = None
    if "--csv" in argv:
        i = argv.index("--csv")
        if i + 1 < len(argv):
            csv_out = argv[i + 1]
            args = [a for a in args if a != csv_out]
    if not args:
        print("Usage: python main.py <file.pdf> [...] [--csv out.csv] [--debug]",
              file=sys.stderr)
        return 2

    paths = []
    for a in args:
        hits = glob.glob(a)
        paths.extend(hits if hits else [a])

    results = []
    for p in paths:
        if not os.path.exists(p):
            print(f"[skip] not found: {p}", file=sys.stderr)
            continue
        try:
            r = process(p, debug=debug)
        except Exception as e:
            print(f"[fail] {p}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        results.append(r)
        print(json.dumps(r["data"], ensure_ascii=False, indent=2))
        print(f"  {r['_seconds']}s" +
              (f" | на проверку: {', '.join(r['_review'])}" if r["_review"] else ""),
              file=sys.stderr)

    if csv_out and results:
        import csv
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=["file"] + SCHEMA + ["_review"])
            wr.writeheader()
            for r in results:
                wr.writerow({"file": r["file"], **r["data"],
                             "_review": ";".join(r["_review"])})
        print(f"CSV: {csv_out}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))