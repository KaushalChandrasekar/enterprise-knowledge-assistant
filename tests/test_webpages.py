"""Mock HTTP and model calls; use a real temporary Chroma database."""
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from ingest import index_documents, split_documents
from rag import build_context, format_sources, generate_answer, UNKNOWN
from webpage_loader import extract_webpage, load_webpages, normalize_url

URL = "https://handbook.gitlab.com/handbook/example/"
HTML = """<html><head><title>Policy | Handbook</title></head><body>
<header>Global Header</header><nav>Navigation menu</nav><aside>Sidebar</aside>
<main><nav>On this page</nav><article><header><h1>Example Policy</h1></header>
<p>These are fictional test rules.</p><h2>Annual leave</h2>
<p>Employees coordinate time away with their manager.</p>
<ul><li>Plan coverage.</li><li>Record approved dates.<ul><li>Notify the team.</li></ul></li></ul>
<h2>Expenses</h2><p>Keep an itemized receipt.</p>
<div class="cookie-notice">Accept cookies</div></article></main>
<footer>Footer legal links</footer><script>tracking()</script></body></html>"""


def response(text="", status=200, content_type="text/html"):
    result = Mock(text=text, status_code=status, headers={"Content-Type": content_type})
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return result


def mock_session(pages=None, robots="User-agent: *\nAllow: /", robots_status=200):
    session = Mock()
    pages = pages or {URL: response(HTML)}

    def get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return response(robots, robots_status, "text/plain")
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value

    session.get.side_effect = get
    return session


class CountingEmbeddings(Embeddings):
    def __init__(self):
        self.texts = []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


