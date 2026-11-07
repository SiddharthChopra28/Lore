"""Test doubles for the chain and for P3's content/storage libraries."""

from eth_utils import keccak
from web3 import Web3


class FakeChain:
    def __init__(self):
        self.access, self.since, self.roles, self.sent, self.events = set(), {}, set(), [], []
        self.persons = {}  # agent (lowercase) -> World ID person
        self.w3 = Web3
        self.chain_id = 16602

    @staticmethod
    def _a(addr):
        return Web3.to_checksum_address(addr)

    def params(self):
        return {"stake": self.stake_amount, "stake_fee": 500000, "node_value": self.price, "platform_cut_bps": 1000,
                "quorum_bps": 5000, "voting_window": self.window}

    def has_access(self, topic_id, addr):
        return (topic_id, addr.lower()) in self.access

    def contributor_since(self, topic_id, addr):
        return self.since.get((topic_id, addr.lower()), 0)

    def has_role(self, role, addr):
        return (role, addr.lower()) in self.roles

    def send(self, contract, fn, *args):
        self.sent.append((contract, fn, *args))

    # P2's on-chain views. Tests set these directly; real rules are covered by P2's Foundry tests.
    window, price, votable, finalizable_ids, statuses = 24 * 3600, 5_000000, None, set(), {}

    def can_vote(self, sub_id, addr):
        if self.votable is not None:
            return (sub_id, addr.lower()) in self.votable
        return self.contributor_since(1, addr) > 0  # default: any contributor of topic 1 (the tests' topic)

    def can_finalize(self, sub_id):
        return sub_id in self.finalizable_ids

    def voting_window(self):
        return self.window

    def topic_price(self, topic_id=None):
        return self.price

    stake_amount, claimable_by_node = 10_000000, {}

    banned: set = set()

    def is_banned(self, addr):
        return addr.lower() in self.banned

    def stake(self):
        return self.stake_amount

    def claimable(self, node_id):
        return self.claimable_by_node.get(node_id, 0)

    def address(self, contract):
        return "0x" + contract.encode().hex()[:40].ljust(40, "0")

    def prepare(self, contract, fn, sender, *args, value=0):
        return {"from": sender, "to": self.address(contract), "description": f"{contract}.{fn}", "args": list(args),
                "value": value}

    def quote_buy(self, topic_id):
        return {"base_wei": self.price, "authors": [], "total_wei": self.price}

    chain_id = 16602

    def selector(self, contract, fn):
        return "0x" + (contract + fn).encode().hex()[:8]

    def balances(self, addr):
        return {"native_wei": 0}

    def person_of(self, addr):
        return self.persons.get(addr.lower())

    def submission_status(self, sub_id):
        return self.statuses.get(sub_id, "Pending")

    # events: list of (block, log_index, name, args, timestamp)
    events: list = []
    head = 0

    def latest(self):
        return self.head

    def logs(self, frm, to, only=None):
        return [e for e in self.events if frm <= e[0] <= to and (not only or e[2] in only)]


class FakeContent:
    """NOT encryption: a visible stand-in for P3's crypto/watermark in tests only."""

    def content_hash(self, text):
        return "0x" + keccak(text=text).hex()

    def encrypt_node(self, text, ch):
        return b"ENC:" + ch.encode() + b":" + text.encode()

    def decrypt_node(self, blob, ch):
        prefix = b"ENC:" + ch.encode() + b":"
        assert blob.startswith(prefix), "wrong key"
        return blob[len(prefix):].decode()

    def watermark(self, text, wallet, ref):
        return f"{text}[wm:{wallet}:{ref}]"

    def validate_plaintext(self, text):
        if "\u2063" in text or "\u200b" in text or "\u200c" in text:
            raise ValueError("plaintext contains reserved watermark characters")


class FakeStorage:
    def __init__(self):
        self.blobs = {}

    def upload(self, data: bytes) -> str:
        root = "0x" + keccak(data).hex()
        self.blobs[root] = data
        return root

    def download(self, root: str) -> bytes:
        return self.blobs[root]
