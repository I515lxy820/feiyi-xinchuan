import io
import json

from conftest import FakeSearch, FakeWebReader
from fastapi.testclient import TestClient
from PIL import Image

from app.db import Database
from app.main import create_app
from app.question_io import question_hash
from app.schemas import QuestionRecord
from app.solver import Solver


def make_client(settings, solver):
    app = create_app(settings, solver=solver)
    return TestClient(app)


def add_question(settings, text, answer="A", question_type="single", options=None):
    database = Database(settings.database_path)
    database.initialize()
    record = QuestionRecord(
        question_text=text,
        question_type=question_type,
        options=options if options is not None else {"A": "甲", "B": "乙"},
        answer=answer,
        topic="检索基础",
        explanation="参考解析",
    )
    database.insert_questions([(record, question_hash(record.question_text))])
    with database.connect() as connection:
        return connection.execute("SELECT id FROM practice_questions WHERE question_hash = ?", (question_hash(text),)).fetchone()[0]


def test_health_static_and_text_solve(settings, solver):
    with make_client(settings, solver) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["model_configured"] is True
        assert client.get("/").status_code == 200

        response = client.post("/api/solve", data={
            "text": "以下哪项正确？\nA. 甲\nB. 乙",
            "search_mode": "auto",
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload["answer"] == "A"
        assert payload["attempt_id"] > 0
        assert payload["sources"][0]["url"] == "https://example.com/source"
        assert payload["needs_review"] is False


def test_image_solve_and_invalid_image(settings, solver):
    buffer = io.BytesIO()
    Image.new("RGB", (240, 100), "white").save(buffer, format="PNG")
    with make_client(settings, solver) as client:
        response = client.post(
            "/api/solve",
            data={"text": "", "search_mode": "auto"},
            files={"image": ("question.png", buffer.getvalue(), "image/png")},
        )
        assert response.status_code == 200
        assert response.json()["question_text"] == "图片中的题目"
        assert solver.model.calls[0]["image_data_url"].startswith("data:image/jpeg;base64,")

        invalid = client.post(
            "/api/solve",
            data={"text": ""},
            files={"image": ("fake.png", b"not-an-image", "image/png")},
        )
        assert invalid.status_code == 400


def test_practice_feedback_mistakes_and_stats(settings, solver):
    solver.question_bank = None
    add_question(settings, "正确题")
    add_question(settings, "答错题")
    with make_client(settings, solver) as client:
        run = client.post("/api/practice/runs", json={"topic": "", "wrong_only": False}).json()
        run_id = run["run_id"]
        wrong_attempt = None
        for _ in range(2):
            question = client.get("/api/practice/next", params={"run_id": run_id}).json()
            assert question is not None
            assert "answer" not in question
            result = client.post(f"/api/practice/{question['id']}/solve", params={"run_id": run_id}).json()
            if not result["is_correct"]:
                wrong_attempt = result["attempt_id"]
        assert client.get("/api/practice/next", params={"run_id": run_id}).json() is None
        assert wrong_attempt is not None

        stats = client.get("/api/stats").json()
        assert stats["overall"]["attempts"] == 2
        assert stats["overall"]["correct"] == 1
        assert stats["overall"]["accuracy"] == 0.5
        assert stats["recent_runs"][0]["completed_at"] is not None
        assert len(client.get("/api/mistakes").json()) == 1

        feedback = client.post(f"/api/attempts/{wrong_attempt}/feedback", json={
            "correct": True, "corrected_answer": "", "note": "人工确认",
        })
        assert feedback.status_code == 200
        assert client.get("/api/mistakes").json() == []
        assert client.get("/api/stats").json()["overall"]["accuracy"] == 1.0


def test_standard_answer_never_enters_solver(settings, solver):
    secret = "绝密标准答案"
    question_id = add_question(settings, "请填写检索术语", answer=secret, question_type="fill", options={})
    with make_client(settings, solver) as client:
        run_id = client.post("/api/practice/runs", json={"topic": "", "wrong_only": False}).json()["run_id"]
        response = client.post(f"/api/practice/{question_id}/solve", params={"run_id": run_id})
        assert response.status_code == 200
        assert response.json()["correct_answer"] == secret
    for call in solver.model.calls:
        assert secret not in json.dumps(call, ensure_ascii=False, default=str)


def test_webpage_source_is_linked_without_exposing_page_text(settings, model):
    model.needs_search = False
    reader = FakeWebReader()
    solver = Solver(settings, model=model, search=FakeSearch(), web_reader=reader)
    with make_client(settings, solver) as client:
        response = client.post("/api/solve", data={
            "text": "请核对论文 https://example.com/paper",
            "search_mode": "auto",
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload["sources"] == [{
            "title": "测试网页",
            "url": "https://example.com/paper",
            "snippet": "作者：测试作者；摘要：网页摘要",
            "rank": 1,
            "kind": "webpage",
        }]
        assert "page_text" not in response.text

        database = Database(settings.database_path)
        with database.connect() as connection:
            citation = connection.execute(
                "SELECT title, url, snippet, rank FROM citations WHERE attempt_id = ?",
                (payload["attempt_id"],),
            ).fetchone()
        assert dict(citation) == {
            "title": "测试网页",
            "url": "https://example.com/paper",
            "snippet": "作者：测试作者；摘要：网页摘要",
            "rank": 1,
        }

    review_call = next(call for call in model.calls if "citations" in call)
    assert review_call["citations"][0].page_text == "仅供模型复核的网页正文"


def find_question_id(settings, text):
    database = Database(settings.database_path)
    with database.connect() as connection:
        return connection.execute(
            "SELECT id FROM practice_questions WHERE question_hash = ?", (question_hash(text),)
        ).fetchone()[0]


def test_import_questions_marks_pending_and_confirms(settings, solver):
    with make_client(settings, solver) as client:
        response = client.post("/api/import/questions", json={"questions": [
            {"question_text": "已带答案的导入题", "question_type": "single", "options": {"A": "甲", "B": "乙"}, "answer": "B"},
            {"question_text": "需要自动作答的导入题", "question_type": "single", "options": {"A": "甲", "B": "乙"}},
            {"question_text": "已带答案的导入题", "question_type": "single", "options": {"A": "甲", "B": "乙"}, "answer": "A"},
        ]})
        assert response.status_code == 200
        summary = response.json()
        assert summary["total"] == 3
        assert summary["confirmed"] == 1
        assert summary["pending"] == 1
        assert summary["skipped"] == 1
        assert client.get("/api/stats").json()["pending_questions"] == 1

        question_id = find_question_id(settings, "需要自动作答的导入题")
        assert client.post(f"/api/questions/{question_id}/answer", json={"answer": "C"}).status_code == 400
        confirmed = client.post(f"/api/questions/{question_id}/answer", json={"answer": "B"})
        assert confirmed.json() == {"id": question_id, "answer": "B", "answer_status": "confirmed"}
        assert client.get("/api/stats").json()["pending_questions"] == 0


def test_import_without_answer_and_without_autosolve_fails(settings, solver):
    with make_client(settings, solver) as client:
        summary = client.post("/api/import/questions", json={
            "questions": [{"question_text": "没有答案的题", "question_type": "single", "options": {"A": "甲"}}],
            "auto_solve": False,
        }).json()
        assert summary["failed"] == 1
        assert "未提供答案" in summary["items"][0]["message"]


def test_import_rejects_too_many_questions(settings, solver):
    with make_client(settings, solver) as client:
        questions = [{"question_text": f"批量题 {index}", "question_type": "boolean", "answer": "正确"} for index in range(51)]
        response = client.post("/api/import/questions", json={"questions": questions})
        assert response.status_code == 400
