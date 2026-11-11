import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import Citation, QuestionRecord, SolveResponse


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS practice_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_text TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    question_type TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '未分类',
                    answer TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    image_path TEXT NOT NULL DEFAULT '',
                    question_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS practice_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL DEFAULT '',
                    wrong_only INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    correct INTEGER NOT NULL DEFAULT 0,
                    config_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id INTEGER REFERENCES practice_questions(id),
                    run_id INTEGER REFERENCES practice_runs(id),
                    question_snapshot TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    question_type TEXT NOT NULL,
                    input_type TEXT NOT NULL,
                    model_answer TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    needs_review INTEGER NOT NULL,
                    is_correct INTEGER,
                    error_category TEXT,
                    model_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    timings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS citations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    snippet TEXT NOT NULL,
                    rank INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    correct INTEGER NOT NULL,
                    corrected_answer TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_question ON attempts(question_id, id);
                CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_feedback_attempt ON feedback(attempt_id, id);
                """
            )

    @staticmethod
    def _question_from_row(row: sqlite3.Row, include_answer: bool = False) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "question_text": row["question_text"],
            "question_type": row["question_type"],
            "options": json.loads(row["options_json"]),
            "topic": row["topic"],
            "source": row["source"],
            "has_image": bool(row["image_path"]),
        }
        if include_answer:
            result.update(
                answer=row["answer"],
                explanation=row["explanation"],
                image_path=row["image_path"],
                question_hash=row["question_hash"],
            )
        return result

    def insert_questions(self, records: list[tuple[QuestionRecord, str]]) -> int:
        inserted = 0
        with self.connect() as connection:
            for record, question_hash in records:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO practice_questions (
                        question_text, options_json, question_type, topic, answer,
                        explanation, source, image_path, question_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.question_text,
                        json.dumps(record.options, ensure_ascii=False),
                        record.question_type,
                        record.topic,
                        record.answer,
                        record.explanation,
                        record.source,
                        record.image_path,
                        question_hash,
                        utc_now(),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def create_run(self, topic: str, wrong_only: bool, config_version: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO practice_runs (topic, wrong_only, config_version, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (topic, int(wrong_only), config_version, utc_now()),
            )
            return int(cursor.lastrowid)

    def get_question(self, question_id: int, include_answer: bool = False) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM practice_questions WHERE id = ?", (question_id,)
            ).fetchone()
        return self._question_from_row(row, include_answer) if row else None

    def next_question(
        self, run_id: int, topic: str = "", wrong_only: bool = False
    ) -> dict[str, Any] | None:
        conditions = ["1 = 1"]
        parameters: list[Any] = []
        if topic:
            conditions.append("q.topic = ?")
            parameters.append(topic)
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM attempts used WHERE used.run_id = ? AND used.question_id = q.id)"
        )
        parameters.append(run_id)
        if wrong_only:
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM attempts a
                    WHERE a.question_id = q.id
                      AND a.id = (SELECT MAX(a2.id) FROM attempts a2 WHERE a2.question_id = q.id)
                      AND COALESCE(
                          (SELECT f.correct FROM feedback f WHERE f.attempt_id = a.id ORDER BY f.id DESC LIMIT 1),
                          a.is_correct
                      ) = 0
                )
                """
            )
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT q.* FROM practice_questions q WHERE {' AND '.join(conditions)} ORDER BY RANDOM() LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                connection.execute(
                    "UPDATE practice_runs SET completed_at = COALESCE(completed_at, ?) WHERE id = ?",
                    (utc_now(), run_id),
                )
        return self._question_from_row(row) if row else None

    def record_attempt(
        self,
        response: SolveResponse,
        *,
        question_id: int | None = None,
        run_id: int | None = None,
        input_type: str = "text",
        is_correct: bool | None = None,
        error_category: str | None = None,
        model_version: str,
        prompt_version: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO attempts (
                    question_id, run_id, question_snapshot, options_json, question_type,
                    input_type, model_answer, explanation, confidence, needs_review,
                    is_correct, error_category, model_version, prompt_version,
                    timings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    run_id,
                    response.question_text,
                    json.dumps(response.options, ensure_ascii=False),
                    response.question_type,
                    input_type,
                    response.answer,
                    response.explanation,
                    response.confidence,
                    int(response.needs_review),
                    None if is_correct is None else int(is_correct),
                    error_category,
                    model_version,
                    prompt_version,
                    json.dumps(response.timings),
                    utc_now(),
                ),
            )
            attempt_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO citations (attempt_id, title, url, snippet, rank) VALUES (?, ?, ?, ?, ?)",
                [
                    (attempt_id, source.title, source.url, source.snippet, source.rank)
                    for source in response.sources
                ],
            )
            if run_id is not None:
                connection.execute(
                    """
                    UPDATE practice_runs SET
                        total = (SELECT COUNT(*) FROM attempts WHERE run_id = ?),
                        correct = (SELECT COUNT(*) FROM attempts WHERE run_id = ? AND is_correct = 1)
                    WHERE id = ?
                    """,
                    (run_id, run_id, run_id),
                )
        return attempt_id

    def attempt_exists(self, attempt_id: int) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone() is not None

    def add_feedback(
        self, attempt_id: int, correct: bool, corrected_answer: str, note: str
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO feedback (attempt_id, correct, corrected_answer, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (attempt_id, int(correct), corrected_answer, note, utc_now()),
            )
            return int(cursor.lastrowid)

    def list_mistakes(self, topic: str = "") -> list[dict[str, Any]]:
        parameters: list[Any] = []
        topic_filter = "AND q.topic = ?" if topic else ""
        if topic:
            parameters.append(topic)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.id AS attempt_id, a.model_answer, a.explanation AS model_explanation,
                       a.error_category, a.created_at, q.id AS question_id, q.question_text,
                       q.question_type, q.options_json, q.topic, q.answer AS correct_answer,
                       q.explanation AS correct_explanation,
                       (SELECT f.corrected_answer FROM feedback f WHERE f.attempt_id = a.id ORDER BY f.id DESC LIMIT 1) AS corrected_answer
                FROM attempts a JOIN practice_questions q ON q.id = a.question_id
                WHERE a.id = (SELECT MAX(a2.id) FROM attempts a2 WHERE a2.question_id = q.id)
                  AND COALESCE(
                      (SELECT f.correct FROM feedback f WHERE f.attempt_id = a.id ORDER BY f.id DESC LIMIT 1),
                      a.is_correct
                  ) = 0
                  {topic_filter}
                ORDER BY a.id DESC LIMIT 100
                """,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "options": json.loads(row["options_json"]),
                "options_json": None,
            }
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.id, a.is_correct, a.timings_json, a.created_at,
                       q.question_type, q.topic,
                       (SELECT f.correct FROM feedback f WHERE f.attempt_id = a.id ORDER BY f.id DESC LIMIT 1) AS feedback_correct
                FROM attempts a JOIN practice_questions q ON q.id = a.question_id
                """
            ).fetchall()
            runs = connection.execute(
                "SELECT * FROM practice_runs ORDER BY id DESC LIMIT 10"
            ).fetchall()

        overall = {"attempts": len(rows), "graded": 0, "correct": 0, "accuracy": None}
        by_type: dict[str, dict[str, Any]] = {}
        by_topic: dict[str, dict[str, Any]] = {}
        latencies: list[float] = []

        def update(group: dict[str, Any], correct: bool | None) -> None:
            group["graded"] += 1
            if correct:
                group["correct"] += 1
            group["accuracy"] = group["correct"] / group["graded"] if group["graded"] else None

        for row in rows:
            correct_value = row["feedback_correct"]
            if correct_value is None:
                correct_value = row["is_correct"]
            if correct_value is None:
                continue
            correct = bool(correct_value)
            update(overall, correct)
            for collection, key in ((by_type, row["question_type"]), (by_topic, row["topic"])):
                group = collection.setdefault(key, {"graded": 0, "correct": 0, "accuracy": None})
                update(group, correct)
            try:
                total = json.loads(row["timings_json"]).get("total")
                if total is not None:
                    latencies.append(float(total))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        latencies.sort()

        def percentile(fraction: float) -> float | None:
            if not latencies:
                return None
            index = min(len(latencies) - 1, round((len(latencies) - 1) * fraction))
            return round(latencies[index], 3)

        return {
            "overall": overall,
            "by_type": by_type,
            "by_topic": by_topic,
            "latency_seconds": {"p50": percentile(0.5), "p95": percentile(0.95)},
            "recent_runs": [dict(run) for run in runs],
        }

    def question_hashes(self) -> set[str]:
        with self.connect() as connection:
            return {row[0] for row in connection.execute("SELECT question_hash FROM practice_questions")}

    def question_texts(self) -> list[str]:
        with self.connect() as connection:
            return [row[0] for row in connection.execute("SELECT question_text FROM practice_questions")]
