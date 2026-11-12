#!/usr/bin/env python3
"""解析竞赛平台模拟练习解析页的原始文本，导入题库（JSON + SQLite + Excel）。

用法: python3 parse_exam.py <batch_id>
原始文本: /workspace/question_bank/raw_exam_<batch_id>.txt
"""
import re
import json
import hashlib
import sys
import os
import sqlite3
import datetime
import unicodedata

BANK_JSON = "/workspace/question_bank/question_bank.json"
BANK_XLSX = "/workspace/question_bank/question_bank.xlsx"
DB_PATH = "/workspace/ai-literacy-assistant/data/app.db"
DB_BACKUP = "/workspace/question_bank/app.db.backup"

TYPE_MAP = {"单选题": "single", "多选题": "multiple", "判断题": "boolean"}
TYPE_CN = {"single": "单选", "multiple": "多选", "boolean": "判断"}


def make_hash(text: str) -> str:
    """与 app.question_io.question_hash 完全一致，保证查询/自排除能命中。"""
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_questions(text: str):
    lines = text.split("\n")
    questions = []

    start_idx = 0
    for i, line in enumerate(lines):
        if re.match(r'^\d+\.$', line.strip()):
            start_idx = i
            break

    current_q = None
    current_section = None
    i = start_idx
    while i < len(lines):
        line = lines[i].strip()

        if re.match(r'^\d+\.$', line):
            if current_q and current_q.get("question_text"):
                questions.append(current_q)
            current_q = {"_num": int(line.rstrip("."))}
            current_section = "type"
            i += 1
            continue

        if current_section == "type" and line in TYPE_MAP:
            current_q["question_type"] = line
            current_section = "question"
            i += 1
            continue

        if current_section == "question":
            if line.startswith("正确答案：") or line.startswith("收起解析") or line.startswith("解析"):
                if current_q.get("question_text"):
                    current_section = "options"
                i += 1
                continue
            if line and not line.startswith("展开") and not line.startswith("收起"):
                current_q.setdefault("question_text", "")
                current_q["question_text"] += line + "\n"
                i += 1
                continue
            if current_q.get("question_text"):
                current_q["question_text"] = current_q["question_text"].strip()
                current_section = "options"
            i += 1
            continue

        if current_section == "options":
            if line.startswith("收起解析") or line.startswith("正确答案："):
                current_section = "answer"
                i += 1
                continue
            m = re.match(r'^([A-Z])\s*(.*)', line)
            if m:
                current_q.setdefault("options", {})[m.group(1)] = m.group(2)
                i += 1
                continue
            if line and current_q.get("options"):
                last_key = list(current_q["options"].keys())[-1]
                current_q["options"][last_key] += line
                i += 1
                continue
            i += 1
            continue

        if current_section == "answer":
            if line.startswith("正确答案："):
                ans_text = line.replace("正确答案：", "").split("你的答案")[0].strip()
                if len(ans_text) > 1 and all(c in "ABCD" for c in ans_text):
                    ans_text = ",".join(sorted(ans_text))
                current_q["answer"] = ans_text
                current_section = "explanation"
                i += 1
                continue

        if current_section == "explanation":
            if line == "解析":
                i += 1
                continue
            current_q.setdefault("explanation", "")
            current_q["explanation"] += line + "\n"
            i += 1
            continue

        i += 1

    if current_q and current_q.get("question_text"):
        questions.append(current_q)
    return questions


def normalize(q: dict) -> None:
    cn = q.get("question_type")
    if cn in TYPE_MAP:
        q["question_type"] = TYPE_MAP[cn]
    text = q.get("question_text", "")
    for pattern in (r"\n\s*A\s*(正确|错误)", r"\n\s*A\s*[.、．]", r"\n\s*正确答案", r"\n\s*收起解析", r"\n\s*展开全部"):
        match = re.search(pattern, text)
        if match:
            text = text[:match.start()]
    q["question_text"] = text.strip()
    if q["question_type"] == "boolean":
        if q["answer"] == "A":
            q["answer"] = "正确"
        elif q["answer"] == "B":
            q["answer"] = "错误"
        q["options"] = {}
    q.setdefault("options", {})
    q.setdefault("explanation", "")
    q["explanation"] = q["explanation"].strip()


