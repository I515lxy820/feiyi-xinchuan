#!/usr/bin/env python3
"""解析竞赛平台模拟练习解析页的原始文本，导入题库。"""
import re
import json
import hashlib
import sys
import os

BATCH_ID = "df2ffd9a"
SOURCE = f"竞赛平台模拟练习解析页 {BATCH_ID}"
INPUT = "/workspace/question_bank/raw_exam_df2ffd9a.txt"
BANK_JSON = "/workspace/question_bank/question_bank.json"
OUTPUT_BATCH = f"/workspace/question_bank/batches/{BATCH_ID}_new.json"

def make_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]

def parse_questions(text: str):
    """从原始文本中解析题目"""
    lines = text.split("\n")
    questions = []
    
    # 跳过头部信息，从第一个 "01." 开始
    start_idx = 0
    for i, line in enumerate(lines):
        if re.match(r'^\d+\.$', line.strip()):
            start_idx = i
            break
    
    # 按题目编号切分
    current_q = None
    current_section = None  # question, options, answer, explanation
    option_buffer = []
    
    i = start_idx
    while i < len(lines):
        line = lines[i].strip()
        
        # 检测新题目开始
        if re.match(r'^\d+\.$', line):
            if current_q and current_q.get("question_text"):
                questions.append(current_q)
            num = line.rstrip(".")
            current_q = {"_num": int(num)}
            current_section = "type"
            option_buffer = []
            i += 1
            continue
        
        # 题型
        if current_section == "type" and line in ("单选题", "多选题", "判断题"):
            current_q["question_type"] = line
            current_section = "question"
            i += 1
            continue
        
        # 题目文本
        if current_section == "question":
            if line.startswith("正确答案：") or line.startswith("收起解析") or line.startswith("解析"):
                if current_q.get("question_text"):
                    current_section = "options"
                else:
                    i += 1
                    continue
            elif line and not line.startswith("展开") and not line.startswith("收起"):
                current_q.setdefault("question_text", "")
                current_q["question_text"] += line + "\n"
                i += 1
                continue
            else:
                if current_q.get("question_text"):
                    current_q["question_text"] = current_q["question_text"].strip()
                    current_section = "options"
                i += 1
                continue
        
        # 选项
        if current_section == "options":
            if line.startswith("收起解析") or line.startswith("正确答案："):
                current_section = "answer"
                i += 1
                continue
            m = re.match(r'^([A-Z])\s*(.*)', line)
            if m:
                if option_buffer and current_q.get("question_type") == "判断题":
                    # 判断题选项
                    current_q.setdefault("options", {})[m.group(1)] = m.group(2)
                else:
                    option_buffer = [m.group(1), m.group(2)]
                    current_q.setdefault("options", {})[m.group(1)] = m.group(2)
                i += 1
                continue
            elif line and current_q.get("options"):
                # 选项续行
                last_key = list(current_q["options"].keys())[-1]
                current_q["options"][last_key] += line
                i += 1
                continue
            i += 1
            continue
        
        # 答案
        if current_section == "answer":
            if line.startswith("正确答案："):
                ans_text = line.replace("正确答案：", "").split("你的答案")[0].strip()
                # 处理多选题格式 ABCD -> A,B,C,D
                current_q["answer"] = ",".join(sorted(ans_text)) if len(ans_text) > 1 and all(c in "ABCD" for c in ans_text) else ans_text
                current_section = "explanation"
                i += 1
                continue
        
        # 解析
        if current_section == "explanation":
            if line == "解析":
                i += 1
                continue
            if re.match(r'^\d+\.$', line):
                # 新题目开始
                if current_q and current_q.get("question_text"):
                    questions.append(current_q)
                num = line.rstrip(".")
                current_q = {"_num": int(num)}
                current_section = "type"
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

def main():
    # 读取原始文本
    with open(INPUT, "r", encoding="utf-8") as f:
        raw = f.read()
    
    # 跳过非题目的头部
    questions = parse_questions(raw)
    print(f"解析到 {len(questions)} 道题")
    
    # 读取现有题库
    with open(BANK_JSON, "r", encoding="utf-8") as f:
        existing = json.load(f)
    
    existing_hashes = {make_hash(q["question_text"]) for q in existing}
    existing_texts = {q["question_text"].strip() for q in existing}
    
    new_questions = []
    for q in questions:
        qt = q["question_text"].strip()
        h = make_hash(qt)
        
        if h in existing_hashes or qt in existing_texts:
            continue
        
        cleaned = {
            "question_text": qt,
            "question_type": q["question_type"],
            "options": q.get("options", {}),
            "answer": q.get("answer", ""),
            "explanation": q.get("explanation", "").strip(),
            "source": SOURCE,
            "answer_status": "confirmed",
            "question_hash": h,
            "topic": "",
        }
        
        # 判断题特殊处理
        if cleaned["question_type"] == "判断题":
            cleaned["options"] = {}
        
        new_questions.append(cleaned)
        existing.append(cleaned)
        existing_hashes.add(h)
        existing_texts.add(qt)
    
    print(f"新增题目: {len(new_questions)}，跳过重复: {len(questions) - len(new_questions)}")
    print(f"题库总数: {len(existing)}")
    
    # 保存批次新题
    os.makedirs(os.path.dirname(OUTPUT_BATCH), exist_ok=True)
    with open(OUTPUT_BATCH, "w", encoding="utf-8") as f:
        for q in new_questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    print(f"批次新题已保存: {OUTPUT_BATCH}")
    
    # 更新题库JSON
    with open(BANK_JSON, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"题库JSON已更新: {BANK_JSON}")
    
    # 输出预览
    if new_questions:
        print("\n=== 新增题目预览 ===")
        for q in new_questions[:3]:
            print(f"[{q['question_type']}] {q['question_text'][:60]}...")
            print(f"  答案: {q['answer']}")
            print(f"  解析: {q['explanation'][:60]}...")
            print()

if __name__ == "__main__":
    main()
