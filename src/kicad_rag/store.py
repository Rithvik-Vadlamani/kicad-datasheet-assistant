from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .config import Project, atomic_json, canonical_hash, digest, local_path
from .ingest import PIPELINE_VERSION, chunk_document, extract
from .kicad import schematic_hashes
from .providers import Provider


def fingerprint(root: Path, project: Project) -> str:
    if project.schematic:
        if schematic_hashes(Path(project.schematic)) != project.schematic_hashes:
            raise ValueError("Schematic or a child sheet changed. Run sync-kicad, then index.")
    s = project.settings
    return canonical_hash({"pipeline": PIPELINE_VERSION, "search_preprocessing": "camel-boundaries-v1", "documents": [
        {**d.model_dump(), "sha": digest(local_path(root, d.path))} for d in project.documents],
        "netlist": digest(local_path(root, project.netlist)) if project.netlist else None,
        "parts": project.component_parts,
        "embedding": embedding_signature(s),
        "extraction": extraction_signature(s),
        "chunking": [s.chunk_chars, s.overlap_chars]})


def embedding_signature(settings):
    identity = None
    if settings.embedding_provider == "local":
        from .retrieval_adapters import local_model_identity
        identity = ["token-window-mean-v1", local_model_identity(settings.embedding_model)]
    return [settings.embedding_provider, settings.embedding_model, settings.ollama_url,
            settings.embedding_document_prefix, identity]


def extraction_signature(settings):
    if settings.ocr_mode == "off":
        return [PIPELINE_VERSION, "ocr-off"]
    from .ocr import engine_identity
    return [PIPELINE_VERSION, settings.ocr_mode, settings.ocr_language, settings.ocr_dpi,
            engine_identity(settings.ocr_language)]


def searchable(text):
    # Extraction often fuses manufacturer table headings (e.g. AbsoluteMaximumRatings).
    # Expand only the FTS representation; original quote text and page geometry remain unchanged.
    expanded = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return text + "\n" + expanded


