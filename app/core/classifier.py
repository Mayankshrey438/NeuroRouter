"""
Lightweight complexity classifier.

Goal: decide, in <1ms and with zero GPU/network dependency, whether an
incoming prompt is "simple" (route to a small/cheap/fast model), "medium",
or "complex" (route to the larger/more capable model).

This is intentionally a transparent, tunable feature-scoring model rather
than a black-box neural classifier — for a routing layer you want
predictable, explainable decisions (and instant cold-start, no training
data needed). The scoring function is easy to extend with more signals
or to swap out for a trained logistic-regression model later (the
`extract_features` function already returns a clean numeric vector that
a sklearn model could consume directly).
"""
import re
from dataclasses import dataclass

from app.models.schemas import Complexity

# Signals that tend to correlate with a prompt needing more reasoning
CODE_PATTERN = re.compile(r"```|def |class |function\s*\(|import |SELECT |<[a-z]+>", re.IGNORECASE)
MATH_PATTERN = re.compile(r"\b(integral|derivative|matrix|equation|theorem|proof|calculate)\b", re.IGNORECASE)
REASONING_PATTERN = re.compile(
    r"\b(why|explain|analyze|analyse|compare|design|architect|optimi[sz]e|debug|"
    r"refactor|trade-?off|strategy|algorithm|prove)\b",
    re.IGNORECASE,
)
MULTI_STEP_PATTERN = re.compile(r"\b(step by step|first.*then|and then|after that)\b", re.IGNORECASE)
SIMPLE_GREETING_PATTERN = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|sure|yes|no|cool)\b[\s!.,]*$",
    re.IGNORECASE,
)

WEIGHTS = {
    "length": 0.25,
    "code": 0.25,
    "math": 0.15,
    "reasoning_terms": 0.20,
    "multi_step": 0.10,
    "question_marks": 0.05,
}


@dataclass
class ClassificationResult:
    complexity: Complexity
    score: float  # 0.0 (trivial) - 1.0 (very complex)
    features: dict


def extract_features(prompt: str) -> dict:
    length = len(prompt)
    word_count = len(prompt.split())

    return {
        "length_score": min(word_count / 200.0, 1.0),  # normalize, cap at 200 words
        "code_score": 1.0 if CODE_PATTERN.search(prompt) else 0.0,
        "math_score": 1.0 if MATH_PATTERN.search(prompt) else 0.0,
        "reasoning_score": min(len(REASONING_PATTERN.findall(prompt)) / 3.0, 1.0),
        "multi_step_score": 1.0 if MULTI_STEP_PATTERN.search(prompt) else 0.0,
        "question_marks": min(prompt.count("?") / 3.0, 1.0),
        "raw_length": length,
        "word_count": word_count,
    }


def classify(prompt: str) -> ClassificationResult:
    stripped = prompt.strip()

    if not stripped:
        return ClassificationResult(Complexity.simple, 0.0, {})

    if SIMPLE_GREETING_PATTERN.match(stripped) or len(stripped) < 12:
        return ClassificationResult(Complexity.simple, 0.02, {"reason": "trivial_greeting_or_short"})

    features = extract_features(stripped)

    score = (
        WEIGHTS["length"] * features["length_score"]
        + WEIGHTS["code"] * features["code_score"]
        + WEIGHTS["math"] * features["math_score"]
        + WEIGHTS["reasoning_terms"] * features["reasoning_score"]
        + WEIGHTS["multi_step"] * features["multi_step_score"]
        + WEIGHTS["question_marks"] * features["question_marks"]
    )
    score = round(min(max(score, 0.0), 1.0), 4)

    if score < 0.25:
        complexity = Complexity.simple
    elif score < 0.55:
        complexity = Complexity.medium
    else:
        complexity = Complexity.complex

    return ClassificationResult(complexity, score, features)
