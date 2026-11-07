"""Turnkey delegated-access wallets: requests, stamps, policies, signing and sending."""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from eth_account import Account
from eth_utils import keccak
from hexbytes import HexBytes
from web3.exceptions import TimeExhausted

from gateway.service import LoreError, Service
from gateway.signer import Signer
from gateway.turnkey import MAX_GAS, MAX_VALUE_WEI, RETRY_WINDOW_MS, Turnkey, TurnkeyError, lore_policies, unsigned_eip1559
from tests.fakes import FakeChain, FakeContent, FakeStorage

USER = "0x" + "ab" * 20
TX = {"chainId": 16602, "nonce": 7, "maxPriorityFeePerGas": 1, "maxFeePerGas": 3, "gas": 50_000,
      "to": "0x" + "11" * 20, "value": 0, "data": "0x6a761202", "accessList": []}


def p256():
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint).hex()
    return pub, format(key.private_numbers().private_value, "064x")


PARENT, DELEGATED = p256(), p256()


def verify_stamp(req: httpx.Request) -> str:
    """The X-Stamp must be a valid P-256 signature over the exact body. Returns the signer's public key."""
    stamp = json.loads(base64.urlsafe_b64decode(req.headers["X-Stamp"] + "=="))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(stamp["publicKey"]))
    pub.verify(bytes.fromhex(stamp["signature"]), req.content, ec.ECDSA(hashes.SHA256()))
    return stamp["publicKey"]


def turnkey(handler, seen):
    def h(req):
        body = json.loads(req.content)
        seen.append((req.url.path, verify_stamp(req), body))
        return handler(req.url.path, body)

    return Turnkey("org-parent", PARENT, DELEGATED, http=httpx.Client(base_url="https://api.turnkey.com",
                   transport=httpx.MockTransport(h)), sleep=lambda s: None)


def done(result, aid="a1"):
    return httpx.Response(200, json={"activity": {"id": aid, "status": "ACTIVITY_STATUS_COMPLETED", "result": result}})


def test_unsigned_encoding_is_what_ethereum_signs():
    raw = bytes.fromhex(unsigned_eip1559(TX))
    acct = Account.create()
    signed = acct.sign_transaction({**TX, "type": 2})  # eth-account's own EIP-1559 signing
    ours = Account.unsafe_sign_hash(keccak(raw), acct.key)  # signing our bytes' hash with the same key
    assert raw[0] == 2 and (ours.r, ours.s) == (signed.r, signed.s)  # deterministic: same bytes, same signature


