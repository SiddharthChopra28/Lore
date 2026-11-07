"""Copy each Lore contract's ABI from Foundry's out/ to shared/lore_shared/abi/<Name>.json.

Run from contracts/ after `forge build`:  python script/export_abi.py
Standard library only.
"""

import json
from pathlib import Path

CONTRACTS = ["LoreRoles", "TopicGraph", "Submissions", "Access", "Royalties", "Reports", "PersonRegistry"]

root = Path(__file__).resolve().parent.parent
out_dir = root.parent / "shared" / "lore_shared" / "abi"
out_dir.mkdir(parents=True, exist_ok=True)

for name in CONTRACTS:
    artifact = json.loads((root / "out" / f"{name}.sol" / f"{name}.json").read_text(encoding="utf-8"))
    abi = artifact["abi"]
    (out_dir / f"{name}.json").write_text(json.dumps(abi, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"abi/{name}.json ({len(abi)} entries)")
