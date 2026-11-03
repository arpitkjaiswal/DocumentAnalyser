"""DocumentAnalyser API and frontend. Run with one Uvicorn worker."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

load_dotenv()

from backend import ingest, llm, rag  # noqa: E402 - load .env before provider configuration
from backend.store import VectorStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
logger = logging.getLogger(__name__)


def create_app(store: VectorStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.store = (
            store
            if store is not None
            else VectorStore(
                os.environ.get(
                    "DOCUMENT_DB_PATH", str(ROOT / "data" / "documents.sqlite3")
                )
            )
        )
        try:
            yield
        finally:
            if store is None:
                application.state.store.close()

    application = FastAPI(title="DocumentAnalyser", version="2.0.0", lifespan=lifespan)

    @application.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "llm": llm.info()}

    @application.get("/api/status")
    def status(request: Request) -> dict:
        return {
            **request.app.state.store.stats(),
            "llm": llm.info(),
            "limits": {"max_file_bytes": MAX_FILE_BYTES, "max_files": MAX_FILES},
        }

    @application.post("/api/ingest")
    async def ingest_files(
        request: Request, files: list[UploadFile] = File(...)
    ) -> dict:
        if len(files) > MAX_FILES:
            for file in files:
                await file.close()
            raise HTTPException(400, f"Upload at most {MAX_FILES} files at a time.")
        added, errors = [], []
        for file in files:
            name = (file.filename or "").replace("\\", "/").split("/")[-1]
            try:
                if not name or len(name) > 180 or any(ord(c) < 32 for c in name):
                    raise ValueError(
                        "Use a filename of 1–180 characters without control characters."
                    )
                if Path(name).suffix.lower() not in EXTENSIONS:
                    raise ValueError("Unsupported file type. Use PDF, DOCX, TXT or MD.")
                data = await file.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("File exceeds the 10 MB limit.")
                if not data:
                    raise ValueError("The file is empty.")
                try:
                    doc = await run_in_threadpool(ingest.build_document, name, data, 0)
                except ValueError:
                    raise
                except Exception as exc:
                    raise ValueError(
                        "Could not read this document. Check that it is a valid, unencrypted file."
                    ) from exc
                replaced = await run_in_threadpool(
                    request.app.state.store.add_document, doc, len(data)
                )
                added.append(
                    {"name": name, "chunks": len(doc.chunks), "replaced": replaced}
                )
            except ValueError as exc:
                errors.append({"name": name, "error": str(exc)})
            except Exception:
                logger.exception("Document indexing failed")
                errors.append(
                    {"name": name, "error": "Indexing failed. Please try again."}
                )
            finally:
                await file.close()
        if not added and errors:
            raise HTTPException(400, errors[0]["error"])
        return {
            "added": added,
            "errors": errors,
            "status": request.app.state.store.stats(),
        }

    @application.post("/api/ask")
    def ask(
        request: Request,
        question: str = Form(..., min_length=1, max_length=2000),
        top_k: int = Form(5, ge=1, le=10),
        mode: Literal["auto", "extractive", "generative"] = Form("auto"),
        document: str | None = Form(None, max_length=180),
    ) -> dict:
        vector_store = request.app.state.store
        if document and not any(
            d["name"] == document for d in vector_store.stats()["docs"]
        ):
            raise HTTPException(404, "This document is no longer in the library.")
        try:
            return rag.answer(
                question,
                top_k=top_k,
                mode=mode,
                document=document or None,
                store=vector_store,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @application.post("/api/remove")
    def remove(request: Request, name: str = Form(..., max_length=180)) -> dict:
        vector_store = request.app.state.store
        if not vector_store.remove_document(name):
            raise HTTPException(404, "This document is no longer in the library.")
        return {"removed": name, "status": vector_store.stats()}

    @application.post("/api/reset")
    def reset(request: Request) -> dict:
        request.app.state.store.reset()
        return {"status": request.app.state.store.stats()}

    application.mount(
        "/static", StaticFiles(directory=str(ROOT / "static")), name="static"
    )

    @application.get("/")
    def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    return application


app = create_app()
