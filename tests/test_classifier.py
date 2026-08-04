from app.core.classifier import classify
from app.models.schemas import Complexity


def test_greeting_is_simple():
    result = classify("hi there")
    assert result.complexity == Complexity.simple


def test_short_prompt_is_simple():
    result = classify("what's 2+2?")
    assert result.complexity in (Complexity.simple, Complexity.medium)


def test_code_heavy_prompt_is_complex():
    prompt = (
        "Explain step by step, then compare and analyze the trade-offs, why we "
        "should design and optimize this algorithm. ```python\ndef refill(bucket): pass\n``` "
        "After that, debug and refactor it, and analyze the derivative of throughput."
    )
    result = classify(prompt)
    assert result.complexity == Complexity.complex


def test_empty_prompt_is_simple():
    result = classify("")
    assert result.complexity == Complexity.simple
    assert result.score == 0.0


def test_score_is_bounded():
    result = classify("a" * 5000 + " explain analyze compare design architect optimize " * 20)
    assert 0.0 <= result.score <= 1.0
