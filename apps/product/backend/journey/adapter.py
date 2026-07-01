"""Load + apply the linear retrieval adapter (see train_vec_adapter.py).

The adapter is a (d x d) matrix W learned to map a query embedding toward the
commodity-code self-text embedding space. Applied to the QUERY vector only:

    v_adapted = normalise(v @ W)

Stored code embeddings are NOT transformed, so retrieval cosine stays in a
single consistent space. If the artifact is missing, apply_adapter is a no-op
(returns the input unchanged) - retrieval degrades to the un-adapted vector leg.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

_W_PATH = Path(__file__).parent / "data" / "vec_adapter.npy"


@lru_cache(maxsize=1)
def _load_W() -> Optional[np.ndarray]:
    if not _W_PATH.exists():
        return None
    try:
        return np.load(_W_PATH).astype(np.float32)
    except Exception as e:  # pragma: no cover
        print(f"[adapter] failed to load {_W_PATH}: {e}")
        return None


def is_available() -> bool:
    return _load_W() is not None


def apply_adapter(emb: Optional[Sequence[float]]) -> Optional[list[float]]:
    """Map a single query embedding through W and L2-normalise.

    No-op (returns the input as a list) if W is unavailable or emb is falsy.
    """
    W = _load_W()
    if W is None or not emb:
        return list(emb) if emb else emb
    v = np.asarray(emb, dtype=np.float32)
    out = v @ W
    n = float(np.linalg.norm(out))
    if n > 0:
        out = out / n
    return out.tolist()
