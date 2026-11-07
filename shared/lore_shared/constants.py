"""Configurable v1 placeholders, mirrored from contracts/src/Constants.sol; amounts are native 0G in wei."""

from dataclasses import dataclass, fields
from collections.abc import Mapping

STAKE = 10**17  # 0.1 0G
STAKE_FEE = 5 * 10**15  # 0.005 0G
NODE_VALUE = 10**16  # price per node (Access.priceOf)
PLATFORM_CUT_BPS = 1_000
SLASH_TO_VOTERS_BPS = 5_000
VOTING_WINDOW = 24 * 60 * 60
QUORUM_BPS = 5_000
MIN_CHARS = 500
MAX_CHARS = 50_000
DUP_SIMILARITY = 0.92
EDGE_SIMILARITY = 0.75
MAX_PARENTS = 5


@dataclass(frozen=True)
class Constants:
    """Override at application startup to match the deployed contracts."""

    STAKE: int = STAKE
    STAKE_FEE: int = STAKE_FEE
    NODE_VALUE: int = NODE_VALUE
    PLATFORM_CUT_BPS: int = PLATFORM_CUT_BPS
    SLASH_TO_VOTERS_BPS: int = SLASH_TO_VOTERS_BPS
    VOTING_WINDOW: int = VOTING_WINDOW
    QUORUM_BPS: int = QUORUM_BPS
    MIN_CHARS: int = MIN_CHARS
    MAX_CHARS: int = MAX_CHARS
    DUP_SIMILARITY: float = DUP_SIMILARITY
    EDGE_SIMILARITY: float = EDGE_SIMILARITY
    MAX_PARENTS: int = MAX_PARENTS


@dataclass(frozen=True)
class PricingParameters:
    """Dynamic-model parameters, opt-in until P2 deploys the new contracts.

    Amounts are USDC micro-units; durations are seconds. Existing ``Constants``
    remain the fixed-price deployment's defaults. Call ``assert_matches`` with
    a complete on-chain snapshot before using this model for user quotes.
    """

    P0_MIN: int = 1_000_000
    P0_DEFAULT: int = 5_000_000
    P0_MAX: int = 50_000_000
    NODE_STEP_BPS: int = 1_000
    DEMAND_STEP_BPS: int = 1_000
    PRICE_CAP_MULT: int = 20
    PRICE_TICK: int = 10_000
    EPOCH: int = 5 * 24 * 60 * 60
    EPOCHS: int = 6
    PLATFORM_CUT_BPS: int = 2_000
    SUBMIT_FEE: int = 1_000_000
    STAKE_MIN: int = 2_000_000
    STAKE_PRICE_BPS: int = 1_000
    VOTER_FEE_SHARE_BPS: int = 0
    SLASH_TO_VOTERS_BPS: int = 5_000
    QUORUM_BPS: int = 5_000
    OVERTURN_BPS: int = 6_667
    VOTING_WINDOW: int = 24 * 60 * 60
    ACCEPT_SCORE_BPS: int = 5_000
    ROOT_QF_CAP_BPS: int = 12_000
    BASE_WEIGHT: int = 10_000
    REUSE_BONUS: int = 3_000
    MAX_BONUS_MULT: int = 2
    DUP_SIMILARITY_BPS: int = 9_200
    EDGE_SIMILARITY_BPS: int = 7_500
    NOVELTY_FLOOR_SIM_BPS: int = 5_000
    MAX_PARENTS: int = 5
    LEAK_MIN_SCORE_BPS: int = 9_000
    REVOKE_DELAY: int = 72 * 60 * 60
    RECOVERY_DELAY: int = 7 * 24 * 60 * 60

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 <= value < 2**256:
                raise ValueError(f"{field.name} must be a uint256 integer")
            if field.name.endswith("_BPS") and value > 10_000:
                # QF is a multiplier and may exceed 100%.
                if field.name != "ROOT_QF_CAP_BPS":
                    raise ValueError(f"{field.name} must be at most 10000")
        if not 0 < self.P0_MIN <= self.P0_DEFAULT <= self.P0_MAX:
            raise ValueError("initial price bounds must contain the default")
        if not 0 < self.PRICE_TICK <= self.P0_MIN:
            raise ValueError("price tick must be positive and no greater than P0_MIN")
        if not self.PRICE_CAP_MULT or self.P0_MAX * self.PRICE_CAP_MULT >= 2**256:
            raise ValueError("price cap must be positive and fit uint256")
        if not self.EPOCH or not 1 <= self.EPOCHS <= 6:
            raise ValueError("demand window requires a positive epoch and 1..6 buckets")
        if not self.NOVELTY_FLOOR_SIM_BPS < self.DUP_SIMILARITY_BPS:
            raise ValueError("novelty floor must be below the duplicate threshold")
        if not 5_000 <= self.ROOT_QF_CAP_BPS <= 15_000:
            raise ValueError("root quality cap must be between 5000 and 15000")
        if not self.BASE_WEIGHT or not 1 <= self.MAX_PARENTS <= 5:
            raise ValueError("base weight must be positive and parent bound must be 1..5")

    def assert_matches(self, live: Mapping[str, int]) -> None:
        """Fail closed on missing, mistyped or drifted values; never use defaults.

        The caller maps P2's eventual getter names to these parameter names and
        reads them at one block. This method makes no assumptions about that ABI.
        """
        mismatched = [field.name for field in fields(self)
                      if type(live.get(field.name)) is not int
                      or live[field.name] != getattr(self, field.name)]
        if mismatched:
            raise ValueError("pricing parameter mismatch: " + ", ".join(mismatched))