class Store:
    def __init__(self, root: Path):
        folder = root / ".rag"
        folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(folder / "index.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, doc_id TEXT NOT NULL,
                page INTEGER NOT NULL, payload TEXT NOT NULL, embedding TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, body,
                tokenize='unicode61 remove_diacritics 0');
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (doc_id TEXT, page INTEGER, text TEXT NOT NULL,
                PRIMARY KEY(doc_id, page));
            CREATE TABLE IF NOT EXISTS page_layouts (doc_id TEXT, page INTEGER, payload TEXT NOT NULL,
                PRIMARY KEY(doc_id, page));
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def meta(self, key: str):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def require_fresh(self, root: Path, project: Project):
        if self.meta("fingerprint") != fingerprint(root, project):
            raise ValueError("Index is missing or stale. Run index for this project before asking questions.")

    def chunks(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM chunks ORDER BY id")]

    def page(self, doc_id: str, number: int) -> str:
        row = self.db.execute("SELECT text FROM pages WHERE doc_id=? AND page=?", (doc_id, number)).fetchone()
        if not row:
            raise ValueError(f"Unknown source page {doc_id}:{number}")
        return row[0]

    def page_layout(self, doc_id: str, number: int) -> dict:
        row = self.db.execute("SELECT payload FROM page_layouts WHERE doc_id=? AND page=?", (doc_id, number)).fetchone()
        if not row:
            raise ValueError(f"Unknown source layout {doc_id}:{number}; reindex with the current pipeline")
        return json.loads(row[0])


def index_project(root: Path, project: Project, *, force: bool = False, provider=None, cancel=None) -> dict:
    def cancellation_point():
        if cancel is not None and cancel.is_set():
            raise ValueError('Indexing cancelled; previous index preserved')
    cancellation_point()
    before = fingerprint(root, project)
    provider = provider or Provider(project.settings)
    with Store(root) as store:
        if not force and store.meta("fingerprint") == before:
            return {**(store.meta("stats") or {}), "unchanged": True}
        chunks, all_pages, warnings, cache_writes, layouts = [], [], [], {}, []
        extraction_hits = 0
        for doc in project.documents:
            cancellation_point()
            path = local_path(root, doc.path)
            sha = digest(path)
            key = canonical_hash([extraction_signature(project.settings), sha])
            cached = store.db.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
            shared = (Path(project.settings.shared_extraction_cache).expanduser() / f'{key}.json'
                      if project.settings.shared_extraction_cache else None)
            if not cached and shared and shared.exists() and not force:
                shared_data = json.loads(shared.read_text())
                if shared_data.get('source_sha256') != sha or shared_data.get('pipeline') != PIPELINE_VERSION:
                    raise ValueError('Shared extraction cache identity mismatch; force a fresh extraction')
                if shared_data.get('payload_hash') != canonical_hash(shared_data['payload']):
                    raise ValueError('Shared extraction cache changed; force a fresh extraction')
                cached = (json.dumps(shared_data['payload']),)
            if cached and not force:
                pages, issues = json.loads(cached[0])
                extraction_hits += 1
            else:
                pages, issues = extract(path, project.settings)
                cache_writes[key] = json.dumps([pages, issues])
                if shared:
                    atomic_json(shared, {'source_sha256': sha, 'pipeline': PIPELINE_VERSION,
                                         'payload': [pages, issues], 'payload_hash': canonical_hash([pages, issues])})
            warnings.extend(issues)
            chunks.extend(chunk_document(doc, pages, project.settings, sha))
            layouts.extend((doc.id, p["page"], json.dumps(p)) for p in pages)
            all_pages.extend((doc.id, p["page"], p["text"] + "\n" + "\n".join(
                "\n".join(t) for t in p["tables"])) for p in pages)
        if not chunks:
            raise ValueError("No chunks to index. Add at least one readable datasheet first.")
        s = project.settings
        signature = embedding_signature(s)
        old = {}
        if not force and store.meta("embedding_signature") == signature:
            old = {r[0]: r[1] for r in store.db.execute("SELECT id, embedding FROM chunks") if r[1]}
        pending = [c for c in chunks if c["id"] not in old] if s.embedding_provider != "none" else []
        embeddings = {}
        if pending:
            cancellation_point()
            texts = [s.embedding_document_prefix + f"{c['title']} Parts: {', '.join(c['parts'])}\n{c['text']}" for c in pending]
            vectors = provider.embed(texts)
            embeddings = {c["id"]: json.dumps(v.tolist()) for c, v in zip(pending, vectors, strict=True)}
        if before != fingerprint(root, project):
            raise ValueError("Project files changed during indexing. Run index again.")
        cancellation_point()
        stats = {"documents": len(project.documents), "pages": len(all_pages), "chunks": len(chunks),
                 "extraction_cache_hits": extraction_hits, "new_embeddings": len(pending),
                 "embedding_provider": s.embedding_provider, "warnings": warnings}
        # Replace the visible index in one transaction, only after extraction/embedding succeeds.
        with store.db:
            for table in ("chunks", "search", "pages", "page_layouts"):
                store.db.execute(f"DELETE FROM {table}")
            store.db.executemany("INSERT INTO cache VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                                 cache_writes.items())
            store.db.executemany("INSERT INTO pages VALUES (?, ?, ?)", all_pages)
            store.db.executemany("INSERT INTO page_layouts VALUES (?, ?, ?)", layouts)
            for c in chunks:
                vector = embeddings.get(c["id"], old.get(c["id"])) if s.embedding_provider != "none" else None
                store.db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                                 (c["id"], c["doc_id"], c["page"], json.dumps(c), vector))
                store.db.execute("INSERT INTO search VALUES (?, ?)",
                                 (c["id"], searchable(f"{c['title']} {' '.join(c['parts'])}\n{c['text']}")))
            for key, value in {"fingerprint": before, "stats": stats,
                               "embedding_signature": signature}.items():
                store.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
        return stats
