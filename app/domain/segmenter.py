from dataclasses import dataclass


BOUNDARY_CHARS = set("。！？!?；;：:\n")


@dataclass(frozen=True)
class TextSegment:
    index: int
    text: str


def split_text(text: str, target_chars: int = 900, max_chars: int = 1400) -> list[TextSegment]:
    if target_chars <= 0 or max_chars < target_chars:
        raise ValueError("max_chars must be greater than or equal to target_chars")
    if not text:
        return []

    segments: list[TextSegment] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        if text_length - start <= max_chars:
            end = text_length
        else:
            target_end = min(start + target_chars, text_length)
            hard_end = min(start + max_chars, text_length)
            before_target = [
                position
                for position in range(start + 1, target_end + 1)
                if text[position - 1] in BOUNDARY_CHARS
            ]
            after_target = [
                position
                for position in range(target_end + 1, hard_end + 1)
                if text[position - 1] in BOUNDARY_CHARS
            ]
            if before_target:
                end = before_target[-1]
            elif after_target:
                end = after_target[0]
            else:
                end = hard_end

        chunk = text[start:end]
        if not chunk:
            raise ValueError("segmenter produced an empty segment")
        segments.append(TextSegment(index=len(segments), text=chunk))
        start = end

    if "".join(segment.text for segment in segments) != text:
        raise ValueError("segmenter lost or duplicated source text")
    return segments

