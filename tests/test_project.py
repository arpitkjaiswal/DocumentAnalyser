"""Regression tests for ingestion, transactional storage, retrieval, and HTTP API."""

import io
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from docx import Document as WordDocument
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend import ingest, llm
from backend.main import create_app
from backend.store import VectorStore


def document(name, text):
    return ingest.build_document(name, text.encode(), 0)


def text_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 20 250 Td (The telescope is located in Jaipur.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class IngestTests(unittest.TestCase):
    def test_long_sentence_chunking_makes_progress(self):
        chunks = ingest.chunk_text("x" * 5000, chunk_size=200, overlap=40)
        self.assertTrue(all(0 < len(c) <= 200 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks) - 40 * (len(chunks) - 1), 5000)

    def test_invalid_overlap_is_rejected(self):
        for size, overlap in [(100, 100), (0, 0), (10, -1)]:
            with self.assertRaises(ValueError):
                ingest.chunk_text("text", size, overlap)

    def test_pdf_page_metadata(self):
        result = ingest.build_document("guide.pdf", text_pdf(), 0)
        self.assertIn("Jaipur", result.text)
        self.assertTrue(all(c.page == 1 for c in result.chunks))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = VectorStore()
        self.addCleanup(self.store.close)

    def add(self, name="guide.txt", text="The telescope is in Jaipur."):
        return self.store.add_document(document(name, text), len(text))

    def test_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "documents.sqlite3"
            first = VectorStore(path)
            first.add_document(document("guide.txt", "The telescope is in Jaipur."), 30)
            first.close()
            second = VectorStore(path)
            try:
                self.assertEqual(second.stats()["num_docs"], 1)
                self.assertIn("Jaipur", second.search("telescope")[0]["text"])
                second.reset()
            finally:
                second.close()
            third = VectorStore(path)
            try:
                self.assertEqual(third.stats()["num_docs"], 0)
            finally:
                third.close()

    def test_reupload_replaces_instead_of_appending(self):
        self.assertFalse(self.add())
        self.assertTrue(self.add(text="The telescope moved to Delhi."))
        self.assertEqual(self.store.stats()["num_chunks"], 1)
        self.assertEqual(self.store.search("Jaipur"), [])
        self.assertIn("Delhi", self.store.search("telescope")[0]["text"])

    def test_failed_replacement_preserves_existing_document(self):
        self.add()
        with self.assertRaises(ValueError):
            self.add(text="the and or")
        self.assertIn("Jaipur", self.store.search("telescope")[0]["text"])
        self.assertEqual(self.store.stats()["num_docs"], 1)

    def test_unrelated_query_does_not_produce_sources(self):
        self.add()
        self.assertEqual(self.store.search("cryptocurrency blockchain"), [])

    def test_search_scope_excludes_other_documents(self):
        self.add()
        self.add("office.txt", "The Jaipur office opens at noon.")
        results = self.store.search("Jaipur", document="office.txt")
        self.assertEqual([r["doc"] for r in results], ["office.txt"])
        self.assertEqual(self.store.search("telescope", document="office.txt"), [])

    def test_remove_then_insert_preserves_unique_ids(self):
        self.add("first.txt", "First telescope instrument.")
        self.add("second.txt", "Second telescope instrument.")
        self.store.remove_document("first.txt")
        self.add("third.txt", "Third telescope instrument.")
        ids = [r["chunk_id"] for r in self.store.search("telescope")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_parallel_mutations_preserve_index_and_storage(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda n: self.add(f"{n}.txt", f"Telescope number {n} in Jaipur."),
                    range(12),
                )
            )
        self.assertEqual(self.store.stats()["num_docs"], 12)
        self.assertEqual(len(self.store.search("telescope", top_k=10)), 10)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = VectorStore()
        self.client = TestClient(create_app(self.store))
        self.client.__enter__()
        self.addCleanup(self.store.close)
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, name="guide.txt", content=b"The telescope is located in Jaipur."):
        return self.client.post("/api/ingest", files={"files": (name, content)})

    def ask(self, **kwargs):
        return self.client.post(
            "/api/ask",
            data={
                "question": "Where is the telescope?",
                "mode": "extractive",
                **kwargs,
            },
        )

    def test_frontend_and_health(self):
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        self.assertIn("Ask your documents", self.client.get("/").text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_upload_ask_citation_remove_reset(self):
        self.assertEqual(self.upload().status_code, 200)
        answer = self.ask().json()
        self.assertEqual(answer["mode"], "extractive")
        self.assertIn("Jaipur", answer["answer"])
        numbers = {int(n) for n in re.findall(r"\[(\d+)\]", answer["answer"])}
        self.assertTrue(numbers)
        self.assertTrue(numbers <= {s["n"] for s in answer["sources"]})
        self.assertEqual(
            self.client.post("/api/remove", data={"name": "guide.txt"}).json()[
                "status"
            ]["num_docs"],
            0,
        )
        self.upload()
        self.assertEqual(
            self.client.post("/api/reset").json()["status"]["num_chunks"], 0
        )

    def test_docx_and_pdf_uploads(self):
        word = WordDocument()
        word.add_paragraph("The telescope is in Jaipur.")
        output = io.BytesIO()
        word.save(output)
        for name, content in [
            ("guide.docx", output.getvalue()),
            ("guide.pdf", text_pdf()),
        ]:
            self.assertEqual(self.upload(name, content).status_code, 200)
        result = self.ask(document="guide.pdf").json()
        self.assertEqual(result["sources"][0]["page"], 1)

    def test_partial_upload_returns_both_successes_and_errors(self):
        response = self.client.post(
            "/api/ingest",
            files=[
                ("files", ("good.md", b"Useful telescope instructions.")),
                ("files", ("bad.exe", b"not a document")),
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["added"]), 1)
        self.assertEqual(len(response.json()["errors"]), 1)

    def test_empty_corrupt_unsupported_and_oversized_files(self):
        for name, content in [
            ("empty.txt", b""),
            ("bad.pdf", b"not a PDF"),
            ("bad.exe", b"invalid"),
        ]:
            self.assertEqual(self.upload(name, content).status_code, 400)
        with patch("backend.main.MAX_FILE_BYTES", 20):
            self.assertEqual(self.upload(content=b"x" * 21).status_code, 400)
        self.assertEqual(self.store.stats()["num_docs"], 0)

    def test_excessive_batch_is_rejected(self):
        response = self.client.post(
            "/api/ingest",
            files=[("files", (f"{i}.txt", b"telescope")) for i in range(11)],
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.stats()["num_docs"], 0)

    def test_invalid_questions_and_options(self):
        for data in [
            {"top_k": 0},
            {"top_k": 11},
            {"mode": "invented"},
            {"question": "x" * 2001},
        ]:
            self.assertEqual(self.ask(**data).status_code, 422)
        self.assertEqual(self.ask(question="  ").status_code, 400)
        self.assertEqual(self.ask(document="missing.txt").status_code, 404)

    def test_no_matching_evidence_abstains(self):
        self.upload()
        result = self.ask(question="cryptocurrency blockchain").json()
        self.assertEqual(result["mode"], "none")
        self.assertEqual(result["sources"], [])

    def test_provider_failure_falls_back_without_exposing_error(self):
        self.upload()
        with patch(
            "backend.llm.generate", side_effect=RuntimeError("secret-provider-key")
        ):
            response = self.ask(mode="generative")
        self.assertEqual(response.json()["mode"], "extractive")
        self.assertIn("Jaipur", response.json()["answer"])
        self.assertNotIn("secret-provider-key", response.text)

    def test_filename_path_is_removed(self):
        self.assertEqual(
            self.upload("../../guide.txt").json()["added"][0]["name"], "guide.txt"
        )


class ProviderTests(unittest.TestCase):
    @patch("backend.llm._GEMINI_KEY", "")
    @patch("backend.llm._API_KEY", "test-key")
    def test_generated_answer_requires_valid_citations(self):
        for content in ["", "An uncited answer.", "Wrong source [9]"]:
            response = Mock()
            response.json.return_value = {
                "choices": [{"message": {"content": content}}]
            }
            with patch("backend.llm.requests.post", return_value=response):
                with self.assertRaises(ValueError):
                    llm.generate("Where?", [{"doc": "guide.txt", "text": "Jaipur."}])

    @patch("backend.llm._GEMINI_KEY", "")
    @patch("backend.llm._API_KEY", "test-key")
    def test_openai_answer_and_timeout(self):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "Jaipur [1]"}}]
        }
        with patch("backend.llm.requests.post", return_value=response) as post:
            self.assertEqual(
                llm.generate("Where?", [{"doc": "guide.txt", "text": "Jaipur."}]),
                "Jaipur [1]",
            )
            self.assertEqual(post.call_args.kwargs["timeout"], 30)

    @patch("backend.llm._GEMINI_KEY", "test-key")
    def test_gemini_answer(self):
        response = Mock()
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Jaipur [1]"}]}}]
        }
        with patch("backend.llm.requests.post", return_value=response):
            self.assertEqual(
                llm.generate("Where?", [{"doc": "guide.txt", "text": "Jaipur."}]),
                "Jaipur [1]",
            )


if __name__ == "__main__":
    unittest.main()