def test_create_wallet_runs_the_delegated_access_setup_in_order_and_replays_exactly():
    seen, pending = [], {"left": 1}

    def handler(path, body):
        if path.endswith("create_sub_organization"):  # root ids in the "wrong" order: Lore must not rely on it
            return done({"createSubOrganizationResultV7": {"subOrganizationId": "sub-1", "rootUserIds": ["u-user", "u-lore"],
                                                           "wallet": {"walletId": "w", "addresses": [USER]}}})
        if path.endswith("whoami"):
            assert body == {"organizationId": "sub-1"}
            return httpx.Response(200, json={"organizationId": "sub-1", "userId": "u-lore"})
        if path.endswith("create_policy") and pending["left"]:  # one activity is still pending at first
            pending["left"] -= 1
            return httpx.Response(200, json={"activity": {"id": "p1", "status": "ACTIVITY_STATUS_PENDING"}})
        return done({})

    tk = turnkey(handler, seen)
    policies = lambda uid: [{"policyName": uid}, {"policyName": "second"}]  # noqa: E731
    sub, addr = tk.create_wallet("0x" + "cd" * 32, "me@example.com", policies, ms=1_000)
    assert (sub, addr) == ("sub-1", USER)
    paths = [p for p, _, _ in seen]
    assert paths == ["/public/v1/submit/create_sub_organization", "/public/v1/query/whoami",
                     "/public/v1/submit/create_policy", "/public/v1/query/get_activity",
                     "/public/v1/submit/create_policy", "/public/v1/submit/update_root_quorum"]
    who = [k for _, k, _ in seen]
    assert who[0] == PARENT[0] and set(who[1:]) == {DELEGATED[0]}  # parent creates; delegated user does the rest
    create, policy, quorum = seen[0][2], seen[2][2], seen[5][2]
    assert create["organizationId"] == "org-parent" and policy["organizationId"] == quorum["organizationId"] == "sub-1"
    assert create["parameters"]["rootUsers"][0]["apiKeys"][0]["publicKey"] == DELEGATED[0]
    assert create["parameters"]["rootUsers"][1]["userEmail"] == "me@example.com"
    assert policy["parameters"] == {"policyName": "u-lore"}
    assert quorum["parameters"] == {"threshold": 1, "userIds": ["u-user"]}  # Lore leaves the root quorum
    submits = [b for p, _, b in seen if "/submit/" in p]
    assert [b["timestampMs"] for b in submits] == ["1000", "1001", "1002", "1003"]  # all derived from ms

    # A retry with the same ms resends byte-identical bodies, which Turnkey answers with the same activities.
    again = []
    turnkey(handler, again).create_wallet("0x" + "cd" * 32, "me@example.com", policies, ms=1_000)
    assert [b for p, _, b in again if "/submit/" in p] == submits


def test_policies_pin_chain_contracts_selectors_and_value():
    contracts = {n: "0x" + c * 40 for n, c in zip(("Submissions", "Access", "Royalties", "Reports"), "abcd")}
    sels = {"Submissions.submit": "0x11111111", "Submissions.vote": "0x22222222", "Access.buy": "0x33333333",
            "Royalties.claim": "0x44444444", "Reports.report": "0x55555555"}
    ps = {p["policyName"]: p["condition"] for p in lore_policies("u-lore", 16602, contracts, sels)}
    assert len(ps) == 5 and all(c.startswith(f"eth.tx.chain_id == 16602 && eth.tx.gas <= {MAX_GAS} && ")
                                for c in ps.values())
    for call, cond in ps.items():  # 0G leaves the wallet only in a purchase or a stake, capped
        paying = call in ("Lore: Access.buy", "Lore: Submissions.submit")
        assert (f"eth.tx.value <= {MAX_VALUE_WEI}" in cond) == paying and ("eth.tx.value == 0" in cond) != paying
    assert "eth.tx.to == '0x" + "b" * 40 + "'" in ps["Lore: Access.buy"] and "0x33333333" in ps["Lore: Access.buy"]


def test_sign_transaction_returns_the_signed_raw_tx_and_surfaces_denials():
    seen = []
    tk = turnkey(lambda p, b: done({"signTransactionResult": {"signedTransaction": "02f8abcd"}}), seen)
    assert tk.sign_transaction("sub-1", USER, "02e3") == "0x02f8abcd"
    params = seen[0][2]["parameters"]
    assert params == {"signWith": USER, "unsignedTransaction": "02e3", "type": "TRANSACTION_TYPE_ETHEREUM"}
    denied = turnkey(lambda p, b: httpx.Response(200, json={"activity": {"id": "x", "status": "ACTIVITY_STATUS_FAILED",
                                                                        "failure": "policy denied"}}), [])
    with pytest.raises(TurnkeyError, match="policy denied"):
        denied.sign_transaction("sub-1", USER, "02e3")


class FakeW3:
    def __init__(self, status=1, estimate_error=None, gas=100_000, block=None, timeout=False):
        self.eth, self.status, self.err, self.sent, self.gas = self, status, estimate_error, [], gas
        self.max_priority_fee, self.gas_price, self.block, self.timeout = 2, 7, {"baseFeePerGas": 10} if block is None else block, timeout

    def estimate_gas(self, call):
        if self.err:
            raise self.err
        return self.gas

    def get_transaction_count(self, addr, block):
        return 5

    def get_block(self, which):
        return self.block

    def send_raw_transaction(self, raw):
        self.sent.append(raw)
        return HexBytes("0x" + "aa" * 32)

    def wait_for_transaction_receipt(self, h):
        if self.timeout:
            raise TimeExhausted("not mined")
        return {"status": self.status}


