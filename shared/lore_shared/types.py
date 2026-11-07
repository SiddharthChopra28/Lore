"""Frozen off-chain record shapes from BUILD_PLAN.md section 3.3."""

from typing import Literal, NotRequired, TypedDict


class InferenceReceipt(TypedDict):
    model: str
    provider: str
    chat_id: str
    trust_mode: Literal["private"]
    tee_verified: bool
    signed_text: str
    signature: str
    signer: str
    signer_matches_onchain: bool
    request_hash: str
    response_hash: str
    prompt_version: str
    created_at: str


class Similarity(TypedDict):
    node_id: NotRequired[str]
    sub_id: NotRequired[str]
    score: float


class Parent(TypedDict):
    node_id: str
    score: float


class VerifierReport(TypedDict):
    sub_id: str
    topic_id: str
    passed: bool
    checks: dict[str, bool]
    max_similarity: Similarity
    parents: list[Parent]
    reasons: list[str]
    receipts: list[str]
    created_at: str


class NodeMeta(TypedDict):
    node_id: str
    title: str
    description: str
    receipt: str
    created_at: str
    author: NotRequired[str]  # the contributing agent wallet


class TopicMeta(TypedDict):
    topic_id: str
    title: str
    description: str
    node_count: int
    receipt: str
    created_at: str


# Explicit migration shapes: existing stored reports use floating-point scores.
# Never reinterpret those old numbers as basis points when reading old content.
class QualityScores(TypedDict):
    novelty_bps: int
    correctness_bps: int
    usefulness_bps: int
    total_bps: int


class BpsSimilarity(TypedDict):
    node_id: NotRequired[str]
    sub_id: NotRequired[str]
    score: int


class BpsParent(TypedDict):
    node_id: str
    score: int


class PricingVerifierReport(TypedDict):
    """New pricing deployment only; scores and similarities use integer bps."""

    sub_id: str
    topic_id: str
    passed: bool
    checks: dict[str, bool]
    max_similarity: BpsSimilarity
    parents: list[BpsParent]
    scores: QualityScores
    reasons: list[str]
    receipts: list[str]
    verdict_receipt: str | None
    created_at: str


class PricingTopicMeta(TopicMeta):
    """Advisory initial price in USDC micro-units, never a chain price input."""

    suggested_p0: int
