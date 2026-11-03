from __future__ import annotations

import argparse
import csv
import random
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyntheticComplaint:
    complaint_id: str
    category: str
    description: str
    image_reference: str
    latitude: float
    longitude: float
    timestamp: str
    severity: str
    duplicate_group_id: str
    priority: str
    status: str
    citizen_count: int


def generate_dataset(size: int, seed: int = 42) -> list[SyntheticComplaint]:
    if size < 2:
        raise ValueError("Dataset size must be at least 2")
    randomizer = random.Random(seed)
    templates = [
        ("road_damage", "Large pothole near the bus stop", "high"),
        ("garbage", "Overflowing garbage collection point", "medium"),
        ("streetlight", "Streetlight not working near the junction", "medium"),
        ("water_leakage", "Water pipe leak flooding the roadside", "high"),
    ]
    rows: list[SyntheticComplaint] = []
    for index in range(size):
        category, description, priority = templates[index % len(templates)]
        group = f"DUP{index // 4 + 1:04d}"
        variation = randomizer.choice(["reported again", "visible from another angle", "near the same location"])
        rows.append(SyntheticComplaint(
            complaint_id=f"CMP{index + 1:06d}",
            category=category,
            description=f"{description}, {variation}",
            image_reference=f"image_{index + 1:06d}.jpg",
            latitude=17.3850 + randomizer.uniform(-0.02, 0.02),
            longitude=78.4867 + randomizer.uniform(-0.02, 0.02),
            timestamp=f"2026-01-{index % 28 + 1:02d}T08:00:00Z",
            severity=priority,
            duplicate_group_id=group,
            priority=priority,
            status="under_verification",
            citizen_count=randomizer.randint(1, 25),
        ))
    return rows


def write_csv(rows: list[SyntheticComplaint], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0])))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate labeled synthetic CivicLens complaints")
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path("artifacts/synthetic_complaints.csv"))
    args = parser.parse_args()
    write_csv(generate_dataset(args.size), args.output)
    print(f"Wrote {args.size} synthetic complaints to {args.output}")
