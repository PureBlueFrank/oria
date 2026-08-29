"""Deterministic fixture and pinned sentence-transformers embedders."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
import re
from collections.abc import Callable, Sequence
from typing import Protocol, cast


class _SentenceTransformer(Protocol):
    def get_embedding_dimension(self) -> int | None: ...

    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> object: ...


ModelFactory = Callable[..., _SentenceTransformer]


class FixtureEmbedder:
    """Offline deterministic embedder used only by fixture/demo profiles."""

    def __init__(self, *, dim: int = 128) -> None:
        if dim <= 0:
            raise ValueError("fixture embedding dimension must be positive")
        self.dim = dim

    async def embed(self, texts: list[str], ctx: object) -> list[list[float]]:
        del ctx
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        normalized = text.casefold()
        words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", normalized)
        chinese = "".join(token for token in words if len(token) == 1 and token > "\x7f")
        features = words + [chinese[index : index + 2] for index in range(len(chinese) - 1)]
        if not features:
            features = [normalized]
        values = [0.0] * self.dim
        for feature in features:
            digest = hashlib.sha256(feature.encode()).digest()
            index = int.from_bytes(digest[:8], "big") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            values[index] += sign
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            values[0] = 1.0
            return values
        return [value / norm for value in values]


class BGEEmbedder:
    """Pinned local BGE embedder with remote model code disabled."""

    def __init__(
        self,
        *,
        model: str,
        revision: str | None,
        trust_remote_code: bool,
        model_factory: ModelFactory | None = None,
    ) -> None:
        if not revision:
            raise ValueError("BGE model revision must be pinned")
        if trust_remote_code:
            raise ValueError("BGE remote model code is forbidden")
        factory = model_factory if model_factory is not None else self._load_factory()
        self._model = factory(model, revision=revision, trust_remote_code=False)
        dimension = self._model.get_embedding_dimension()
        if dimension is None or dimension <= 0:
            raise ValueError("BGE model returned an invalid embedding dimension")
        self.dim = dimension

    @staticmethod
    def _load_factory() -> ModelFactory:
        try:
            module = importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required for the selected embedding profile"
            ) from exc
        return cast(ModelFactory, module.SentenceTransformer)

    async def embed(self, texts: list[str], ctx: object) -> list[list[float]]:
        del ctx
        encoded = await asyncio.to_thread(
            self._model.encode,
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        raw = encoded.tolist() if hasattr(encoded, "tolist") else encoded
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValueError("BGE model returned an invalid embedding batch")
        vectors: list[list[float]] = []
        for row in raw:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
                raise ValueError("BGE model returned an invalid embedding vector")
            vector = [float(value) for value in row]
            if len(vector) != self.dim or any(not math.isfinite(value) for value in vector):
                raise ValueError("BGE model returned an invalid embedding vector")
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
                raise ValueError("BGE model returned a non-normalized embedding vector")
            vectors.append(vector)
        if len(vectors) != len(texts):
            raise ValueError("BGE model returned an invalid embedding batch size")
        return vectors
