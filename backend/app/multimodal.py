"""Deterministic multimodal duplicate scoring with optional model adapters.

The default implementation is dependency-light and reproducible for local
evaluation. Production deployments can provide CLIP and sentence-transformer
adapters through the same protocol without changing the scoring contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol, Sequence


class EmbeddingModel(Protocol):
    model_name: str

    def encode(self, value: str | bytes) -> Sequence[float]:
        ...


@dataclass(frozen=True)
class SimilarityEvidence:
    image_similarity: float
    text_similarity: float
    location_similarity: float
    combined_score: float
    distance_meters: float
    image_model: str
    text_model: str
    weights: dict[str, float]


def _unit_vector(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm else values


def _hashed_vector(value: str | bytes, dimensions: int = 128) -> list[float]:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    vector = [0.0] * dimensions
    for index in range(0, len(raw), 4):
        digest = hashlib.blake2b(raw[index:index + 4], digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        vector[bucket] += 1.0 if digest[4] % 2 else -1.0
    return _unit_vector(vector)


def _text_tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.casefold()) if len(token) > 2}


def text_similarity(left: str, right: str) -> float:
    left_tokens = _text_tokens(left)
    right_tokens = _text_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def image_similarity(left: bytes | None, right: bytes | None) -> float:
    if not left or not right:
        return 0.0
    left_vector = _hashed_vector(left)
    right_vector = _hashed_vector(right)
    return max(0.0, min(1.0, (sum(a * b for a, b in zip(left_vector, right_vector)) + 1.0) / 2.0))


def haversine_distance_meters(
    left_latitude: float,
    left_longitude: float,
    right_latitude: float,
    right_longitude: float,
) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(left_latitude), math.radians(right_latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(right_longitude - left_longitude)
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))


def location_similarity(distance_meters: float, radius_meters: float = 500.0) -> float:
    if distance_meters >= radius_meters:
        return 0.0
    return 1.0 - distance_meters / radius_meters


def score_duplicate(
    *,
    description: str,
    candidate_description: str,
    image: bytes | None,
    candidate_image: bytes | None,
    latitude: float,
    longitude: float,
    candidate_latitude: float,
    candidate_longitude: float,
    weights: dict[str, float] | None = None,
    radius_meters: float = 500.0,
) -> SimilarityEvidence:
    selected_weights = weights or {"image": 0.4, "text": 0.35, "location": 0.25}
    if abs(sum(selected_weights.values()) - 1.0) > 0.001:
        raise ValueError("Similarity weights must sum to 1.0")
    distance = haversine_distance_meters(latitude, longitude, candidate_latitude, candidate_longitude)
    image_score = image_similarity(image, candidate_image)
    text_score = text_similarity(description, candidate_description)
    location_score = location_similarity(distance, radius_meters)
    combined = (
        selected_weights["image"] * image_score
        + selected_weights["text"] * text_score
        + selected_weights["location"] * location_score
    )
    return SimilarityEvidence(
        image_similarity=round(image_score, 5),
        text_similarity=round(text_score, 5),
        location_similarity=round(location_score, 5),
        combined_score=round(combined, 5),
        distance_meters=round(distance, 3),
        image_model="hashed-image-baseline-v1",
        text_model="token-overlap-baseline-v1",
        weights=selected_weights,
    )