class WebpageTests(unittest.TestCase):
    def test_extract_content_and_remove_chrome(self):
        docs = extract_webpage(HTML, URL)
        text = "\n".join(d.page_content for d in docs)
        for expected in ["Example Policy", "Annual leave", "Expenses", "Plan coverage.",
                         "Notify the team.", "Keep an itemized receipt."]:
            self.assertIn(expected, text)
        for unwanted in ["Navigation menu", "Footer legal links", "Global Header",
                         "Sidebar", "Accept cookies", "tracking()", "On this page"]:
            self.assertNotIn(unwanted, text)
        self.assertIn("\n\n", text)
        self.assertIn("- Plan coverage.", text)

    def test_metadata_survives_chunking(self):
        chunks = split_documents(extract_webpage(HTML, URL))
        self.assertTrue(all(c.metadata["source"] == URL for c in chunks))
        self.assertTrue(all(c.metadata["title"] == "Example Policy" for c in chunks))
        self.assertTrue(all(c.metadata["document_type"] == "webpage" for c in chunks))
        self.assertTrue(any("Annual leave" in c.metadata["section"] for c in chunks))
        self.assertTrue(all("page" not in c.metadata for c in chunks))

    def test_fallback_without_main_or_article(self):
        html = "<body><nav>Menu</nav><div><h1>Policy</h1><p>Keep receipts for claims.</p></div></body>"
        self.assertIn("Keep receipts", extract_webpage(html, URL)[0].page_content)

    def test_role_main_and_plain_body_fallbacks(self):
        for html in ["<div role='main'><h1>Policy</h1><p>Keep receipts.</p></div>",
                     "<html><body>Keep itemized receipts for every claim.</body></html>"]:
            self.assertTrue(extract_webpage(html, URL))

    def test_gitlab_content_container_is_preferred(self):
        html = "<main><p>Outer junk</p><div class='td-content'><h1>Policy</h1><p>Keep receipts.</p></div></main>"
        self.assertNotIn("Outer junk", extract_webpage(html, URL)[0].page_content)

    def test_empty_and_login_pages_are_rejected(self):
        for html in ["<html><body></body></html>", "<h1>Only a title</h1>",
                     "<title>Sign in</title><p>Enter your password to view documents.</p>"]:
            with self.subTest(html=html), self.assertRaises(ValueError):
                extract_webpage(html, URL)

    def test_invalid_urls_do_not_make_http_calls(self):
        for invalid in ["not a url", "ftp://example.com/a", "https://", "https://a b/",
                        "https://user:secret@example.com/", "https://example.com:bad/", None]:
            session = mock_session()
            docs, errors = load_webpages([invalid], session=session, interval=0)
            self.assertEqual(docs, [])
            self.assertTrue(errors)
            session.get.assert_not_called()

    def test_normalization_and_duplicate_urls(self):
        self.assertEqual(normalize_url(URL + "#leave"), URL)
        session = mock_session()
        docs, errors = load_webpages([URL, URL + "#leave"], session=session, interval=0)
        self.assertTrue(docs)
        self.assertFalse(errors)
        self.assertEqual(session.get.call_count, 2)  # robots plus one page

    def test_multiple_pages_and_single_url(self):
        other = URL + "second/"
        session = mock_session({URL: response(HTML), other: response(HTML)})
        docs, errors = load_webpages([URL, other], session=session, interval=0)
        self.assertEqual({d.metadata["source"] for d in docs}, {URL, other})
        self.assertFalse(errors)
        self.assertEqual(session.get.call_count, 3)  # robots cached per origin
        self.assertTrue(load_webpages(URL, session=mock_session(), interval=0)[0])
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 20)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIn("EnterpriseKnowledgeAssistant", kwargs["headers"]["User-Agent"])

    def test_http_failure_and_timeout_preserve_successful_pages(self):
        for failure in [response("", 404), response("", 403), response("", 500),
                        requests.Timeout("timed out"), requests.ConnectionError("offline")]:
            bad = URL + "bad/"
            docs, errors = load_webpages([URL, bad],
                                        session=mock_session({URL: response(HTML), bad: failure}),
                                        interval=0)
            self.assertEqual({d.metadata["source"] for d in docs}, {URL})
            self.assertIn(bad, errors)

    def test_robots_denial_and_failure_prevent_page_request(self):
        for session in [mock_session(robots="User-agent: *\nDisallow: /handbook/"),
                        mock_session(robots_status=503), mock_session(robots_status=403),
                        mock_session(robots="<html>Login</html>")]:
            docs, errors = load_webpages(URL, session=session, interval=0)
            self.assertFalse(docs)
            self.assertTrue(errors)
            self.assertEqual(session.get.call_count, 1)

    def test_missing_robots_is_allowed_but_redirects_and_non_html_are_not(self):
        self.assertTrue(load_webpages(URL, session=mock_session(robots_status=404), interval=0)[0])
        for reply in [response("", 302), response("binary", content_type="application/pdf")]:
            docs, errors = load_webpages(URL, session=mock_session({URL: reply}), interval=0)
            self.assertFalse(docs)
            self.assertTrue(errors)

    @patch("webpage_loader.time.sleep")
    def test_crawl_delay_is_respected(self, sleep):
        rules = "User-agent: *\nAllow: /\nCrawl-delay: 3"
        load_webpages(URL, session=mock_session(robots=rules), interval=1)
        self.assertGreater(sleep.call_args.args[0], 2.5)

    def test_repeat_ingestion_and_changed_content_preserve_other_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            embeddings = CountingEmbeddings()
            local = split_documents([Document(page_content="Local leave policy.",
                                              metadata={"source": "local.txt"})])
            web = split_documents(extract_webpage(HTML, URL))
            store = index_documents(local + web, embeddings, folder)
            count = len(store.get()["ids"])
            embedded = len(embeddings.texts)
            index_documents(web, embeddings, folder)
            self.assertEqual(len(store.get()["ids"]), count)
            self.assertEqual(len(embeddings.texts), embedded)
            changed = split_documents(extract_webpage(
                "<article><h1>New Policy</h1><p>New rules require prior approval.</p></article>", URL))
            index_documents(changed, embeddings, folder)
            stored = store.get(where={"source": URL})
            self.assertEqual(len(stored["ids"]), len(changed))
            self.assertNotIn("Keep an itemized receipt.", "\n".join(stored["documents"]))
            self.assertEqual(len(store.get(where={"source": "local.txt"})["ids"]), 1)

    def test_legacy_ids_are_replaced_for_only_the_same_source(self):
        with tempfile.TemporaryDirectory() as folder:
            embeddings = CountingEmbeddings()
            current = split_documents([Document(page_content="Old policy", metadata={"source": URL})])
            store = index_documents(current, embeddings, folder)
            store.delete(ids=store.get()["ids"])
            store.add_documents([Document(page_content="Old policy", metadata={"source": URL})],
                                ids=["chunk-0001"])
            index_documents(current, embeddings, folder)
            self.assertEqual(store.get()["ids"], [current[0].metadata["chunk_id"]])

    def test_web_citations_only_include_used_evidence(self):
        chunks = split_documents(extract_webpage(HTML, URL))
        llm = Mock()
        llm.invoke.return_value.content = "Coordinate with your manager. [S2]"
        result = generate_answer("How to plan leave?", chunks, llm)
        self.assertEqual(list(result["sources"]), ["S2"])
        formatted = format_sources(result["sources"])
        self.assertIn("Example Policy", formatted)
        self.assertIn(f"[{URL}](<{URL}>)", formatted)
        self.assertNotIn("PDF page", formatted)
        _, all_sources = build_context(chunks)
        self.assertEqual(format_sources(all_sources).count(f"](<{URL}>)"), 1)
        llm.invoke.return_value.content = UNKNOWN
        self.assertEqual(format_sources(generate_answer("Missing?", chunks, llm)["sources"]), "")

    def test_local_citation_formats(self):
        self.assertIn("manual.pdf, PDF page 2", format_sources({
            "S1": {"document": "manual.pdf", "page": 2}}))
        self.assertIn("policy.txt", format_sources({"S1": {"document": "policy.txt"}}))

    def test_failed_embedding_does_not_delete_previous_source(self):
        with tempfile.TemporaryDirectory() as folder:
            embeddings = CountingEmbeddings()
            original = split_documents([Document(page_content="Old approved policy.",
                                                 metadata={"source": URL})])
            store = index_documents(original, embeddings, folder)
            changed = split_documents([Document(page_content="Updated approved policy.",
                                                metadata={"source": URL})])
            with patch.object(embeddings, "embed_documents", side_effect=RuntimeError("API offline")):
                with self.assertRaisesRegex(RuntimeError, "API offline"):
                    index_documents(changed, embeddings, folder)
            self.assertEqual(store.get(where={"source": URL})["documents"],
                             ["Old approved policy."])

    def test_failed_download_produces_no_replacement_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            embeddings = CountingEmbeddings()
            original = split_documents(extract_webpage(HTML, URL))
            store = index_documents(original, embeddings, folder)
            docs, errors = load_webpages(URL, session=mock_session({URL: requests.Timeout()}),
                                        interval=0)
            self.assertTrue(errors)
            with self.assertRaisesRegex(ValueError, "No chunks"):
                index_documents(split_documents(docs), embeddings, folder)
            self.assertEqual(len(store.get()["ids"]), len(original))

    def test_source_ids_do_not_depend_on_other_documents(self):
        pages = extract_webpage(HTML, URL)
        local = Document(page_content="A separate source.", metadata={"source": "local.txt"})
        alone = [c.metadata["chunk_id"] for c in split_documents(pages)]
        together = [c.metadata["chunk_id"] for c in split_documents([local] + pages)
                    if c.metadata["source"] == URL]
        self.assertEqual(alone, together)


if __name__ == "__main__":
    unittest.main()
