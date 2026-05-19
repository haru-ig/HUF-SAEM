"""ChromaDB-backed vector store of structurally sound seed programs."""

from __future__ import annotations

import hashlib
import random
from typing import Dict, List, Optional


class SeedDatabase:
    def __init__(
        self,
        db_path: str,
        collection_name: str = "seeds",
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self.db_path = db_path
        self.collection_name = collection_name
        self.embedding_model_name = embedding_model
        self._client = None
        self._collection = None
        self._embedder = None
        self._build_client()

    def _build_client(self) -> None:
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer

            self._client = chromadb.PersistentClient(path=self.db_path)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self._embedder = SentenceTransformer(self.embedding_model_name)
        except ImportError as e:
            # Degrade gracefully if optional deps are missing
            print(f"[Phase3] SeedDatabase unavailable: {e}. Phase 3 disabled.")
            self._client = None

    def _embed(self, text: str) -> List[float]:
        return self._embedder.encode(text, convert_to_numpy=True).tolist()

    def add_seed(self, code: str, metadata: Dict) -> None:
        if self._collection is None:
            return
        doc_id = hashlib.md5(code.encode()).hexdigest()  # noqa: S324
        try:
            self._collection.upsert(
                ids=[doc_id],
                embeddings=[self._embed(code)],
                documents=[code],
                metadatas=[{k: str(v) for k, v in metadata.items()}],
            )
        except Exception:
            pass

    def retrieve_similar(self, query: str, n: int = 3) -> List[str]:
        if self._collection is None or self._collection.count() == 0:
            return []
        n = min(n, self._collection.count())
        try:
            results = self._collection.query(
                query_embeddings=[self._embed(query)],
                n_results=n,
                include=["documents"],
            )
            return results["documents"][0] if results["documents"] else []
        except Exception:
            return []

    def retrieve_random(self) -> Optional[str]:
        if self._collection is None or self._collection.count() == 0:
            return None
        try:
            total = self._collection.count()
            offset = random.randint(0, total - 1)
            results = self._collection.get(
                limit=1, offset=offset, include=["documents"]
            )
            docs = results.get("documents", [])
            return docs[0] if docs else None
        except Exception:
            return None

    def size(self) -> int:
        if self._collection is None:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0
