from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Document(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    path: str
    title: str
    parts: list[str] = Field(default_factory=list)
    revision: str = "unspecified"
    kind: Literal["datasheet", "application_note", "errata", "design_note"] = "datasheet"
    source_url: str | None = None


class Settings(StrictModel):
    ocr_mode: Literal["off", "auto", "always"] = "off"
    ocr_language: str = Field(default="eng", pattern=r"^[a-zA-Z_+]+$")
    ocr_dpi: int = Field(default=300, ge=150, le=400)
    shared_extraction_cache: str = ""
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""
    reranker_model: str = ""
    reranker_candidates: int = Field(default=30, ge=5, le=100)
    restore_parent_context: bool = True
    embedding_provider: Literal["none", "ollama", "openai", "local"] = "none"
    embedding_model: str = ""
    chat_provider: Literal["none", "ollama", "openai"] = "none"
    chat_model: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    chunk_chars: int = Field(default=2200, ge=500, le=6000)
    overlap_chars: int = Field(default=250, ge=0, le=1000)
    top_k: int = Field(default=12, ge=1, le=40)
    context_chars: int = Field(default=38000, ge=6000, le=120000)
    max_output_tokens: int = Field(default=4000, ge=500, le=16000)

    @model_validator(mode="after")
    def valid(self):
        if self.shared_extraction_cache and not Path(self.shared_extraction_cache).expanduser().is_absolute():
            raise ValueError('shared_extraction_cache must be an explicitly chosen absolute local directory')
        if self.overlap_chars >= self.chunk_chars:
            raise ValueError("overlap_chars must be smaller than chunk_chars")
        for kind in ("embedding", "chat"):
            if getattr(self, kind + "_provider") != "none" and not getattr(self, kind + "_model"):
                raise ValueError(f"Set {kind}_model when enabling {kind}_provider")
        return self


class Project(StrictModel):
    schema_version: Literal[1] = 1
    name: str
    documents: list[Document] = Field(default_factory=list)
    netlist: str | None = None
    schematic: str | None = None
    schematic_hashes: dict[str, str] = Field(default_factory=dict)
    component_parts: dict[str, str] = Field(default_factory=dict)
    design: str = "design.json"
    settings: Settings = Field(default_factory=Settings)

    @model_validator(mode="after")
    def unique_documents(self):
        ids = [d.id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("Document IDs must be unique")
        return self


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write("\n")
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def load_project(root: Path) -> Project:
    return Project.model_validate_json((root / "project.json").read_text())


def save_project(root: Path, project: Project):
    atomic_json(root / "project.json", project.model_dump())


def local_path(root: Path, path: str) -> Path:
    """Documents/netlists must be inside the selected project; schematics may be external."""
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Project file must stay inside project folder: {path}")
    return result
