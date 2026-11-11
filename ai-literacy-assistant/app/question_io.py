import csv
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .answers import normalize_answer
from .schemas import QuestionRecord


def normalized_question_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def question_hash(text: str) -> str:
    return hashlib.sha256(normalized_question_text(text).encode("utf-8")).hexdigest()


def similarity(left: str, right: str) -> float:
    def grams(text: str) -> set[str]:
        value = normalized_question_text(text)
        return {value[index:index + 3] for index in range(max(1, len(value) - 2))}

    left_grams, right_grams = grams(left), grams(right)
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 1.0


def _parse_options(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key).strip().upper(): str(item).strip() for key, item in value.items()}
    if isinstance(value, list):
        return {chr(65 + index): str(item).strip() for index, item in enumerate(value)}
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        return _parse_options(json.loads(text))
    except json.JSONDecodeError:
        options: dict[str, str] = {}
        for part in re.split(r"\|\||\n", text):
            match = re.match(r"\s*([A-Za-z])\s*(?:=|[:：.、．])\s*(.+)", part.strip())
            if not match:
                raise ValueError(f"无法解析选项：{part}")
            options[match.group(1).upper()] = match.group(2).strip()
        return options


def validate_record(record: QuestionRecord) -> QuestionRecord:
    record.question_text = record.question_text.strip()
    record.options = _parse_options(record.options)
    record.answer = normalize_answer(record.answer, record.question_type)
    if record.question_type not in {"single", "multiple", "boolean"}:
        raise ValueError("比赛题库仅支持 single（单选）、multiple（多选）、boolean（判断）")
    if record.question_type in {"single", "multiple"}:
        if not record.options:
            raise ValueError("选择题必须提供 options")
        labels = record.answer.split(",")
        if not record.answer or any(label not in record.options for label in labels):
            raise ValueError("选择题答案必须是由有效选项标签组成的答案")
        if record.question_type == "single" and len(labels) != 1:
            raise ValueError("单选题只能有一个答案标签")
    if record.question_type == "boolean" and record.answer not in {"正确", "错误"}:
        raise ValueError("判断题标准答案必须是“正确”或“错误”")
    return record


def load_question_records(path: str | Path) -> list[QuestionRecord]:
    source_path = Path(path).resolve()
    if source_path.suffix.lower() == ".json":
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        raw_records = payload.get("questions") if isinstance(payload, dict) else payload
        if not isinstance(raw_records, list):
            raise ValueError("JSON 题库必须是数组，或包含 questions 数组")
    elif source_path.suffix.lower() == ".csv":
        with source_path.open(encoding="utf-8-sig", newline="") as handle:
            raw_records = list(csv.DictReader(handle))
    else:
        raise ValueError("仅支持 JSON 或 CSV 题库")

    records: list[QuestionRecord] = []
    hashes: dict[str, int] = {}
    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 题不是对象")
        values = dict(raw)
        values["options"] = _parse_options(values.get("options"))
        values["answer"] = values.get("answer") or values.get("correct_answer") or ""
        try:
            record = validate_record(QuestionRecord.model_validate(values))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"第 {index} 题校验失败：{exc}") from exc
        if record.image_path:
            image = Path(record.image_path)
            record.image_path = str(image if image.is_absolute() else (source_path.parent / image).resolve())
        digest = question_hash(record.question_text)
        if digest in hashes:
            raise ValueError(f"第 {index} 题与第 {hashes[digest]} 题重复")
        hashes[digest] = index
        records.append(record)
    if not records:
        raise ValueError("题库为空")
    return records


def assert_no_leakage(records: list[QuestionRecord], existing_hashes: set[str], existing_texts: list[str]) -> None:
    for index, record in enumerate(records, start=1):
        if question_hash(record.question_text) in existing_hashes:
            raise ValueError(f"数据泄漏：第 {index} 题已存在于日常练习题库")
        for existing in existing_texts:
            if similarity(record.question_text, existing) >= 0.88:
                raise ValueError(f"疑似近重复泄漏：第 {index} 题与练习题高度相似，请人工拆分")
