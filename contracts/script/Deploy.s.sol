// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {LoreRoles} from "../src/LoreRoles.sol";
import {TopicGraph} from "../src/TopicGraph.sol";
import {Submissions} from "../src/Submissions.sol";
import {Access} from "../src/Access.sol";
import {Royalties} from "../src/Royalties.sol";
import {Reports} from "../src/Reports.sol";
import {PersonRegistry} from "../src/PersonRegistry.sol";

/// @notice Deploys every Lore contract, wires them, grants roles and writes
///         shared/lore_shared/deployments/<chainId>.json. Every amount is native 0G.
///
/// Env:
///   DEPLOYER_PRIVATE_KEY   required, except on anvil (31337), which always uses anvil account #0
///   TREASURY               default: deployer
///   VERIFIER, CURATOR      role wallets of the verifier and curator agents (default: deployer)
///   LEAK_DETECTOR          the gateway agent's wallet; it calls recordMatch (default: deployer)
///   REGISTRAR              the gateway agent's wallet; it records agent -> person (default: deployer)
///
/// Then run `python script/export_abi.py` to refresh shared/lore_shared/abi/.
contract Deploy is Script {
    // Anvil's well-known account #0. Only ever used when chainid == 31337.
    uint256 internal constant ANVIL_KEY_0 = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    struct Deployed {
        LoreRoles roles;
        TopicGraph graph;
        Royalties royalties;
        Submissions subs;
        Access access;
        Reports reports;
        PersonRegistry registry;
    }

    struct Roles {
        address admin;
        address treasury;
        address verifier;
        address curator;
        address detector;
        address registrar;
    }

    function run() external returns (Deployed memory d) {
        uint256 pk = block.chainid == 31337 ? ANVIL_KEY_0 : vm.envUint("DEPLOYER_PRIVATE_KEY");
        Roles memory r;
        r.admin = vm.addr(pk);
        r.treasury = vm.envOr("TREASURY", r.admin);
        r.verifier = vm.envOr("VERIFIER", r.admin);
        r.curator = vm.envOr("CURATOR", r.admin);
        r.detector = vm.envOr("LEAK_DETECTOR", r.admin);
        r.registrar = vm.envOr("REGISTRAR", r.admin);
        uint256 deployBlock = block.number;

        vm.startBroadcast(pk);
        d.roles = new LoreRoles(r.admin, r.treasury);
        d.graph = new TopicGraph(d.roles);
        d.royalties = new Royalties(d.roles, d.graph);
        d.subs = new Submissions(d.roles, d.graph);
        d.access = new Access(d.roles, d.graph, d.royalties);
        d.reports = new Reports(d.roles, d.graph);
        d.registry = new PersonRegistry(d.roles);

        d.graph.setSubmissions(address(d.subs));
        d.graph.setRoyalties(address(d.royalties));
        d.graph.setRegistry(address(d.registry));
        d.royalties.setAccess(address(d.access));
        d.roles.grantRole(d.roles.VERIFIER_ROLE(), r.verifier);
        d.roles.grantRole(d.roles.CURATOR_ROLE(), r.curator);
        d.roles.grantRole(d.roles.LEAK_DETECTOR_ROLE(), r.detector);
        d.roles.grantRole(d.roles.REGISTRAR_ROLE(), r.registrar);
        vm.stopBroadcast();

        _write(d, r, deployBlock);
    }

    function _write(Deployed memory d, Roles memory roles, uint256 deployBlock) internal {
        string memory c = "contracts";
        vm.serializeAddress(c, "LoreRoles", address(d.roles));
        vm.serializeAddress(c, "TopicGraph", address(d.graph));
        vm.serializeAddress(c, "Royalties", address(d.royalties));
        vm.serializeAddress(c, "Submissions", address(d.subs));
        vm.serializeAddress(c, "Access", address(d.access));
        vm.serializeAddress(c, "PersonRegistry", address(d.registry));
        string memory contractsJson = vm.serializeAddress(c, "Reports", address(d.reports));

        string memory r = "roles";
        vm.serializeAddress(r, "admin", roles.admin);
        vm.serializeAddress(r, "treasury", roles.treasury);
        vm.serializeAddress(r, "verifier", roles.verifier);
        vm.serializeAddress(r, "curator", roles.curator);
        vm.serializeAddress(r, "leakDetector", roles.detector);
        string memory rolesJson = vm.serializeAddress(r, "registrar", roles.registrar);

        string memory o = "out";
        vm.serializeUint(o, "chainId", block.chainid);
        vm.serializeUint(o, "deployBlock", deployBlock);
        vm.serializeString(o, "roles", rolesJson);
        string memory json = vm.serializeString(o, "contracts", contractsJson);

        string memory path =
            string.concat(vm.projectRoot(), "/../shared/lore_shared/deployments/", vm.toString(block.chainid), ".json");
        vm.writeJson(json, path);
        console2.log("Wrote", path);
    }
}
