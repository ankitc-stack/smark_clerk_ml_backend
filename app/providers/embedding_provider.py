from __future__ import annotations
from app.config import settings

def _stub_embed(text: str) -> list[float]:
    import numpy as np
    rng = np.random.default_rng(abs(hash(text)) % (2**32))
    vec = rng.normal(size=(settings.EMBEDDING_DIM,)).astype("float32")
    vec = vec / (np.linalg.norm(vec) + 1e-12)
    return vec.tolist()

_fast_model = None

def embed(text: str) -> list[float]:
    """Local embedding provider.

    Providers:
    - fastembed: local ONNX embeddings (recommended)
    - stub: deterministic pseudo-embedding (dev only)
    """
    if settings.EMBED_PROVIDER.lower() == "stub":
        return _stub_embed(text)

    if settings.EMBED_PROVIDER.lower() == "fastembed":
        global _fast_model
        if _fast_model is None:
            try:
                from fastembed import TextEmbedding
                _fast_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
            except Exception:
                return _stub_embed(text)
        try:
            vec = next(_fast_model.embed([text]))
            return vec.tolist()
        except Exception:
            return _stub_embed(text)

    return _stub_embed(text)
