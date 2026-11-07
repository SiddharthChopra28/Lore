"""Users' wallets on Turnkey, with Lore holding delegated access (https://docs.turnkey.com, delegated access).

Each person gets a Turnkey sub-organization with one Ethereum wallet. Two root users are created: the user
(their email, so they can always reach and revoke it) and Lore's delegated key. Lore's delegated key then
grants itself narrow policies and leaves the root quorum, so afterwards:
  - the user is the only root: they can remove Lore's key or add a DENY policy at any time;
  - Lore's key can sign only Lore's own calls on Lore's chain (see lore_policies), checked inside Turnkey's
    enclave, so funds can only move into Lore's contracts. Within that, whoever holds Lore's key can spend the
    wallet on Lore actions (buy any topic, stake, vote): users should keep only what they mean to spend there.

Two API keys (P-256), both from Lore's Turnkey parent organization:
  TURNKEY_API_PUBLIC_KEY/PRIVATE_KEY        parent org: creates sub-organizations
  TURNKEY_DELEGATED_PUBLIC_KEY/PRIVATE_KEY  the delegated user inside every sub-organization: signs
"""

import json
import time

import httpx
import rlp
from eth_account._utils.transaction_utils import transaction_rpc_to_rlp_structure
from eth_account.typed_transactions import TypedTransaction
from eth_account.typed_transactions.dynamic_fee_transaction import DynamicFeeTransaction
from eth_utils.toolz import dissoc
from turnkey_api_key_stamper import ApiKeyStamper, ApiKeyStamperConfig

API = "https://api.turnkey.com"
DONE = "ACTIVITY_STATUS_COMPLETED"
FAILED = {"ACTIVITY_STATUS_FAILED", "ACTIVITY_STATUS_REJECTED"}
MAX_GAS = 5_000_000  # per transaction, in the policies and the Signer
# 0G a paying call (Access.buy, Submissions.submit) may carry; every other Lore call carries none.
# ponytail: one cap for both; Turnkey compared `eth.tx.value == 0` in spike T1, a large `<=` is not yet verified live.
MAX_VALUE_WEI = 10 * 10**18
PAYABLE = {"Access.buy", "Submissions.submit"}
# ponytail: no fee-per-gas cap yet (eth.tx has value and gas; a max_fee_per_gas field isn't confirmed). Fees go
# to validators, so a bad fee burns the user's 0G but pays no attacker. Add the cap if spike T1 finds the field.
RETRY_WINDOW_MS = 55 * 60 * 1000  # Turnkey accepts a timestampMs up to 1 h old; create_wallet's retries rely on it


class TurnkeyError(Exception):
    pass


def unsigned_eip1559(tx: dict) -> str:
    """0x02 || rlp([...]) of an unsigned EIP-1559 transaction, hex without 0x: what Turnkey signs
    (the same bytes @turnkey/ethers sends as `unsignedSerialized.substring(2)`)."""
    t = TypedTransaction.from_dict({**tx, "type": 2}).transaction
    fields = transaction_rpc_to_rlp_structure(dissoc(t.dictionary, "v", "r", "s"))
    return (bytes([2]) + rlp.encode(DynamicFeeTransaction._unsigned_transaction_serializer.from_dict(fields))).hex()


def lore_policies(delegated_user_id: str, chain_id: int, contracts: dict[str, str], selectors: dict[str, str]) -> list[dict]:
    """What Lore's delegated key may sign: only Lore's calls, only on Lore's chain.

    contracts: name -> address. selectors: "Contract.fn" -> 4-byte selector (0x-hex).
    Everything is paid in native 0G, so the only way value leaves the wallet is in a call to Lore's own contracts:
    Access.buy and Submissions.submit may carry up to MAX_VALUE_WEI, every other call none.
    """
    a = {k: v.lower() for k, v in contracts.items()}
    consensus = f"approvers.any(user, user.id == '{delegated_user_id}')"
    base = f"eth.tx.chain_id == {chain_id} && eth.tx.gas <= {MAX_GAS}"
    out = []
    for call, sel in selectors.items():
        value = f"eth.tx.value <= {MAX_VALUE_WEI}" if call in PAYABLE else "eth.tx.value == 0"
        out.append({"policyName": f"Lore: {call}", "effect": "EFFECT_ALLOW", "consensus": consensus,
                    "condition": f"{base} && {value} && eth.tx.to == '{a[call.split('.')[0]]}'"
                                 f" && eth.tx.data[0..10] == '{sel}'", "notes": "Lore delegated access"})
    return out


