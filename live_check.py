"""Paid GitLab RAG smoke checks; keyword matches are not a faithfulness score."""
import json
import os
import re

from langchain_openai import ChatOpenAI

from ingest import MANIFEST, URL_CONFIG, embedding_model, make_embeddings, open_store
from rag import UNKNOWN, format_sources, generate_answer, retrieve

BASE = "https://handbook.gitlab.com/handbook/"
# Each inner tuple contains acceptable wording for ONE required concept.
CASES = [
    {
        "question": "Under GitLab's Flexible PTO policy, what should I coordinate with my manager and plan before taking PTO?",
        "sources": [BASE + "people-group/time-off-and-absence/time-off-types/"],
        "concepts": [("manager",), ("coverage",)],
    },
    {
        "question": "Under GitLab's Global Travel and Expense Policy, whose approval must team members receive for proposed business-critical trips?",
        "sources": [BASE + "finance/expenses/"],
        "concepts": [("manager",), ("approval", "approve", "authorization", "authorisation")],
    },
    {
        "question": "According to the Purpose section of GitLab's People Policies, how should I contact People Operations about internal policies?",
        "sources": [BASE + "people-policies/"],
        "concepts": [("compass",)],
    },
    {
        "question": "Under GitLab's Leave Types policy, how many weeks of paid Parental Leave does GitLab provide?",
        "sources": [BASE + "people-group/time-off-and-absence/leave-types/"],
        "concepts": [("16", "sixteen"), ("weeks", "week")],
    },
    {
        "question": "According to GitLab's General and Entity Specific Benefits page, where should team members send benefits-related questions?",
        "sources": [BASE + "total-rewards/benefits/general-and-entity-benefits/"],
        "concepts": [("compass",)],
    },
    {
        "question": "What is the private bank account number of GitLab employee Fictional Test Person ZXQ-984731?",
        "sources": [],
        "concepts": [],
    },
]


def contains_term(text, term):
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I) is not None


def check_result(case, chunks, result):
    """Return failure reasons, separating retrieval from answer/citation checks."""
    cited = {source["document"] for source in result["sources"].values()}
    retrieved = {chunk.metadata["source"] for chunk in chunks}
    if not case["sources"]:
        return [] if result["answer"].strip() == UNKNOWN and not cited else [
            "Expected the exact insufficient-information response without citations."]
    problems = []
    expected = set(case["sources"])
    if not expected.intersection(retrieved):
        problems.append("Expected policy was not retrieved in the top three chunks.")
    if not expected.intersection(cited):
        problems.append("Expected policy was not cited in the answer.")
    if not cited.issubset(retrieved):
        problems.append("Answer cited a source that was not retrieved.")
    for alternatives in case["concepts"]:
        if not any(contains_term(result["answer"], term) for term in alternatives):
            problems.append("Missing expected answer concept: " + " / ".join(alternatives))
    return problems


def main():
    if not MANIFEST.exists():
        raise ValueError("Run python ingest.py first.")
    info = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not info.get("ready", True):
        raise ValueError("Index update was interrupted. Re-run ingestion first.")
    if info["embedding_model"] != embedding_model():
        raise ValueError("Embedding model changed; rebuild the index first.")
    configured = json.loads(URL_CONFIG.read_text(encoding="utf-8"))
    if not isinstance(configured, list) or not all(isinstance(url, str) for url in configured):
        raise ValueError("policy_urls.json must contain a list of URL strings.")
    required = {url for case in CASES for url in case["sources"]}
    if required - set(configured):
        raise ValueError("Smoke-check sources missing from policy_urls.json: "
                         + ", ".join(sorted(required - set(configured))))
    store = open_store(make_embeddings())
    indexed = {metadata["source"] for metadata in store.get()["metadatas"]}
    if required - indexed:
        raise ValueError("Required policies are not indexed. Run python ingest.py. Missing: "
                         + ", ".join(sorted(required - indexed)))
    for source in sorted(set(configured) - indexed):
        print(f"WARNING: Configured URL not indexed: {source}")
    if any(not source.startswith(BASE) for source in indexed):
        raise ValueError("Index contains non-GitLab sources. For this webpage-only project, "
                         "run python ingest.py after fixing inaccessible URLs.")