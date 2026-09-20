#!/usr/bin/env python3
"""Embed every distinct signature retrieval_text that isn't cached yet.

    python scripts/embed_signatures.py
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.db import get_conn
from winnow.vector import SentenceTransformerEmbedder, embed_missing_signatures

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    t0 = time.time()
    embedder = SentenceTransformerEmbedder()
    with get_conn() as conn:
        n = embed_missing_signatures(conn, embedder)
    dt = time.time() - t0
    print(f"embedded {n} distinct texts with {embedder.name} ({embedder.dims}d) in {dt:.1f}s"
          + (f" -> {n / dt:.1f} texts/s" if n else ""))
