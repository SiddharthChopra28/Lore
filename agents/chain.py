"""Thin web3 wrapper used by the gateway, verifier and curator, on P2's deployed contracts."""

import threading

from eth_account import Account
from web3 import Web3

import shared

# The events Lore's agents index. Contracts emit others too (role, ERC1155, params) that we ignore.
WATCHED = {
    "TopicGraph": ["TopicCreated", "NodeAdded", "NodeMetaSet", "TopicMetaSet"],
    "Submissions": ["Submitted", "Reported", "Voted", "Finalized"],
    "Access": ["Bought"],
    "Royalties": ["Distributed"],
    "Reports": ["LeakReported", "LeakMatched"],
    "PersonRegistry": ["AgentRegistered"],
}
STATUS = ("Pending", "Reported", "Accepted", "Rejected")  # ISubmissions.Status


def hexstr(b) -> str:
    return "0x" + bytes(b).hex()


class Chain:
    def __init__(self, rpc: str, contracts: dict[str, str], key: str | None = None, chain_id: int | None = None):
        self.chain_id = chain_id
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        self.c = {n: self.w3.eth.contract(address=Web3.to_checksum_address(a), abi=shared.abi(n))
                  for n, a in contracts.items()}
        self.acct = Account.from_key(key) if key else None
        self._ts, self._roles = {}, {}
        self._send_lock = threading.Lock()  # the gateway sends from its API thread and its worker thread

    @classmethod
    def for_chain(cls, chain_id: int, key: str | None = None, rpc: str | None = None) -> "Chain":
        dep = shared.deployment(chain_id)
        return cls(rpc or shared.network(chain_id).rpc_url, dep["contracts"], key, chain_id)

    @staticmethod
    def _a(addr: str) -> str:
        return Web3.to_checksum_address(addr)

    # --- reads (always live, never cached: BUILD_PLAN §4 C) ---
    def has_access(self, topic_id: int, addr: str) -> bool:
        return self.c["Access"].functions.hasAccess(topic_id, self._a(addr)).call()

    def contributor_since(self, topic_id: int, addr: str) -> int:
        return self.c["TopicGraph"].functions.contributorSince(topic_id, self._a(addr)).call()

    def has_role(self, role: str, addr: str) -> bool:
        """role: VERIFIER, CURATOR, LEAK_DETECTOR or ADMIN (P2's LoreRoles.<role>_ROLE)."""
        roles = self.c["LoreRoles"]
        if role not in self._roles:  # role ids are constants, safe to cache; membership is not
            self._roles[role] = getattr(roles.functions, f"{role}_ROLE")().call()
        return roles.functions.hasRole(self._roles[role], self._a(addr)).call()

    def can_vote(self, sub_id: int, addr: str) -> bool:
        return self.c["Submissions"].functions.canVote(sub_id, self._a(addr)).call()

    def can_finalize(self, sub_id: int) -> bool:
        return self.c["Submissions"].functions.canFinalize(sub_id).call()

    def voting_window(self) -> int:
        return self.c["Submissions"].functions.votingWindow().call()

    def submission_status(self, sub_id: int) -> str:
        return STATUS[self.c["Submissions"].functions.getSubmission(sub_id).call()[4]]

    def person_of(self, addr: str) -> str | None:
        """The World ID person (0x-hex nullifier) this agent is linked to, or None."""
        p = bytes(self.c["PersonRegistry"].functions.personOf(self._a(addr)).call())
        return None if p == bytes(32) else hexstr(p)

    def is_banned(self, addr: str) -> bool:
        """P2's PersonRegistry.isBanned: the wallet, or the person behind it, was removed for a proven leak."""
        return self.c["PersonRegistry"].functions.isBanned(self._a(addr)).call()

    def stake(self) -> int:
        return self.c["Submissions"].functions.stake().call()

    def claimable(self, node_id: int) -> int:
        return self.c["Royalties"].functions.claimable(node_id).call()

    def selector(self, contract: str, fn: str) -> str:
        from eth_utils import function_abi_to_4byte_selector

        entry = next(e for e in self.c[contract].abi if e.get("type") == "function" and e["name"] == fn)
        return "0x" + function_abi_to_4byte_selector(entry).hex()

    def balances(self, addr: str) -> dict:
        """Native 0G in wei: Lore is paid in 0G (stakes, topics) and 0G pays the gas."""
        return {"native_wei": self.w3.eth.get_balance(self._a(addr))}

    def quote_buy(self, topic_id: int) -> dict:
        """What buying `topic_id` costs: the base price only (Access.priceOf)."""
        base = self.topic_price(topic_id)
        return {"base_wei": base, "authors": [], "total_wei": base}

    def address(self, contract: str) -> str:
        return self.c[contract].address

    def prepare(self, contract: str, fn: str, sender: str, *args, value: int = 0) -> dict:
        """An unsigned transaction for `sender`; `value` is the 0G (wei) it sends (stakes, purchases)."""
        return {"chain_id": self.chain_id or self.w3.eth.chain_id, "from": self._a(sender),
                "to": self.c[contract].address, "value": value, "data": self.c[contract].encode_abi(fn, args=list(args)),
                "description": f"{contract}.{fn}"}

    def params(self) -> dict:
        """Live economic parameters (all settable by ADMIN), for the public explorer."""
        sub, acc, roy = self.c["Submissions"].functions, self.c["Access"].functions, self.c["Royalties"].functions
        return {"stake": sub.stake().call(), "stake_fee": sub.stakeFee().call(), "node_value": acc.nodeValue().call(),
                "platform_cut_bps": roy.platformCutBps().call(), "quorum_bps": sub.quorumBps().call(),
                "voting_window": sub.votingWindow().call()}

    def topic_price(self, topic_id: int) -> int:
        """The price, nodeValue x the topic's accepted nodes (Access.priceOf), live."""
        return self.c["Access"].functions.priceOf(topic_id).call()

    def latest(self) -> int:
        return self.w3.eth.block_number

    # --- writes ---
    def send(self, contract: str, fn: str, *args):
        with self._send_lock:  # one nonce at a time, counting txs still pending
            tx = getattr(self.c[contract].functions, fn)(*args).build_transaction(
                {"from": self.acct.address, "nonce": self.w3.eth.get_transaction_count(self.acct.address, "pending")}
            )
            h = self.w3.eth.send_raw_transaction(self.acct.sign_transaction(tx).raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h)
        if receipt.status != 1:
            raise RuntimeError(f"{contract}.{fn} reverted: {hexstr(h)}")
        return receipt

    # --- events ---
    def logs(self, from_block: int, to_block: int, only: set[str] | None = None):
        """Yield (block, log_index, event_name, args, block_timestamp) in chain order."""
        out = []
        for name, events in WATCHED.items():
            if name not in self.c:  # e.g. a deployment from before PersonRegistry existed
                continue
            for ev in events:
                if only and ev not in only:
                    continue
                for log in self.c[name].events[ev].get_logs(from_block=from_block, to_block=to_block):
                    out.append((log.blockNumber, log.logIndex, log.event, dict(log.args), self._time(log.blockNumber)))
        return sorted(out, key=lambda e: (e[0], e[1]))

    def _time(self, n: int) -> int:
        if n not in self._ts:
            self._ts[n] = self.w3.eth.get_block(n).timestamp
        return self._ts[n]
