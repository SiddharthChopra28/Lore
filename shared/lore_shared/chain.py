"""Read-only contract bindings using P2's packaged ABI/deployment artifacts."""

from importlib.resources import files
import json

from web3 import Web3

from .network import get_network


def load_deployment(chain_id: int) -> dict:
    return json.loads(files("lore_shared").joinpath(f"deployments/{chain_id}.json").read_text())


def load_abi(name: str) -> list:
    if name not in {"Access", "LoreRoles", "Reports", "Royalties", "Submissions", "TopicGraph", "PersonRegistry"}:
        raise ValueError("unknown Lore contract")
    return json.loads(files("lore_shared").joinpath(f"abi/{name}.json").read_text())


class ChainReader:
    """No signer or transaction methods: scenario writes belong to local MCP."""

    def __init__(self, chain_id: int = 16602, *, rpc_url: str | None = None, web3=None):
        self.chain_id = chain_id
        self.deployment = load_deployment(chain_id)
        self.web3 = web3 or Web3(Web3.HTTPProvider(rpc_url or get_network(chain_id).rpc_url, request_kwargs={"timeout": 20}))
        self.contracts = {
            name: self.web3.eth.contract(address=Web3.to_checksum_address(address), abi=load_abi(name))
            for name, address in self.deployment["contracts"].items()
        }

    def call(self, contract: str, method: str, *args):
        return getattr(self.contracts[contract].functions, method)(*args).call()

    def record(self, contract: str, method: str, *args) -> dict:
        abi = next(item for item in self.contracts[contract].abi if item.get("type") == "function" and item.get("name") == method)
        fields = abi["outputs"][0]["components"]
        return dict(zip((item["name"] for item in fields), self.call(contract, method, *args), strict=True))

    def validate(self) -> dict:
        if self.web3.eth.chain_id != self.chain_id:
            raise ValueError("RPC chain ID does not match deployment")
        addresses = self.deployment["contracts"]
        for name, address in addresses.items():
            if not self.web3.eth.get_code(Web3.to_checksum_address(address)):
                raise ValueError(f"missing deployed code: {name}")
        wiring = [
            ("Access", "graph", "TopicGraph"), ("Access", "royalties", "Royalties"),
            ("Submissions", "graph", "TopicGraph"), ("Royalties", "graph", "TopicGraph"),
            ("Royalties", "access", "Access"), ("Reports", "graph", "TopicGraph"),
            ("TopicGraph", "registry", "PersonRegistry"), ("PersonRegistry", "roles", "LoreRoles"),
        ]
        for contract, method, target in wiring:
            if self.call(contract, method).lower() != addresses[target].lower():
                raise ValueError(f"incorrect wiring: {contract}.{method}")
        return {
            "chain_id": self.chain_id, "block": self.web3.eth.block_number,
            "deploy_block": self.deployment["deployBlock"], "contracts_with_code": sorted(addresses),
            "wiring_checked": len(wiring),
            "voting_window": self.call("Submissions", "votingWindow"),
            "stake": self.call("Submissions", "stake"),
            "stake_fee": self.call("Submissions", "stakeFee"),
            "node_value": self.call("Access", "nodeValue"),
            "topic_count": self.call("TopicGraph", "topicCount"),
            "submission_count": self.call("Submissions", "submissionCount"),
            "treasury": self.call("LoreRoles", "treasury"),
        }
