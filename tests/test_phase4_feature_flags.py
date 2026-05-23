from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Add extension folder to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extensions" / "memory"))

from embedder import Embedder, semantic_embeddings_enabled
from vector_index import VectorIndex, semantic_vector_index_enabled


def test_embedder_disabled_by_default():
    with pytest.raises(RuntimeError, match="disabled"):
        Embedder(model="test-embed-v1")


def test_vector_index_disabled_by_default():
    with pytest.raises(RuntimeError, match="disabled"):
        VectorIndex()


def test_semantic_components_require_master_switch(monkeypatch):
    monkeypatch.setattr("tau.core.rehydrate._rehydrate_providers", [])
    monkeypatch.setenv("TAU_SEMANTIC_EMBEDDINGS_ENABLED", "1")
    monkeypatch.setenv("TAU_SEMANTIC_VECTOR_INDEX_ENABLED", "1")
    monkeypatch.delenv("TAU_SEMANTIC_RETRIEVAL", raising=False)
    assert semantic_embeddings_enabled() is False
    assert semantic_vector_index_enabled() is False
