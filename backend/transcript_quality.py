"""Local transcript normalization and hallucination gates."""

from __future__ import annotations

import math
import re
from typing import Any

NO_SPEECH_THRESHOLD = 0.60
MIN_AVERAGE_LOGPROB = -1.0

_KNOWN_HALLUCINATIONS = frozenset(
    {
        "thankyou",
        "thanksforwatching",
        "subscribe",
        "you",
        "bye",
        "谢谢",
        "谢谢观看",
        "感谢观看",
        "请订阅",
        "字幕由amaramorg社区提供",
    }
)
_PHRASE_NORMALIZER = re.compile(r"[^0-9a-z\u3400-\u9fff]+")


def average_logprob(probability: Any) -> float | None:
    try:
        value = float(probability)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return math.log(min(value, 1.0))


def hallucination_reason(
    text: str,
    *,
    segment_average_logprob: float | None,
    no_speech_probability: float | None,
) -> str | None:
    normalized = _PHRASE_NORMALIZER.sub("", text.casefold())
    if normalized in _KNOWN_HALLUCINATIONS:
        return "known_hallucination"
    if no_speech_probability is not None and no_speech_probability >= NO_SPEECH_THRESHOLD:
        return "no_speech_probability"
    if segment_average_logprob is not None and segment_average_logprob < MIN_AVERAGE_LOGPROB:
        return "low_average_logprob"
    return None


def traditional_to_simplified(text: str) -> str:
    from opencc import OpenCC

    return OpenCC("t2s").convert(text)
