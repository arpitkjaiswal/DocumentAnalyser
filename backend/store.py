"""SQLite-backed documents with a process-local TF-IDF retrieval index.

A mutation is validated before it is committed. The search index is rebuilt from
SQLite on startup; run a single server worker so its cache stays consistent.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from backend.ingest import Chunk, Document


class VectorStore:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                name TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc TEXT NOT NULL REFERENCES documents(name) ON DELETE CASCADE,
                text TEXT NOT NULL,
                page INTEGER
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc);
        """)
        self._chunks = [
            Chunk(**dict(row))
            for row in self._db.execute(
                "SELECT id, doc, text, page FROM chunks ORDER BY id"
            )
        ]
        self._vectorizer, self._matrix = self._fit(self._chunks)

    @staticmethod
    def _fit(chunks: list[Chunk]):
        if not chunks:
            return None, None
        vectorizer = TfidfVectorizer(
            lowercase=True,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=1,
        )
        try:
            matrix = vectorizer.fit_transform(c.text for c in chunks)
        except ValueError as exc:
            raise ValueError(
                "The document has no searchable words. Try a document with more text."
            ) from exc
        return vectorizer, matrix

    def add_document(self, document: Document, size_bytes: int) -> bool:
        """Atomically add or replace a filename; return whether it replaced one."""
        # Validate each incoming document, even if the existing corpus is valid.
        self._fit(document.chunks)
        with self._lock:
            replaced = any(c.doc == document.name for c in self._chunks)
            incoming = [
                Chunk(0, document.name, c.text, c.page) for c in document.chunks
            ]
            chunks = [c for c in self._chunks if c.doc != document.name] + incoming
            vectorizer, matrix = self._fit(chunks)
            with self._db:
                self._db.execute(
                    "DELETE FROM documents WHERE name = ?", (document.name,)
                )
                self._db.execute(
                    "INSERT INTO documents VALUES (?, ?, ?)",
                    (document.name, size_bytes, datetime.now(timezone.utc).isoformat()),
                )
                for chunk in incoming:
                    cursor = self._db.execute(
                        "INSERT INTO chunks (doc, text, page) VALUES (?, ?, ?)",
                        (chunk.doc, chunk.text, chunk.page),
                    )
                    chunk.id = cursor.lastrowid
            self._chunks, self._vectorizer, self._matrix = chunks, vectorizer, matrix
            return replaced

    def search(
        self, query: str, top_k: int = 5, document: str | None = None
    ) -> list[dict]:
        if not 1 <= top_k <= 10:
            raise ValueError("Source count must be between 1 and 10.")
        with self._lock:
            if self._vectorizer is None:
                return []
            query_vector = self._vectorizer.transform([query])
            scores = cosine_similarity(query_vector, self._matrix)[0]
            # Zero-similarity passages must never be presented as evidence.
            candidates = [
                i
                for i, c in enumerate(self._chunks)
                if scores[i] > 0 and (document is None or c.doc == document)
            ]
            candidates.sort(key=lambda i: (-scores[i], self._chunks[i].id))
            return [
                {
                    "chunk_id": self._chunks[i].id,
                    "doc": self._chunks[i].doc,
                    "page": self._chunks[i].page,
                    "text": self._chunks[i].text,
                    "score": round(float(scores[i]), 4),
                }
                for i in candidates[:top_k]
            ]

    def remove_document(self, name: str) -> bool:
        with self._lock:
            chunks = [c for c in self._chunks if c.doc != name]
            if len(chunks) == len(self._chunks):
                return False
            vectorizer, matrix = self._fit(chunks)
            with self._db:
                self._db.execute("DELETE FROM documents WHERE name = ?", (name,))
            self._chunks, self._vectorizer, self._matrix = chunks, vectorizer, matrix
            return True

    def reset(self) -> None:
        with self._lock:
            with self._db:
                self._db.execute("DELETE FROM documents")
            self._chunks, self._vectorizer, self._matrix = [], None, None

    def stats(self) -> dict:
        with self._lock:
            docs = [
                dict(row)
                for row in self._db.execute("""
                SELECT d.name, d.size_bytes, d.added_at, COUNT(c.id) AS chunks
                FROM documents d JOIN chunks c ON c.doc = d.name
                GROUP BY d.name ORDER BY d.added_at DESC, d.name
            """)
            ]
            return {
                "num_docs": len(docs),
                "num_chunks": len(self._chunks),
                "docs": docs,
                "size_bytes": sum(d["size_bytes"] for d in docs),
                "vocab_size": len(self._vectorizer.vocabulary_)
                if self._vectorizer
                else 0,
            }

    def close(self) -> None:
        with self._lock:
            self._db.close()
