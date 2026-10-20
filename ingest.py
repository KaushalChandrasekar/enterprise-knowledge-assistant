
"""Load configured HTML webpages into the persistent semantic index."""
import argparse
import json
import os
import hashlib
from pathlib import Path

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from webpage_loader import load_webpages

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "chroma"
MANIFEST = DB / "index.json"
COLLECTION = "company_documents"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 100
URL_CONFIG = ROOT / "policy_urls.json"
load_dotenv(ROOT / ".env")


def embedding_model():
    return os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


def make_embeddings():
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError(
            "Set OPENAI_API_KEY in .env before indexing or querying."
        )

    return OpenAIEmbeddings(model=embedding_model())

def split_documents(pages):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_documents(pages)
    positions = {}
    for chunk in chunks:
        source = chunk.metadata["source"]
        position = positions.get(source, 0)
        positions[source] = position + 1
        # Include metadata so a changed title/section gets refreshed too.
        identity = json.dumps([source, position, chunk.page_content, chunk.metadata],
                              sort_keys=True, ensure_ascii=False)
        chunk.metadata["chunk_id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return chunks


def open_store(embeddings, db_path=DB):
    return Chroma(
        collection_name=COLLECTION, embedding_function=embeddings,
        persist_directory=str(db_path),
        collection_configuration={"hnsw": {"space": "cosine"}},
    )


def index_documents(chunks, embeddings, db_path=DB, rebuild=False):
    if not chunks:
        raise ValueError("No chunks to index; existing documents were not changed.")
    store = open_store(embeddings, db_path)
    if rebuild:
        store.reset_collection()
    by_source = {}
    for chunk in chunks:
        by_source.setdefault(chunk.metadata["source"], []).append(chunk)
    for source, current in by_source.items():
        old_ids = set(store.get(where={"source": source})["ids"])
        new_ids = {c.metadata["chunk_id"] for c in current}
        additions = [c for c in current if c.metadata["chunk_id"] not in old_ids]
        # Add successfully before deleting stale chunks. Unchanged IDs skip embedding.
        if additions:
            store.add_documents(additions, ids=[c.metadata["chunk_id"] for c in additions])
        stale = list(old_ids - new_ids)
        if stale:
            store.delete(ids=stale)
    return store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true",
                        help="Explicitly replace the ENTIRE project collection.")
    parser.add_argument("--urls", nargs="+", help="Load these URLs instead of the config.")
    parser.add_argument("--preview", action="store_true", help="Extract only; no embedding API.")
    args = parser.parse_args()
    pages, errors = [], {}
    # Use command-line URLs if supplied; otherwise use policy_urls.json
    if args.urls:
        urls = args.urls
    else:
        urls = json.loads(URL_CONFIG.read_text(encoding="utf-8"))

    if not isinstance(urls, list) or not urls:
        raise ValueError("policy_urls.json must contain a nonempty JSON list of URLs.")

    # Load webpages only
    web_pages, errors = load_webpages(urls)
    pages.extend(web_pages)

    for url, error in errors.items():
        print(f"Skipped {url}: {error}. Any previously indexed copy is retained.")
    if not pages:
        raise SystemExit("No documents loaded. Existing index was not changed.")
    if args.rebuild and errors:
        raise SystemExit("Rebuild cancelled because some URLs failed; existing index retained.")
    chunks = split_documents(pages)
    if args.preview:
        for source in sorted({c.metadata["source"] for c in chunks}):
            selected = [c for c in chunks if c.metadata["source"] == source]
            print(f"{source}: {len(selected)} chunks")
            print(json.dumps(selected[0].metadata, indent=2, ensure_ascii=False))
            print(selected[0].page_content[:400])
        if errors:
            raise SystemExit(1)
        return
    if MANIFEST.exists() and not args.rebuild:
        info = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if info["embedding_model"] != embedding_model():
            raise ValueError("Embedding model changed. Use --rebuild with all desired sources.")
    embeddings = make_embeddings()
    # Retain the embedding model even after failure, but mark the index not ready.
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({"embedding_model": embedding_model(),
                                    "ready": False}), encoding="utf-8")
    store = index_documents(chunks, embeddings, rebuild=args.rebuild)
    MANIFEST.write_text(json.dumps({
        "embedding_model": embedding_model(), "chunks": len(store.get()["ids"]),
        "chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP,
        "ready": True,
    }, indent=2), encoding="utf-8")
    print(f"Synchronized {len(chunks)} chunks from "
          f"{len({c.metadata['source'] for c in chunks})} documents.")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()