"""Offline contract tests; fake embeddings do not test semantic quality."""
import tempfile
import unittest
from types import SimpleNamespace

import gc

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from ingest import index_documents, open_store, split_documents
from webpage_loader import extract_webpage
from rag import UNKNOWN, build_context, generate_answer, retrieve, validate_citations


class TestEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0] if "leave" in text.lower() else [0.0, 1.0]


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        self.messages = messages
        return SimpleNamespace(content=self.answer)


class PipelineTests(unittest.TestCase):
    def test_load_and_split_preserve_metadata(self):
        url = "https://example.com/leave-policy"
        html = (
            "<html><head><title>Leave Policy</title></head><body><main>"
            "<h1>Leave Policy</h1><h2>Annual leave</h2>"
            f"<p>{'Employees can request annual leave through their manager. ' * 30}</p>"
            "</main></body></html>"
        )
        chunks = split_documents(extract_webpage(html, url))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.page_content) <= 600 for c in chunks))
        self.assertTrue(all(c.metadata["source"] == url for c in chunks))
        self.assertTrue(all(c.metadata["title"] == "Leave Policy" for c in chunks))
        self.assertTrue(all("page" not in c.metadata for c in chunks))
        ids = [c.metadata["chunk_id"] for c in chunks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_webpage_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_webpage("<html><body></body></html>", "https://example.com/empty")

    
    def test_index_persistence_and_explicit_rebuild(self):
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as folder:

            chunks = split_documents([
                Document(
                    page_content="Annual leave is 18 days.",
                    metadata={
                        "source": "https://example.com/leave",
                        "title": "Leave Policy",
                        "section": "Annual leave"
                    }
                ),
                Document(
                    page_content="Meal reimbursement is INR 750.",
                    metadata={
                        "source": "https://example.com/expenses",
                        "title": "Expense Policy",
                        "section": "Meal expenses"
                    }
                )
            ])

            embeddings = TestEmbeddings()

            store = index_documents(
                chunks, embeddings, folder
            )

            result = retrieve(
                "leave entitlement", store, k=1
            )

            self.assertEqual(
                result[0].metadata["source"],
                "https://example.com/leave"
            )

            reopened = open_store(embeddings, folder)

            self.assertEqual(
                len(reopened.get()["ids"]), 2
            )

            index_documents(
                chunks, embeddings, folder
            )

            self.assertEqual(
                len(reopened.get()["ids"]), 2
            )

            index_documents(
                chunks[:1],
                embeddings,
                folder,
                rebuild=True
            )

            self.assertEqual(
                len(
                    open_store(
                        embeddings, folder
                    ).get()["ids"]
                ), 1
            )

            # Release Python references to ChromaDB objects.
            del store
            del reopened

            # Request garbage collection before cleanup.
            gc.collect()

    def test_prompt_and_citation_mapping(self):
        chunks = split_documents([Document(page_content="Annual leave is 18 days.",
                                           metadata={"source": "https://example.com/leave-policy", "title": "Leave Policy", "section": "Annual leave"})])
        llm = FakeLLM("Employees receive 18 days of annual leave. [S1]")
        result = generate_answer("How much leave?", chunks, llm)
        self.assertEqual(result["sources"]["S1"]["document"], "https://example.com/leave-policy")
        self.assertEqual(result["sources"]["S1"]["title"], "Leave Policy")
        self.assertNotIn("page", result["sources"]["S1"])
        self.assertIn("Annual leave is 18 days.", llm.messages[1].content)
        self.assertIn("How much leave?", llm.messages[1].content)

    def test_invalid_citations_and_missing_citations_are_blocked(self):
        for answer in ["Leave is 18 days. [S99]", "Leave is 18 days."]:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                validate_citations(answer, {"S1": {}})

    def test_unknown_and_empty_context(self):
        llm = FakeLLM(UNKNOWN)
        self.assertEqual(generate_answer("Revenue?", [], llm)["answer"], UNKNOWN)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(validate_citations(UNKNOWN, {"S1": {}}), [])

    def test_bad_query_and_k(self):
        with self.assertRaises(ValueError):
            retrieve(" ", None)
        with self.assertRaises(ValueError):
            retrieve("leave", None, k=0)


if __name__ == "__main__":
    unittest.main()
