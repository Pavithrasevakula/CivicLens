from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from multiprocessing import cpu_count
from pathlib import Path

from app.multimodal import score_duplicate


@dataclass(frozen=True)
class BenchmarkResult:
    workers: int
    dataset_size: int
    elapsed_seconds: float
    throughput_per_second: float
    speedup: float
    efficiency: float


def _score(index: int) -> float:
    return score_duplicate(
        description=f"pothole near bus stop {index % 17}",
        candidate_description=f"pothole near bus stop {(index + 1) % 17}",
        image=f"image-{index}".encode(),
        candidate_image=f"candidate-{index}".encode(),
        latitude=17.385 + (index % 10) * 0.0001,
        longitude=78.486 + (index % 10) * 0.0001,
        candidate_latitude=17.385,
        candidate_longitude=78.486,
    ).combined_score


def run_benchmark(dataset_size: int, workers: list[int]) -> list[BenchmarkResult]:
    baseline = 0.0
    results: list[BenchmarkResult] = []
    for worker_count in workers:
        started = time.perf_counter()
        if worker_count == 1:
            [_score(index) for index in range(dataset_size)]
        else:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                list(executor.map(_score, range(dataset_size), chunksize=max(1, dataset_size // (worker_count * 8))))
        elapsed = time.perf_counter() - started
        if worker_count == 1:
            baseline = elapsed
        speedup = baseline / elapsed if baseline else 1.0
        results.append(BenchmarkResult(
            workers=worker_count,
            dataset_size=dataset_size,
            elapsed_seconds=round(elapsed, 6),
            throughput_per_second=round(dataset_size / elapsed, 3),
            speedup=round(speedup, 4),
            efficiency=round(speedup / worker_count, 4),
        ))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run CivicLens sequential/parallel scoring benchmark")
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmark.json"))
    args = parser.parse_args()
    worker_options = [worker for worker in (1, 2, 4, 8) if worker <= max(1, cpu_count())]
    output = [asdict(result) for result in run_benchmark(args.size, worker_options)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))
