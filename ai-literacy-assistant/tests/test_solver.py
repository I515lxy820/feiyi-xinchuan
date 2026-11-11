import pytest

from app.answers import answers_match, normalize_answer
from app.evaluation import evaluate_records, summarize
from app.providers import ProviderError
from app.schemas import QuestionRecord
from app.solver import Solver


class FailingSearch:
    async def search(self, queries):
        raise ProviderError("模拟搜索超时")


def test_answer_normalization():
    assert normalize_answer("答案：C, A", "multiple") == "A,C"
    assert answers_match("C,A", "A,C", "multiple") is True
    assert normalize_answer("对", "boolean") == "正确"
    assert answers_match("", "A", "single") is False
    assert answers_match("任意", "参考", "essay") is None


@pytest.mark.asyncio
async def test_search_failure_returns_candidate_with_review(settings, model):
    solver = Solver(settings, model=model, search=FailingSearch())
    response = await solver.solve("测试题", options={"A": "甲"}, question_type="single")
    assert response.answer == "A"
    assert response.needs_review is True
    assert any("联网核对失败" in warning for warning in response.warnings)


@pytest.mark.asyncio
async def test_changed_review_answer_requires_review(settings, model, solver):
    model.review_answer = "B"
    response = await solver.solve("测试题", options={"A": "甲", "B": "乙"}, question_type="single")
    assert response.answer == "B"
    assert response.needs_review is True
    assert any("改变了初答" in warning for warning in response.warnings)


def test_evaluation_summary_counts_blank_and_manual_separately():
    results = [
        {"auto_graded": True, "is_correct": True, "answer": "A", "question_type": "single", "input_type": "text", "latency_seconds": 1.0},
        {"auto_graded": True, "is_correct": False, "answer": "B", "question_type": "single", "input_type": "text", "latency_seconds": 3.0},
        {"auto_graded": True, "is_correct": False, "answer": "", "question_type": "fill", "input_type": "image", "latency_seconds": 2.0},
        {"auto_graded": False, "is_correct": None, "answer": "简答", "question_type": "essay", "input_type": "text", "latency_seconds": 4.0},
    ]
    metrics = summarize(results)
    assert metrics["total"] == 3
    assert metrics["correct"] == 1
    assert metrics["accuracy"] == pytest.approx(1 / 3)
    assert metrics["coverage"] == pytest.approx(2 / 3)
    assert metrics["manual_review_count"] == 1
    assert metrics["meets_80_percent"] is False
    assert metrics["status"] == "insufficient_sample"


@pytest.mark.asyncio
async def test_evaluation_counts_broken_image_as_error(solver):
    record = QuestionRecord(
        question_text="图片题",
        question_type="single",
        options={"A": "甲", "B": "乙"},
        answer="A",
        image_path="/path/that/does/not/exist.png",
    )
    results = await evaluate_records([record], solver)
    assert results[0]["is_correct"] is False
    assert results[0]["answer"] == ""
    assert results[0]["error"]
    assert results[0]["input_type"] == "mixed"
