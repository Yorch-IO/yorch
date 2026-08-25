"""Outbound model access. One module per provider, one client per process."""

from .adapter import VertexAdapter
from .gemini import Embedding, Generation, Provider, ProviderError, Usage

__all__ = [
    "Embedding",
    "Generation",
    "Provider",
    "ProviderError",
    "Usage",
    "VertexAdapter",
]
