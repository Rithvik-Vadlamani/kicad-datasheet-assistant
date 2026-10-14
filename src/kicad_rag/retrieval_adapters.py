"""Optional, explicitly selected local reranking. Never silently falls back or loads remote code."""

from functools import lru_cache


@lru_cache(maxsize=2)
def model(name):
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as error:
        raise ValueError("Install kicad-rag[rerank] to enable a local cross-encoder") from error
    # Only preinstalled local model folders. Downloading/selecting a model is an explicit setup action.
    from pathlib import Path

    if not Path(name).is_dir():
        raise ValueError("reranker_model must point to a locally downloaded model directory")
    return CrossEncoder(name, trust_remote_code=False, local_files_only=True)


def rerank(name, question, keys, candidates):
    import numpy as np

    values = np.asarray(model(name).predict([(question, candidates[k]["text"]) for k in keys]))
    if values.shape != (len(keys),) or not np.isfinite(values).all():
        raise ValueError("Reranker must produce one finite score per candidate")
    return [keys[i] for i in np.argsort(-values, kind="stable")]


def local_model_identity(name):
    from pathlib import Path


    folder = Path(name).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Local model must be a preinstalled directory")
    files = sorted(p for p in folder.rglob("*") if p.is_file() and
                   p.suffix in {".json", ".txt", ".safetensors", ".model"})
    if not any(p.suffix == ".safetensors" for p in files):
        raise ValueError("Local embedding model requires safetensors weights")
    stamp = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    return _model_hash(stamp)


@lru_cache(maxsize=4)
def _model_hash(stamp):
    from pathlib import Path

    from .config import canonical_hash, digest
    return canonical_hash({name: digest(Path(name)) for name, _, _ in stamp})


@lru_cache(maxsize=2)
def embedding_model(name, identity):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise ValueError("Install kicad-rag[local-models] for local embeddings") from error
    return SentenceTransformer(name, trust_remote_code=False, local_files_only=True, device="cpu")


def embed_local(name, texts):
    """Token-window mean pooling avoids silently discarding the end of long datasheet chunks."""
    import numpy as np
    encoder = embedding_model(name, local_model_identity(name))
    limit = min(int(encoder.max_seq_length), 1024) - 16
    if limit < 32:
        raise ValueError("Unsupported local model context size")
    windows, offsets = [], []
    for text in texts:
        token_ids = encoder.tokenizer.encode(text, add_special_tokens=False)
        if len(token_ids) > 16384:
            raise ValueError("Embedding input exceeds bounded local token budget")
        start = len(windows)
        for offset in range(0, max(1, len(token_ids)), limit):
            windows.append(encoder.tokenizer.decode(token_ids[offset:offset+limit], skip_special_tokens=True))
        offsets.append((start, len(windows)))
    vectors = encoder.encode(windows, batch_size=24, show_progress_bar=False,
                             convert_to_numpy=True, normalize_embeddings=True, prompt="")
    return np.asarray([vectors[a:b].mean(axis=0) for a, b in offsets], dtype=np.float32)
