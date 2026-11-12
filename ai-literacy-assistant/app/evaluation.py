import asyncio
import math
import time
from pathlib import Path
from typing import Any

from .answers import answers_match
from .imaging import prepare_image
from .question_io import question_hash
from .schemas import QuestionRecord
from .solver import Solver


def wilson_interval(correct: int, total: int, z: float = 1.96) -> list[float | None]:
    if not total:
        return [None, None]
    proportion = correct / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0, center - margin), 4), round(min(1, center + margin), 4)]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))], 3)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [item for item in results if item["auto_graded"]]
    total = len(graded)
    correct = sum(item["is_correct"] is True for item in graded)
    answered = [item for item in graded if item["answer"]]
    answered_correct = sum(item["is_correct"] is True for item in answered)
    accuracy = correct / total if total else None

    def grouped(key: str) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for item in graded:
            group = output.setdefault(item[key], {"total": 0, "correct": 0, "accuracy": None})
            group["total"] += 1
            group["correct"] += item["is_correct"] is True
        for group in output.values():
            group["accuracy"] = group["correct"] / group["total"]
        return output

    latencies = [item["latency_seconds"] for item in graded]
    return {
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "accuracy_wilson_95": wilson_interval(correct, total),
        "coverage": len(answered) / total if total else None,
        "answered_accuracy": answered_correct / len(answered) if answered else None,
        "manual_review_count": len(results) - total,
        "by_type": grouped("question_type"),
        "by_input_type": grouped("input_type"),
        "latency_seconds": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "sample_sufficient": total >= 200,
        "meets_80_percent": bool(total >= 200 and accuracy is not None and accuracy >= 0.8),
        "status": (
            "insufficient_sample" if total < 200 else "passed" if accuracy >= 0.8 else "below_target"
        ),
    }


async def evaluate_records(
    records: list[QuestionRecord],
    solver: Solver,
    concurrency: int = 2,
    bank_exclude_self: bool = False,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(record: QuestionRecord) -> dict[str, Any]:
        async with semaphore:
            started = time.perf_counter()
            input_type = "mixed" if record.question_text and record.image_path else "image" if record.image_path else "text"
            base = {
                "question_text": record.question_text,
                "question_type": record.question_type,
                "input_type": input_type,
                "expected_answer": record.answer,
                "auto_graded": record.question_type in {"single", "multiple", "boolean", "fill"},
            }
            try:
                image_data_url = prepare_image(Path(record.image_path).read_bytes()) if record.image_path else None
                response = await solver.solve(
                    record.question_text,
                    image_data_url,
                    options=record.options,
                    question_type=record.question_type,
                    search_mode="auto",
                    bank_exclude_hash=question_hash(record.question_text) if bank_exclude_self else "",
                )
                is_correct = answers_match(response.answer, record.answer, record.question_type)
                return {
                    **base,
                    "answer": response.answer,
                    "is_correct": is_correct,
                    "confidence": response.confidence,
                    "needs_review": response.needs_review,
                    "warnings": response.warnings,
                    "sources": [source.model_dump() for source in response.sources],
                    "error": None,
                    "latency_seconds": round(time.perf_counter() - started, 3),
                }
            except Exception as exc:
                return {
                    **base,
                    "answer": "",
                    "is_correct": False if base["auto_graded"] else None,
                    "confidence": 0,
                    "needs_review": True,
                    "warnings": [],
                    "sources": [],
                    "error": str(exc)[:300],
                    "latency_seconds": round(time.perf_counter() - started, 3),
                }

    return await asyncio.gather(*(one(record) for record in records))
