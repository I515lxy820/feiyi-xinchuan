import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .question_io import normalized_question_text, question_hash, similarity


@dataclass
class BankMatch:
    exact: dict[str, Any] | None = None
    references: list[dict[str, Any]] = field(default_factory=list)


class QuestionBank:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, question_text, options_json, question_type, answer,
                       explanation, source, question_hash
                FROM practice_questions
                WHERE answer_status = 'confirmed'
                """
            ).fetchall()
        return [
            {
                **dict(row),
                "options": json.loads(row["options_json"]),
                "normalized_text": normalized_question_text(row["question_text"]),
            }
            for row in rows
        ]

    def lookup(
        self,
        question_text: str,
        *,
        exclude_hash: str = "",
        reference_limit: int = 3,
    ) -> BankMatch:
        if not question_text.strip():
            return BankMatch()
        normalized_input = normalized_question_text(question_text)
        digest = question_hash(question_text)
        rows = [row for row in self._rows() if row["question_hash"] != exclude_hash]
        exact = None
        for row in rows:
            if row["question_hash"] == digest:
                exact = row
                break
            option_length = sum(
                len(normalized_question_text(value)) for value in row["options"].values()
            )
            if (
                len(row["normalized_text"]) >= 12
                and row["normalized_text"] in normalized_input
                and len(normalized_input) <= len(row["normalized_text"]) + option_length + 20
                and (exact is None or len(row["normalized_text"]) > len(exact["normalized_text"]))
            ):
                exact = row

        candidates = []
        for row in rows:
            if exact is not None and row["id"] == exact["id"]:
                continue
            score = similarity(question_text, row["question_text"])
            if score >= 0.28:
                candidates.append((score, row))
        candidates.sort(key=lambda item: item[0], reverse=True)
        references = []
        for score, row in candidates[:reference_limit]:
            references.append(
                {
                    "question_text": row["question_text"],
                    "question_type": row["question_type"],
                    "options": row["options"],
                    "answer": row["answer"],
                    "explanation": row["explanation"],
                    "similarity": round(score, 3),
                }
            )
        return BankMatch(exact=exact, references=references)
