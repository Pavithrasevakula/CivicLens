"""Transparent priority scoring used by experiments and future intake workers."""

from __future__ import annotations

from dataclasses import dataclass


PRIORITY_LEVELS = ("low", "medium", "high", "critical")


@dataclass(frozen=True)
class PriorityEvidence:
    severity: float
    population_impact: float
    recurrence: float
    location_sensitivity: float
    score: float
    level: str


def calculate_priority(
    *,
    severity: float,
    population_impact: float,
    recurrence: float,
    location_sensitivity: float,
    weights: dict[str, float] | None = None,
) -> PriorityEvidence:
    factors = {
        "severity": severity,
        "population_impact": population_impact,
        "recurrence": recurrence,
        "location_sensitivity": location_sensitivity,
    }
    if any(value < 0 or value > 100 for value in factors.values()):
        raise ValueError("Priority factors must be between 0 and 100")
    selected_weights = weights or {
        "severity": 0.4,
        "population_impact": 0.25,
        "recurrence": 0.2,
        "location_sensitivity": 0.15,
    }
    if set(selected_weights) != set(factors) or abs(sum(selected_weights.values()) - 1.0) > 0.001:
        raise ValueError("Priority weights must contain all factors and sum to 1.0")
    score = sum(factors[name] * selected_weights[name] for name in factors)
    level = "critical" if score >= 80 else "high" if score >= 60 else "medium" if score >= 35 else "low"
    return PriorityEvidence(**factors, score=round(score, 3), level=level)


def raise_priority_for_repeat_report(priority: str, score: float) -> tuple[str, float]:
    """Increase an issue's priority one level for each newly linked repeat report."""
    try:
        current_level = PRIORITY_LEVELS.index(priority)
    except ValueError as error:
        raise ValueError(f"Unknown priority level: {priority}") from error

    next_level = min(current_level + 1, len(PRIORITY_LEVELS) - 1)
    return PRIORITY_LEVELS[next_level], min(round(score + 10, 3), 100.0)
