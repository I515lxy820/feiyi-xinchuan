import re

import pytest

from app.config import Settings
from app.providers import SearchOutcome
from app.schemas import Citation, DraftAnswer
from app.solver import Solver


class FakeModel:
    def __init__(self):
        self.calls = []
        self.review_answer = None
        self.needs_search = True

    async def draft(self, question_text, image_data_url, options, question_type):
        self.calls.append({
            "question_text": question_text,
            "image_data_url": image_data_url,
            "options": options,
            "question_type": question_type,
        })
        parsed_options = options or {
            match.group(1): match.group(2).strip()
            for match in re.finditer(r"(?m)^\s*([A-Z])[.、．]\s*(.+)$", question_text)
        }
        detected_type = question_type
        if detected_type == "unknown":
            detected_type = "single" if parsed_options else "fill"
        if detected_type == "single":
            labels = list(parsed_options) or ["A", "B"]
            answer = labels[-1] if "答错" in question_text else labels[0]
        elif detected_type == "multiple":
            answer = ",".join(list(parsed_options)[:2])
        elif detected_type == "boolean":
            answer = "正确"
        elif detected_type == "essay":
            answer = "简答候选答案"
        else:
            answer = "测试答案"
        return DraftAnswer(
            question_text=question_text or "图片中的题目",
            question_type=detected_type,
            options=parsed_options,
            answer=answer,
            explanation="测试解析",
            confidence=0.91,
            needs_search=self.needs_search,
            search_queries=["信息素养测试"],
        )

    async def review(self, question_text, draft, citations):
        self.calls.append({"review_question": question_text, "citations": citations})
        result = draft.model_copy(deep=True)
        if self.review_answer is not None:
            result.answer = self.review_answer
        return result

    async def close(self):
        return None


class FakeSearch:
    def __init__(self, results=None):
        self.calls = []
        self.results = results

    async def search(self, queries):
        self.calls.append(queries)
        results = self.results
        if results is None:
            results = [Citation(
                title="测试来源",
                url="https://example.com/source",
                snippet="用于测试的检索摘要",
                rank=1,
            )]
        return SearchOutcome(results=results)


class FakeWebReader:
    def __init__(self, results=None, error=None):
        self.calls = []
        self.results = results
        self.error = error

    async def read(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        results = self.results
        if results is None:
            results = [Citation(
                title="测试网页",
                url=url,
                snippet="作者：测试作者；摘要：网页摘要",
                kind="webpage",
                page_text="仅供模型复核的网页正文",
            )]
        return SearchOutcome(results=results)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        model_provider="openai",
        model_api_key="test-key",
        model_name="fake-vision",
        search_provider="bocha",
        search_api_key="test-search",
        app_db_path=str(tmp_path / "test.db"),
        confidence_threshold=0.7,
        web_reader_enabled=False,
    )


@pytest.fixture
def model():
    return FakeModel()


@pytest.fixture
def solver(settings, model):
    return Solver(settings, model=model, search=FakeSearch())
