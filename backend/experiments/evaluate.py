from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationMetrics:
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float
    recall: float
    f1: float


def evaluate(predictions: list[bool], labels: list[bool]) -> EvaluationMetrics:
    if len(predictions) != len(labels) or not labels:
        raise ValueError("Predictions and labels must be non-empty and have equal length")
    tp = sum(predicted and actual for predicted, actual in zip(predictions, labels))
    fp = sum(predicted and not actual for predicted, actual in zip(predictions, labels))
    fn = sum(not predicted and actual for predicted, actual in zip(predictions, labels))
    tn = sum(not predicted and not actual for predicted, actual in zip(predictions, labels))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return EvaluationMetrics(tp, fp, fn, tn, round(precision, 5), round(recall, 5), round(f1, 5))
