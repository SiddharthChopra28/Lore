// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {LoreRoles} from "../src/LoreRoles.sol";
import {TopicGraph} from "../src/TopicGraph.sol";
import {Submissions} from "../src/Submissions.sol";
import {Access} from "../src/Access.sol";
import {Royalties} from "../src/Royalties.sol";
import {Reports} from "../src/Reports.sol";
import {PersonRegistry} from "../src/PersonRegistry.sol";
import {ISubmissions} from "../src/interfaces/ILore.sol";
import {Constants} from "../src/Constants.sol";

/// @notice Full deployment wired the same way as script/Deploy.s.sol, plus flow helpers.
abstract contract Base is Test {
    LoreRoles roles;
    TopicGraph graph;
    Submissions subs;
    Access access;
    Royalties royalties;
    Reports reports;
    PersonRegistry registry;

    address admin = address(this);
    address treasury = makeAddr("treasury");
    address verifier = makeAddr("verifier");
    address curator = makeAddr("curator");
    address detector = makeAddr("detector");
    address registrar = makeAddr("registrar");

    uint256 constant STAKE = Constants.STAKE;
    uint256 constant FEE = Constants.STAKE_FEE;
    uint256 constant PRICE = Constants.NODE_VALUE; // the base price of a one-node topic
    uint256 constant WINDOW = Constants.VOTING_WINDOW;

    uint256 private _salt;

    function setUp() public virtual {
        vm.warp(1_700_000_000);
        roles = new LoreRoles(admin, treasury);
        graph = new TopicGraph(roles);
        royalties = new Royalties(roles, graph);
        subs = new Submissions(roles, graph);
        access = new Access(roles, graph, royalties);
        reports = new Reports(roles, graph);
        registry = new PersonRegistry(roles);

        graph.setSubmissions(address(subs));
        graph.setRoyalties(address(royalties));
        graph.setRegistry(address(registry));
        royalties.setAccess(address(access));
        roles.grantRole(roles.VERIFIER_ROLE(), verifier);
        roles.grantRole(roles.CURATOR_ROLE(), curator);
        roles.grantRole(roles.LEAK_DETECTOR_ROLE(), detector);
        roles.grantRole(roles.REGISTRAR_ROLE(), registrar);
    }

    // ---------- helpers ----------

    function _fund(address who, uint256 amount) internal {
        vm.deal(who, who.balance + amount);
    }

    function _bal(address who) internal view returns (uint256) {
        return who.balance;
    }

    /// @dev Unless already registered, `who` becomes its own person: one agent per person by default.
    function _ensurePerson(address who) internal {
        if (registry.personOf(who) == bytes32(0)) _link(who, _personId(who));
    }

    function _personId(address who) internal pure returns (bytes32) {
        return keccak256(abi.encode("person", who));
    }

    /// @dev Register `agent` to `person` (a second agent of an existing person, for example).
    function _link(address agent, bytes32 person) internal {
        vm.prank(registrar);
        registry.register(agent, person);
    }

    function _hash() internal returns (bytes32) {
        return keccak256(abi.encode("content", ++_salt));
    }

    function _submit(address who, uint256 topicId) internal returns (uint256 subId) {
        if (who.balance < STAKE) _fund(who, STAKE * 10);
        _ensurePerson(who);
        vm.prank(who);
        subId = subs.submit{value: STAKE}(topicId, _hash(), keccak256("root"));
    }

    function _report(uint256 subId, bool pass, uint256[] memory parents) internal {
        vm.prank(verifier);
        subs.postReport(subId, pass, parents, keccak256("report"));
    }

    function _report(uint256 subId, bool pass) internal {
        _report(subId, pass, new uint256[](0));
    }

    function _vote(address who, uint256 subId, bool approve) internal {
        vm.prank(who);
        subs.vote(subId, approve);
    }

    /// @dev New topic accepted by the verifier. Returns (topicId, nodeId).
    function _newTopic(address who) internal returns (uint256 topicId, uint256 nodeId) {
        uint256 subId = _submit(who, 0);
        _report(subId, true);
        subs.finalize(subId);
        nodeId = subs.getSubmission(subId).nodeId;
        (topicId,,,,,) = graph.nodes(nodeId);
    }

    /// @dev Existing-topic node accepted by verifier verdict after the window (no votes). Returns nodeId.
    function _addNode(address who, uint256 topicId, uint256[] memory parents) internal returns (uint256) {
        uint256 subId = _submit(who, topicId);
        _report(subId, true, parents);
        vm.warp(block.timestamp + WINDOW);
        subs.finalize(subId);
        vm.warp(block.timestamp + 1); // so the new contributor predates the next report
        return subs.getSubmission(subId).nodeId;
    }

    function _addNode(address who, uint256 topicId) internal returns (uint256) {
        return _addNode(who, topicId, new uint256[](0));
    }

    /// @dev Topic with `n` distinct contributors c[0..n-1], each with one node.
    function _topicWith(uint256 n) internal returns (uint256 topicId, address[] memory c) {
        c = new address[](n);
        for (uint256 i; i < n; ++i) {
            c[i] = makeAddr(string(abi.encodePacked("contributor", vm.toString(i))));
        }
        (topicId,) = _newTopic(c[0]);
        vm.warp(block.timestamp + 1);
        for (uint256 i = 1; i < n; ++i) {
            _addNode(c[i], topicId);
        }
    }

    function _buy(address who, uint256 topicId) internal {
        uint256 cost = access.priceOf(topicId);
        if (who.balance < cost) _fund(who, cost);
        vm.prank(who);
        access.buy{value: cost}(topicId);
    }

    function _status(uint256 subId) internal view returns (ISubmissions.Status) {
        return subs.getSubmission(subId).status;
    }

    function _arr(uint256 a) internal pure returns (uint256[] memory r) {
        r = new uint256[](1);
        r[0] = a;
    }

    function _arr(uint256 a, uint256 b) internal pure returns (uint256[] memory r) {
        r = new uint256[](2);
        r[0] = a;
        r[1] = b;
    }
}
