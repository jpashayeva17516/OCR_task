"""
FastAPI-обёртка над main.py: извлечение полей СМГС-накладной по HTTP.
 
Файл main.py (ваш существующий OCR-скрипт) должен лежать рядом с этим файлом —
здесь он используется как модуль, а не как CLI.
 
Установка:
    pip install fastapi "uvicorn[standard]" python-multipart
    (+ зависимости main.py: pytesseract opencv-python numpy pymupdf rapidfuzz)
 
Запуск:
    uvicorn api:app --host 0.0.0.0 --port 8000
 
Документация (Swagger UI) поднимается сама:
    http://localhost:8000/docs
"""
from __future__ import annotations
 
import os
import tempfile
from typing import Optional
 
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
 
import main as smgs  # process(), SCHEMA и т.д. из вашего существующего скрипта
import translator  # перевод текстовых полей через Azure AI Translator
 
 
# ══════════════════════════════════════════════════════════════════════
# Схема ответа — ровно те поля, что нужны для Gömrük
# ══════════════════════════════════════════════════════════════════════
 
class ExtractResult(BaseModel):
    invoiceNumber: Optional[int] = None
    exporterName: Optional[str] = None
    importerName: Optional[str] = None
    qnqCode: Optional[int] = None
    goodsDescription: Optional[str] = None
    productWeight: Optional[int] = None
    numberOfPlace: Optional[int] = None
    containerNumber: Optional[str] = None
    customsClearance: Optional[int] = None
    vehicleNumber: Optional[int] = None
    routeOutCode: Optional[str] = None
    routeInCode: Optional[str] = None
    destinationDys: Optional[str] = None
    destinationDysCode: Optional[str] = None
 
 
class BatchEntry(ExtractResult):
    file: str
    error: Optional[str] = None
 
 
app = FastAPI(
    title="SMGS Extractor API",
    description="Извлечение данных СМГС-накладной (PDF) в JSON для Gömrük.",
    version="1.0.0",
)
 
 
def _save_upload(file: UploadFile, content: bytes) -> str:
    suffix = os.path.splitext(file.filename or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        return tmp.name
 
 
def _run(pdf_path: str, debug: bool) -> dict:
    try:
        return smgs.process(pdf_path, debug=debug)
    except Exception as exc:  # noqa: BLE001 — любая ошибка OCR/парсинга -> 500
        raise HTTPException(status_code=500, detail=f"Ошибка обработки: {exc}") from exc
 
 
@app.get("/health", tags=["service"])
def health() -> dict:
    return {"status": "ok"}
 
 
@app.post("/extract", response_model=ExtractResult, tags=["extract"])
async def extract_one(
    file: UploadFile = File(..., description="СМГС-накладная, PDF"),
    include_meta: bool = Query(
        False, description="Добавить _meta (уверенность по полям) и _review в ответ"),
    translate: Optional[str] = Query(
        None,
        description="Код целевого языка (напр. 'az') — перевести текстовые поля "
                    "(exporterName, importerName, goodsDescription, destinationDys) "
                    "через Azure AI Translator. Коды/номера (invoiceNumber, qnqCode, "
                    "containerNumber, routeOutCode и т.д.) переводу не подлежат и "
                    "остаются как есть. Требует AZURE_TRANSLATOR_KEY/"
                    "AZURE_TRANSLATOR_REGION в окружении."),
):
    """Один файл -> один JSON-объект строго по схеме Gömrük."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Ожидается PDF-файл")
 
    tmp_path = _save_upload(file, await file.read())
    try:
        result = _run(tmp_path, debug=False)
    finally:
        os.unlink(tmp_path)
 
    data = result["data"]
    if translate:
        try:
            data = translator.translate_result(data, target=translate)
        except translator.TranslatorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    if include_meta:
        return JSONResponse(content={
            **data,
            "_meta": result["_meta"],
            "_review": result["_review"],
            "_seconds": result["_seconds"],
        })
    return data
 
 
@app.post("/extract/batch", response_model=list[BatchEntry], tags=["extract"])
async def extract_batch(
    files: list[UploadFile] = File(..., description="Несколько PDF за один запрос"),
    include_meta: bool = Query(False),
    translate: Optional[str] = Query(
        None, description="Код целевого языка (напр. 'az') — см. /extract"),
):
    """Несколько файлов -> список JSON-объектов (порядок = порядок файлов)."""
    out: list[dict] = []
    for file in files:
        name = file.filename or "unnamed.pdf"
        if not name.lower().endswith(".pdf"):
            out.append({"file": name, "error": "не PDF, пропущен"})
            continue
 
        tmp_path = _save_upload(file, await file.read())
        try:
            result = _run(tmp_path, debug=False)
            data = result["data"]
            if translate:
                try:
                    data = translator.translate_result(data, target=translate)
                except translator.TranslatorError as exc:
                    out.append({"file": name, "error": str(exc)})
                    continue
            entry = {"file": name, **data}
            if include_meta:
                entry["_meta"] = result["_meta"]
                entry["_review"] = result["_review"]
            out.append(entry)
        except HTTPException as exc:
            out.append({"file": name, "error": exc.detail})
        finally:
            os.unlink(tmp_path)
    return out
 
 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)