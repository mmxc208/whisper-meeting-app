import math

from backend.transcript_quality import (
    average_logprob,
    hallucination_reason,
    traditional_to_simplified,
)


def test_average_logprob_converts_geometric_mean_probability() -> None:
    assert average_logprob(math.exp(-0.5)) == -0.5
    assert average_logprob(float("nan")) is None


def test_known_hallucination_and_confidence_gates_are_classified() -> None:
    assert (
        hallucination_reason("Thank you.", segment_average_logprob=-0.2, no_speech_probability=0.01)
        == "known_hallucination"
    )
    assert (
        hallucination_reason("real words", segment_average_logprob=-1.2, no_speech_probability=0.01)
        == "low_average_logprob"
    )
    assert (
        hallucination_reason("real words", segment_average_logprob=-0.2, no_speech_probability=0.8)
        == "no_speech_probability"
    )


def test_confident_non_boilerplate_text_passes() -> None:
    assert (
        hallucination_reason(
            "这是正常的会议内容", segment_average_logprob=-0.2, no_speech_probability=0.01
        )
        is None
    )


def test_traditional_chinese_is_normalized_to_simplified() -> None:
    assert traditional_to_simplified("會議裡的軟體與資料") == "会议里的软体与资料"
