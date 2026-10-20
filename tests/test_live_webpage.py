"""Opt-in live extraction test; no embeddings or LLM API calls."""
import os
import unittest

from webpage_loader import load_webpages

URL = "https://handbook.gitlab.com/handbook/people-group/time-off-and-absence/time-off-types/"


@unittest.skipUnless(os.getenv("RUN_LIVE_WEB_TESTS") == "1", "Live web test is opt-in")
class LiveWebpageTests(unittest.TestCase):
    def test_official_policy_extraction(self):
        documents, errors = load_webpages(URL)
        self.assertFalse(errors, errors)
        self.assertTrue(documents)
        self.assertTrue(all(d.metadata["source"] == URL for d in documents))
        self.assertTrue(all(d.metadata["title"] == "Time Off Types" for d in documents))
        self.assertIn("Flexible Paid Time Off", "\n".join(d.page_content for d in documents))


if __name__ == "__main__":
    unittest.main()
