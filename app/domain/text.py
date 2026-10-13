import hashlib
import re


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    value = value.replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def to_chinese_quotes(value: str) -> str:
    """英文直引号按出现顺序交替换成中文引号“”。"""
    parts = value.split('"')
    return "".join(
        part + ("“" if index % 2 == 0 else "”") for index, part in enumerate(parts[:-1])
    ) + parts[-1]


# 朗读稿允许把阿拉伯数字改写成中文读法，比较内容时忽略数字相关的字
_NUMERAL_CHARS = set("零〇一二三四五六七八九十百千万亿两")


def _content_bigrams(value: str) -> set[str]:
    chars = "".join(ch for ch in value if "\u4e00" <= ch <= "\u9fff" and ch not in _NUMERAL_CHARS)
    return {chars[index : index + 2] for index in range(len(chars) - 1)}


def content_overlap(original: str, rewritten: str) -> tuple[float, float]:
    """按汉字二元组比较两段文本，返回 (原文被改写稿覆盖的比例, 改写稿内容来自原文的比例)。"""
    source, target = _content_bigrams(original), _content_bigrams(rewritten)
    shared = len(source & target)
    return (shared / len(source) if source else 1.0, shared / len(target) if target else 1.0)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