class FakeTurnkey:
    def __init__(self, fail_creates=0, deny=False):
        self.signed, self.created, self.fail_creates, self.deny = [], [], fail_creates, deny

    def sign_transaction(self, sub, address, unsigned):
        if self.deny:
            raise TurnkeyError("ACTIVITY_STATUS_FAILED: policy denied")
        self.signed.append((sub, address, unsigned))
        return "0xsigned"

    def create_wallet(self, person, email, policies, ms):
        self.created.append((person, email, policies("u-lore"), ms))
        if self.fail_creates:
            self.fail_creates -= 1
            raise TurnkeyError("update_root_quorum timed out")
        return "sub-1", "0x" + "EE" * 20


def signer(w3, wallets=lambda a: "sub-1" if a == USER else None, tk=None):
    chain = FakeChain()
    chain.w3 = w3
    tk = tk or FakeTurnkey()
    return Signer(chain, tk, wallets), tk


PREPARED = {"chain_id": 16602, "from": USER, "to": "0x" + "11" * 20, "value": 0, "data": "0x6a761202", "description": "Access.buy"}


def test_signer_builds_signs_with_turnkey_and_broadcasts():
    w3 = FakeW3()
    s, tk = signer(w3)
    assert s.send(PREPARED) == "0x" + "aa" * 32
    sub, addr, unsigned = tk.signed[0]
    expected = {"chainId": 16602, "nonce": 5, "to": PREPARED["to"], "value": 0, "data": PREPARED["data"], "gas": 120_000,
                "maxPriorityFeePerGas": 2, "maxFeePerGas": 22, "accessList": []}
    assert (sub, addr, unsigned) == ("sub-1", USER, unsigned_eip1559(expected)) and w3.sent == ["0xsigned"]


def test_signer_refuses_unmanaged_wallets_and_reports_reverts_and_missing_funds():
    s, _ = signer(FakeW3(), wallets=lambda a: None)
    with pytest.raises(LoreError, match="isn't a Lore-managed wallet"):
        s.send(PREPARED)
    with pytest.raises(LoreError, match="reverted"):
        signer(FakeW3(status=0))[0].send(PREPARED)
    with pytest.raises(LoreError, match="funded with enough 0G"):
        signer(FakeW3(estimate_error=ValueError("insufficient funds")))[0].send(PREPARED)


def test_signer_caps_gas_and_falls_back_to_gas_price_without_a_base_fee():
    w3 = FakeW3(gas=MAX_GAS - 1, block={})  # 1.2x would pass the cap; no baseFeePerGas in the block
    s, tk = signer(w3)
    s.send(PREPARED)
    expected = {"chainId": 16602, "nonce": 5, "to": PREPARED["to"], "value": 0, "data": PREPARED["data"], "gas": MAX_GAS,
                "maxPriorityFeePerGas": 2, "maxFeePerGas": 7 * 2 + 2, "accessList": []}
    assert tk.signed[0][2] == unsigned_eip1559(expected)
    with pytest.raises(LoreError, match="more than Lore's wallet policy allows"):
        signer(FakeW3(gas=MAX_GAS + 1))[0].send(PREPARED)


