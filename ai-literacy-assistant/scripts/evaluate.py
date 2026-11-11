import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import BASE_DIR, Settings
from app.db import Database
from app.evaluation import evaluate_records, summarize
from app.question_io import assert_no_leakage, load_question_records
from app.solver import Solver


async def run(args: argparse.Namespace) -> None:
    settings = Settings()
    if settings.demo_mode:
        raise SystemExit("演示模式不能用于真实准确率验收，请先配置真实视觉模型和搜索 API。")
    records = load_question_records(args.file)
    database = Database(args.db or settings.database_path)
    database.initialize()
    assert_no_leakage(records, database.question_hashes(), database.question_texts())

    solver = Solver(settings)
    try:
        results = await evaluate_records(records, solver, args.concurrency)
    finally:
        await solver.close()
    metrics = summarize(results)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "split": args.split,
        "config": {
            "model": settings.model_name,
            "prompt_version": settings.prompt_version,
            "search_provider": settings.search_provider,
            "answer_cache": False,
        },
        "metrics": metrics,
        "results": results,
    }
    output = Path(args.output) if args.output else BASE_DIR / "data" / "evaluation-latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"详细结果已保存到 {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="在未参与调优的题库上评测准确率和延迟")
    parser.add_argument("file", help="dev 或 holdout JSON/CSV 题库")
    parser.add_argument("--split", choices=["dev", "holdout"], default="holdout")
    parser.add_argument("--db", help="用于泄漏检查的日常练习 SQLite 路径")
    parser.add_argument("--output", help="评测报告输出路径")
    parser.add_argument("--concurrency", type=int, default=2, choices=range(1, 9))
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
