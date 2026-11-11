import re
import unicodedata

from .schemas import SolveResponse


AUTO_GRADED_TYPES = {"single", "multiple", "boolean", "fill"}


def normalize_answer(answer: str, question_type: str) -> str:
    value = unicodedata.normalize("NFKC", answer or "").strip()
    if question_type == "boolean":
        compact = re.sub(r"[\s。．.]", "", value).lower()
        if compact in {"正确", "对", "是", "true", "√", "对勾"}:
            return "正确"
        if compact in {"错误", "错", "否", "false", "×", "x"}:
            return "错误"
    if question_type == "multiple":
        upper = value.upper()
        compact = re.sub(r"[\s,，、;；/]+", "", upper)
        compact = re.sub(r"^(答案|选项|答案是)[:：]?", "", compact)
        if re.fullmatch(r"[A-Z]{2,}", compact):
            return ",".join(sorted(set(compact)))
        letters = re.findall(r"(?<![A-Z])[A-Z](?![A-Z])", upper)
        return ",".join(sorted(set(letters)))
    if question_type == "single":
        match = re.search(r"(?<![A-Z])([A-Z])(?![A-Z])", value.upper())
        return match.group(1) if match else value.upper()
    if question_type == "fill":
        return re.sub(r"\s+", "", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def answers_match(predicted: str, expected: str, question_type: str) -> bool | None:
    if question_type not in AUTO_GRADED_TYPES:
        return None
    if not predicted.strip():
        return False
    return normalize_answer(predicted, question_type) == normalize_answer(expected, question_type)


def classify_error(response: SolveResponse) -> str | None:
    if response.is_correct is None or response.is_correct:
        return None
    warning_text = "；".join(response.warnings)
    if "识别" in warning_text:
        return "图片识别"
    if "格式" in warning_text:
        return "输出格式"
    if "冲突" in warning_text:
        return "来源冲突"
    if "搜索" in warning_text or "检索" in warning_text or "来源" in warning_text:
        return "检索不足"
    return "知识/推理"
