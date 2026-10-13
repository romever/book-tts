from collections.abc import Iterable


def build_tts_instruction(hints: Iterable[dict[str, object]]) -> str:
    lines = [
        "你是一位有经验的有声书主讲人，正在给听众讲述这段内容。",
        "请用自然的讲述口吻朗读：语速适中，语调有起伏，不要平铺直叙；"
        "遇到转折、因果和关键结论时稍作停顿并适当加重语气；人名、地名、时间清楚读出；"
        "整体沉稳可信，但要有讲故事的感染力，让人愿意一直听下去。",
        "朗读稿已经过口语化整理，请以朗读稿为准；发音提示只用于指导朗读，不要读出提示内容。",
    ]
    confirmed = [hint for hint in hints if hint.get("active") and hint.get("confidence") == "confirmed"]
    if confirmed:
        lines.append("本段特殊读音：")
        for hint in confirmed:
            lines.append(
                f"“{hint['surface']}”读作“{hint['pronunciation']}”，这是{hint['kind']}。"
            )
    else:
        lines.append("本段没有额外的特殊读音提示。")
    return "\n".join(lines)
