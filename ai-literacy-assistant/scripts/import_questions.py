import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import BASE_DIR, Settings
from app.db import Database
from app.question_io import assert_no_leakage, load_question_records, question_hash


def main() -> None:
    parser = argparse.ArgumentParser(description="校验并导入 AI 信息素养题库")
    parser.add_argument("file", help="JSON 或 CSV 题库路径")
    parser.add_argument("--split", choices=["practice", "dev", "holdout"], default="practice")
    parser.add_argument("--db", help="SQLite 路径，默认读取 APP_DB_PATH")
    args = parser.parse_args()

    settings = Settings()
    database = Database(args.db or settings.database_path)
    database.initialize()
    records = load_question_records(args.file)

    if args.split == "practice":
        inserted = database.insert_questions([(record, question_hash(record.question_text)) for record in records])
        print(f"练习集校验通过：读取 {len(records)} 题，新导入 {inserted} 题，重复忽略 {len(records) - inserted} 题。")
        return

    assert_no_leakage(records, database.question_hashes(), database.question_texts())
    other_split = "dev" if args.split == "holdout" else "holdout"
    other_path = BASE_DIR / "data" / f"{other_split}.json"
    if other_path.exists():
        other_records = load_question_records(other_path)
        assert_no_leakage(
            records,
            {question_hash(record.question_text) for record in other_records},
            [record.question_text for record in other_records],
        )
    output = BASE_DIR / "data" / f"{args.split}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([record.model_dump() for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"{args.split} 集校验通过：{len(records)} 题，已保存到 {output}；未导入日常练习题库。")


if __name__ == "__main__":
    main()