def main():
    if len(sys.argv) < 2:
        print("用法: python3 parse_exam.py <batch_id>")
        sys.exit(1)
    batch = sys.argv[1]
    source = f"竞赛平台模拟练习解析页 {batch}"
    raw_path = f"/workspace/question_bank/raw_exam_{batch}.txt"
    batch_out = f"/workspace/question_bank/batches/{batch}_new.json"

    with open(raw_path, "r", encoding="utf-8") as f:
        raw = f.read()
    questions = parse_questions(raw)
    print(f"解析到 {len(questions)} 道题")

    with open(BANK_JSON, "r", encoding="utf-8") as f:
        bank = json.load(f)

    existing_hashes = set()
    existing_texts = set()
    for q in bank:
        if not q.get("question_hash"):
            q["question_hash"] = make_hash(q["question_text"])
        existing_hashes.add(q["question_hash"])
        existing_texts.add(q["question_text"].strip())

    new_questions = []
    for q in questions:
        normalize(q)
        text = q["question_text"].strip()
        h = make_hash(text)
        if h in existing_hashes or text in existing_texts:
            continue
        cleaned = {
            "question_text": text,
            "question_type": q["question_type"],
            "options": q.get("options", {}),
            "answer": q.get("answer", ""),
            "explanation": q.get("explanation", ""),
            "source": source,
            "answer_status": "confirmed",
            "question_hash": h,
            "topic": "",
        }
        new_questions.append(cleaned)
        bank.append(cleaned)
        existing_hashes.add(h)
        existing_texts.add(text)

    print(f"新增题目: {len(new_questions)}，跳过重复: {len(questions) - len(new_questions)}")
    print(f"题库总数: {len(bank)}")

    os.makedirs(os.path.dirname(batch_out), exist_ok=True)
    with open(batch_out, "w", encoding="utf-8") as f:
        for q in new_questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    with open(BANK_JSON, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)

    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute("DELETE FROM practice_questions")
        for q in bank:
            q.setdefault("question_hash", make_hash(q["question_text"]))
            cur.execute(
                "INSERT OR REPLACE INTO practice_questions "
                "(question_text, question_hash, question_type, options_json, answer, explanation, source, answer_status, topic, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (q["question_text"], q["question_hash"], q.get("question_type", "single"),
                 json.dumps(q["options"], ensure_ascii=False) if q.get("options") else None,
                 q.get("answer", ""), q.get("explanation", ""), q.get("source", ""),
                 q.get("answer_status", "confirmed"), q.get("topic", ""), now))
        conn.commit()
        print("SQLite 题数:", cur.execute("select count(*) from practice_questions").fetchone()[0])
        conn.close()
        import shutil
        shutil.copy(DB_PATH, DB_BACKUP)

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "题库"
    ws.append(["序号", "题型", "题干", "选项", "答案", "解析", "来源", "状态"])
    for i, q in enumerate(bank, 1):
        opts = q.get("options") or {}
        ws.append([i, TYPE_CN.get(q.get("question_type"), q.get("question_type")),
                   q.get("question_text", ""),
                   "\n".join(f"{k}. {v}" for k, v in opts.items()),
                   q.get("answer", ""), q.get("explanation", ""),
                   q.get("source", ""), q.get("answer_status", "confirmed")])
    wb.save(BANK_XLSX)
    print("Excel 已更新")

    if new_questions:
        print("\n=== 新增预览 ===")
        for q in new_questions[:3]:
            print(f"[{q['question_type']}] 答案={q['answer']} | {q['question_text'][:50]}...")


if __name__ == "__main__":
    main()
