"""Local embedding model (spec 6.8). CUDA when available, CPU fallback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from core.config import AppConfig

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)


def pick_device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    log.warning("CUDA not available, using CPU for local models")
    return "cpu"


def load_embedder(cfg: AppConfig) -> SentenceTransformer:
    from sentence_transformers import SentenceTransformer

    device = pick_device()
    model = SentenceTransformer(cfg.models.embedding, device=device)
    if device == "cuda":
        model.half()
    log.info("embedding model loaded", extra={"model": cfg.models.embedding, "device": device})
    return model


def embed_passages(model: SentenceTransformer, texts: list[str], batch_size: int = 256) -> npt.NDArray[np.float32]:
    """Passages get no prefix."""
    out = model.encode(texts, batch_size=batch_size, normalize_embeddings=True, convert_to_numpy=True)
    return np.asarray(out, dtype=np.float32)


def embed_query(model: SentenceTransformer, cfg: AppConfig, query: str) -> npt.NDArray[np.float32]:
    """Queries get `embedding_query_prefix`."""
    out = model.encode([cfg.models.embedding_query_prefix + query], normalize_embeddings=True, convert_to_numpy=True)
    return np.asarray(out[0], dtype=np.float32)


def abstract_text(title: str, abstract: str) -> str:
    """What the paper-level vector represents: title + abstract (same text the reranker sees)."""
    return f"{title}. {abstract}"
