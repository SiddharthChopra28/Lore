"""P2's contract artifacts in shared/lore_shared: ABIs, deployments, network config.

ponytail: read by path; switch to a normal `lore_shared` dependency once P3 gives shared/ a pyproject.
"""

import importlib.util
import json
import os
from pathlib import Path

DIR = Path(os.environ.get("LORE_SHARED_DIR") or Path(__file__).resolve().parents[1] / "shared" / "lore_shared")


def abi(name: str) -> list:
    return json.loads((DIR / "abi" / f"{name}.json").read_text())


def deployment(chain_id: int) -> dict:
    """{"chainId", "contracts": {name: address}, "deployBlock", ...}"""
    return json.loads((DIR / "deployments" / f"{chain_id}.json").read_text())


def network(chain_id: int):
    spec = importlib.util.spec_from_file_location("lore_network", DIR / "network.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_network(chain_id)


def deployment_key(chain_id: int) -> str:
    """Identifies one deployment. Databases built for another deployment must not be reused."""
    d = deployment(chain_id)
    return f"{chain_id}:{d['contracts']['Submissions'].lower()}:{d['deployBlock']}"