def test_signer_turns_every_failure_into_a_user_facing_error_and_keeps_sent_hashes():
    with pytest.raises(LoreError, match="Turnkey didn't sign Access.buy: .*policy denied"):
        signer(FakeW3(), tk=FakeTurnkey(deny=True))[0].send(PREPARED)
    with pytest.raises(LoreError, match="was sent \\(tx 0xaaaa.*isn't mined yet"):
        signer(FakeW3(timeout=True))[0].send(PREPARED)
    w3 = FakeW3()
    s, _ = signer(w3)
    second = {**PREPARED, "description": "Submissions.submit"}
    w3_status = iter([1, 0])
    w3.wait_for_transaction_receipt = lambda h: {"status": next(w3_status)}
    with pytest.raises(LoreError, match="Submissions.submit reverted.*already sent: 0xaaaa"):
        s.send_all([PREPARED, second])


def service(tmp_path, tk, now=lambda: 1_000):
    return Service(str(tmp_path / "g.db"), FakeChain(), FakeContent(), FakeStorage(), now, turnkey=tk)


def test_create_wallet_is_idempotent_and_links_the_wallet_on_chain(tmp_path):
    tk = FakeTurnkey()
    svc = service(tmp_path, tk)
    chain = svc.chain
    person = "0x" + "cd" * 32
    with pytest.raises(LoreError, match="email"):
        svc.create_wallet(person, "not-an-email")
    out = svc.create_wallet(person, "me@example.com")
    assert out["address"] == "0x" + "ee" * 20 and svc.managed_sub_org(out["address"]) == "sub-1"
    assert svc.whoami(person)["wallets"] == [out["address"]]  # usable at once, before the indexer sees the event
    assert chain.sent[-1][:2] == ("PersonRegistry", "register") and chain.sent[-1][3] == bytes.fromhex(person[2:])
    policies = tk.created[0][2]
    assert {p["policyName"] for p in policies} >= {"Lore: Access.buy", "Lore: Submissions.vote"}
    chain.persons[out["address"]] = person  # the registration landed
    svc.create_wallet(person, "me@example.com")
    assert len(tk.created) == 1 and len(chain.sent) == 1  # nothing created or sent twice


def test_a_failed_setup_is_not_usable_and_a_retry_replays_the_same_attempt(tmp_path):
    clock = {"now": 1_000}
    tk = FakeTurnkey(fail_creates=1)
    svc = service(tmp_path, tk, now=lambda: clock["now"])
    person = "0x" + "cd" * 32
    with pytest.raises(LoreError, match="call lore_create_wallet again"):
        svc.create_wallet(person, "me@example.com")
    assert svc.wallet_info(person) == {"wallets": []} and svc.chain.sent == []  # nothing usable, nothing linked
    clock["now"] += 60
    out = svc.create_wallet(person, "typo@example.com")  # a retry replays the first attempt exactly
    assert [c[1] for c in tk.created] == ["me@example.com"] * 2 and [c[3] for c in tk.created] == [1_000_000] * 2
    assert svc.managed_sub_org(out["address"]) == "sub-1"


def test_a_setup_older_than_turnkeys_replay_window_is_not_retried(tmp_path):
    clock = {"now": 1_000}
    tk = FakeTurnkey(fail_creates=1)
    svc = service(tmp_path, tk, now=lambda: clock["now"])
    person = "0x" + "cd" * 32
    with pytest.raises(LoreError):
        svc.create_wallet(person, "me@example.com")
    clock["now"] += RETRY_WINDOW_MS // 1000 + 1
    with pytest.raises(LoreError, match="ask Lore's team"):
        svc.create_wallet(person, "me@example.com")
    assert len(tk.created) == 1  # never a fresh request that would make a second sub-org


def test_losing_the_registration_race_is_not_an_error(tmp_path):
    svc = service(tmp_path, FakeTurnkey())
    person = "0x" + "cd" * 32

    def register(*args):  # another request linked it first, so ours reverts with AlreadyRegistered
        svc.chain.persons["0x" + "ee" * 20] = person
        raise RuntimeError("PersonRegistry.register reverted")

    svc.chain.send = register
    assert svc.create_wallet(person, "me@example.com")["address"] == "0x" + "ee" * 20
