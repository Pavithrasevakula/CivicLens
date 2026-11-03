import unittest

from app.multimodal import score_duplicate
from app.priority import calculate_priority, raise_priority_for_repeat_report
from experiments.evaluate import evaluate
from experiments.synthetic_dataset import generate_dataset


class ScoringTests(unittest.TestCase):
    def test_multimodal_score_contains_all_components(self) -> None:
        evidence = score_duplicate(
            description="large pothole near bus stop",
            candidate_description="large pothole beside bus stop",
            image=b"same-image",
            candidate_image=b"same-image",
            latitude=17.385,
            longitude=78.486,
            candidate_latitude=17.3851,
            candidate_longitude=78.4861,
        )
        self.assertEqual(evidence.image_similarity, 1.0)
        self.assertGreater(evidence.text_similarity, 0)
        self.assertGreater(evidence.location_similarity, 0)
        self.assertGreater(evidence.combined_score, 0)

    def test_priority_formula_and_level_are_transparent(self) -> None:
        result = calculate_priority(
            severity=90,
            population_impact=70,
            recurrence=50,
            location_sensitivity=80,
        )
        self.assertEqual(result.score, 75.5)
        self.assertEqual(result.level, "high")

    def test_repeat_report_raises_priority_one_level(self) -> None:
        self.assertEqual(raise_priority_for_repeat_report("low", 25), ("medium", 35))
        self.assertEqual(raise_priority_for_repeat_report("medium", 60), ("high", 70))

    def test_repeat_report_priority_is_capped_at_critical(self) -> None:
        self.assertEqual(raise_priority_for_repeat_report("critical", 95), ("critical", 100))
        self.assertEqual(raise_priority_for_repeat_report("high", 98), ("critical", 100))

    def test_repeat_report_rejects_unknown_priority(self) -> None:
        with self.assertRaises(ValueError):
            raise_priority_for_repeat_report("unknown", 25)

    def test_evaluation_metrics(self) -> None:
        result = evaluate([True, False, True, False], [True, True, False, False])
        self.assertEqual((result.true_positive, result.false_positive, result.false_negative, result.true_negative), (1, 1, 1, 1))
        self.assertEqual(result.f1, 0.5)

    def test_synthetic_dataset_is_reproducible(self) -> None:
        self.assertEqual(generate_dataset(5), generate_dataset(5))
        self.assertEqual(len(generate_dataset(5)), 5)


if __name__ == "__main__":
    unittest.main()