class Turnkey:
    def __init__(self, organization_id: str, api: tuple[str, str], delegated: tuple[str, str],
                 http: httpx.Client | None = None, sleep=time.sleep, now_ms=lambda: int(time.time() * 1000)):
        """api / delegated: (public key hex, private key hex)."""
        self.org = organization_id
        self.parent = ApiKeyStamper(ApiKeyStamperConfig(*api))
        self.delegated = ApiKeyStamper(ApiKeyStamperConfig(*delegated))
        self.delegated_public = delegated[0]
        self.http = http or httpx.Client(base_url=API, timeout=60)
        self.sleep, self.now_ms = sleep, now_ms

    def _post(self, stamper, path: str, body: dict) -> dict:
        payload = json.dumps(body, separators=(",", ":"))  # the stamp covers these exact bytes
        stamp = stamper.stamp(payload)
        r = self.http.post(path, content=payload, headers={"Content-Type": "application/json",
                                                             stamp.stamp_header_name: stamp.stamp_header_value})
        if r.status_code != 200:
            raise TurnkeyError(f"Turnkey {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _activity(self, stamper, path: str, type_: str, org: str, parameters: dict, timeout: float = 60,
                  ms: int | None = None) -> dict:
        """Submit an activity and wait until it completes. Returns activity.result.
        Turnkey fingerprints the body: resubmitting the same body (same `ms`) returns the activity it already ran."""
        act = self._post(stamper, path, {"type": type_, "timestampMs": str(self.now_ms() if ms is None else ms),
                                         "organizationId": org, "parameters": parameters})["activity"]
        waited = 0.0
        while act["status"] != DONE:
            if act["status"] in FAILED or waited >= timeout:
                raise TurnkeyError(f"{type_} {act['status']}: {act.get('failure') or 'timed out'}")
            self.sleep(1)
            waited += 1
            act = self._post(stamper, "/public/v1/query/get_activity",
                             {"organizationId": org, "activityId": act["id"]})["activity"]
        return act["result"]

    def create_wallet(self, person: str, email: str, policies, ms: int) -> tuple[str, str]:
        """New sub-org + Ethereum wallet for `person`. policies(delegated_user_id) -> list of policies.
        Returns (sub_organization_id, wallet_address).

        Retry-safe for RETRY_WINDOW_MS after `ms`: every step's timestampMs is derived from `ms`, so calling again
        with the same arguments returns the activities that already ran and finishes the rest. Never duplicates."""
        # Activity versions, result fields and the policy syntax were confirmed against the live API (spike T1).
        result = self._activity(self.parent, "/public/v1/submit/create_sub_organization",
                                "ACTIVITY_TYPE_CREATE_SUB_ORGANIZATION_V7", self.org, ms=ms, parameters={
            "subOrganizationName": f"lore-{person[2:18]}",
            "rootUsers": [
                {"userName": "Lore delegated access", "apiKeys": [{"apiKeyName": "lore-gateway",
                 "publicKey": self.delegated_public, "curveType": "API_KEY_CURVE_P256"}],
                 "authenticators": [], "oauthProviders": []},
                {"userName": "Lore user", "userEmail": email, "apiKeys": [], "authenticators": [], "oauthProviders": []},
            ],
            "rootQuorumThreshold": 1,
            "wallet": {"walletName": "Lore", "accounts": [{"curve": "CURVE_SECP256K1", "pathFormat": "PATH_FORMAT_BIP32",
                                                         "path": "m/44'/60'/0'/0/0", "addressFormat": "ADDRESS_FORMAT_ETHEREUM"}]},
        })["createSubOrganizationResultV7"]
        sub, address = result["subOrganizationId"], result["wallet"]["addresses"][0]
        # Which root is Lore's comes from Turnkey itself, not from the order of rootUserIds.
        delegated_id = self._post(self.delegated, "/public/v1/query/whoami", {"organizationId": sub})["userId"]
        user_ids = [u for u in result["rootUserIds"] if u != delegated_id]
        if len(user_ids) != 1:
            raise TurnkeyError(f"unexpected root users in {sub}: {result['rootUserIds']}")
        ps = policies(delegated_id)
        for i, policy in enumerate(ps, 1):
            self._activity(self.delegated, "/public/v1/submit/create_policy", "ACTIVITY_TYPE_CREATE_POLICY_V3", sub,
                           policy, ms=ms + i)
        # Last: Lore's key leaves the root quorum, so the user alone controls the sub-organization.
        self._activity(self.delegated, "/public/v1/submit/update_root_quorum", "ACTIVITY_TYPE_UPDATE_ROOT_QUORUM",
                       sub, {"threshold": 1, "userIds": user_ids}, ms=ms + len(ps) + 1)
        return sub, address

    def sign_transaction(self, sub_org: str, address: str, unsigned_hex: str) -> str:
        """Sign under the sub-org's policies (enforced by Turnkey). Returns the signed raw tx, 0x-hex."""
        result = self._activity(self.delegated, "/public/v1/submit/sign_transaction", "ACTIVITY_TYPE_SIGN_TRANSACTION_V2",
                                sub_org, {"signWith": address, "unsignedTransaction": unsigned_hex,
                                          "type": "TRANSACTION_TYPE_ETHEREUM"})
        return "0x" + result["signTransactionResult"]["signedTransaction"].removeprefix("0x")
