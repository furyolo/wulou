"""将题湖属性文本整理为可审计、可安全持久化的模型输入快照。"""

from __future__ import annotations

import html
import re
from typing import Any


SNAPSHOT_VERSION = "2026-09-10.3"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>", re.IGNORECASE | re.DOTALL)
_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[0-9Xx](?![0-9A-Za-z])")
_NUMBERED_PART_RE = re.compile(
    r"(?:^|[。；;.!！？?\n]|(?:解答?|答案?)\s*[:：])\s*[（(]\s*([1-9]\d*)\s*[）)]"
)


def _convert_fangzheng_typesetting(value: str) -> str:
    """转换题湖旧题库中常见的方正排版控制码，不猜测未知控制码。"""
    text = value
    replacements = {
        "〖JB(|〗": "|", "〖JB)|〗": "|",
        "〖JB((〗": "(", "〖JB))〗": ")",
        "\ue00a": "", "\ue003": " ",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"〖KF\(〗(.*?)〖KF\)〗", r"√(\1)", text)
    text = re.sub(r"〖SX\(〗(.*?)〖〗(.*?)〖SX\)〗", r"(\1)/(\2)", text)
    text = re.sub(r"\ue00b\ue008(.*?)\ue009", r"^(\1)", text)
    text = re.sub(r"\ue00b([+\-]?\d+)", r"^(\1)", text)
    return text.replace("\ue008", "").replace("\ue009", "")


def _redact(value: str) -> str:
    """仅保留数学内容，隐藏偶然混入题干的常见个人标识。"""
    value = _EMAIL_RE.sub("[已隐藏邮箱]", value)
    value = _PHONE_RE.sub("[已隐藏手机号]", value)
    return _ID_CARD_RE.sub("[已隐藏证件号]", value)


def normalize_model_text(value: Any) -> str:
    """清理 HTML、常见方正排版码和空白，保留原有数学语义。"""
    text = html.unescape(str(value or ""))
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _convert_fangzheng_typesetting(text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return _redact(text)


def _warnings(prefix: str, raw: Any, normalized: str, latex: str) -> list[str]:
    source = _convert_fangzheng_typesetting(html.unescape(str(raw or "")))
    warnings: list[str] = []
    if not normalized:
        warnings.append(f"{prefix}_text_missing")
    if "\ufffd" in source or _PRIVATE_USE_RE.search(source) or _CONTROL_RE.search(source):
        warnings.append(f"{prefix}_unrecognized_typesetting")
    if latex and latex.count("{") != latex.count("}"):
        warnings.append(f"{prefix}_unbalanced_latex")
    return warnings


def _numbered_parts(value: str) -> set[int]:
    """只识别结构性小题编号，排除分数 ``(1)/(2)`` 等公式括号。"""
    return {int(match) for match in _NUMBERED_PART_RE.findall(value)}


def _field_snapshot(question: dict[str, Any], prefix: str) -> tuple[dict[str, Any], list[str]]:
    press_key = f"{prefix}_press"
    text_key = f"{prefix}_text"
    latex_key = f"{prefix}_latex"
    raw_press = question.get(press_key, "")
    text = normalize_model_text(raw_press)
    supplemental = normalize_model_text(question.get(text_key, ""))
    latex = normalize_model_text(question.get(latex_key, ""))
    if supplemental == text:
        supplemental = ""
    if latex in {text, supplemental}:
        latex = ""
    warnings = _warnings(prefix, raw_press, text, latex)
    return {
        "text": text,
        "supplemental_text": supplemental or None,
        "latex": latex or None,
    }, warnings


def build_model_input_snapshot(question: dict[str, Any]) -> dict[str, Any]:
    """生成与模型文本输入一致的快照，不保存图片 URL、令牌或页面原始 DOM。"""
    question_field, question_warnings = _field_snapshot(question, "question")
    answer_field, answer_warnings = _field_snapshot(question, "answer")
    captured_answer = answer_field["text"]
    question_parts = _numbered_parts(question_field["text"])
    answer_parts = _numbered_parts(captured_answer)
    # 题湖部分旧题的答案字段串入下一道题。题干没有多小题而答案同时出现（1）（2）时，
    # 保留原文供人工审计，但不让污染答案改变清晰题干的分类结果。
    if {1, 2}.issubset(answer_parts) and not {1, 2}.issubset(question_parts) and question_field["text"]:
        answer_field["captured_text"] = captured_answer
        answer_field["text"] = ""
        answer_field["supplemental_text"] = None
        answer_field["latex"] = None
        answer_field["used_for_classification"] = False
        answer_field["omitted_reason"] = "suspected_extra_parts"
        answer_warnings.append("answer_suspected_extra_parts")
    else:
        answer_field["captured_text"] = captured_answer
        answer_field["used_for_classification"] = True
        answer_field["omitted_reason"] = None
    scope = question.get("scope") if isinstance(question.get("scope"), dict) else {}
    site_context = normalize_model_text(question.get("source", ""))
    if question_field["text"] and question_field["text"] in site_context:
        site_context = ""
    warnings = question_warnings + answer_warnings
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "question": question_field,
        "answer": answer_field,
        "page_scope_hint": normalize_model_text(scope.get("title", ""))[:300],
        "site_context": site_context[:240] or None,
        "question_image_available": bool(str(question.get("question_image_url", "")).strip()),
        "answer_image_available": bool(str(question.get("answer_image_url", "")).strip()),
        "warnings": warnings,
    }
