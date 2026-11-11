import json

import pytest

from app.question_io import assert_no_leakage, load_question_records, question_hash, similarity


def test_load_json_and_csv(tmp_path):
    json_path = tmp_path / "questions.json"
    json_path.write_text(json.dumps([{
        "question_text": "下列哪项正确？",
        "question_type": "single",
        "options": ["甲", "乙"],
        "answer": "A",
        "topic": "基础",
    }], ensure_ascii=False), encoding="utf-8")
    records = load_question_records(json_path)
    assert records[0].options == {"A": "甲", "B": "乙"}

    csv_path = tmp_path / "questions.csv"
    csv_path.write_text("question_text,question_type,options,answer,topic\n多选题,multiple,A=甲||B=乙||C=丙,\"C,A\",基础\n", encoding="utf-8")
    csv_record = load_question_records(csv_path)[0]
    assert csv_record.answer == "A,C"


def test_duplicate_and_leakage_detection(tmp_path):
    payload = [
        {"question_text": "同一道题", "question_type": "boolean", "answer": "正确"},
        {"question_text": "同一道题", "question_type": "boolean", "answer": "错误"},
    ]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_question_records(path)

    assert similarity("信息检索的基本步骤是什么", "信息检索的基本步骤是什么") == 1.0
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps([payload[0]], ensure_ascii=False), encoding="utf-8")
    records = load_question_records(valid_path)
    with pytest.raises(ValueError, match="数据泄漏"):
        assert_no_leakage(records, {question_hash("同一道题")}, [])


@pytest.mark.parametrize("question_type", ["fill", "essay", "unknown"])
def test_competition_bank_rejects_unsupported_types(tmp_path, question_type):
    path = tmp_path / f"{question_type}.json"
    path.write_text(json.dumps([{
        "question_text": "不属于比赛题型",
        "question_type": question_type,
        "options": {},
        "answer": "答案",
    }], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="仅支持"):
        load_question_records(path)
