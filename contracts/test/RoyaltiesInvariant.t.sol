// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Base} from "./Base.t.sol";
import {TopicGraph} from "../src/TopicGraph.sol";
import {Access} from "../src/Access.sol";
import {Royalties} from "../src/Royalties.sol";

/// @dev Drives random sequences of node additions, sales (at random prices) and claims.
///      It stands in for Submissions so it can add nodes without the voting flow. This test is about royalty accounting.
contract RoyaltiesHandler is Test {
    TopicGraph graph;
    Access access;
    Royalties royalties;
    address admin;

    address[] public contributors;
    uint256 public received; // total 0G paid in by buyers
    uint256 public claimed; // total 0G claimed by contributors
    uint256 buyerNonce;

    constructor(TopicGraph g, Access a, Royalties r, address admin_) {
        graph = g;
        access = a;
        royalties = r;
        admin = admin_;
        for (uint256 i; i < 4; ++i) {
            contributors.push(makeAddr(string(abi.encodePacked("c", vm.toString(i)))));
        }
    }

    function addNode(uint256 topicSeed, uint256 who) external {
        uint256 topics = graph.topicCount();
        uint256 topicId = topics == 0 || topicSeed % 4 == 0 ? 0 : 1 + topicSeed % topics;
        graph.addNode(
            topicId, contributors[who % contributors.length], keccak256(abi.encode(topicSeed, who)), 0, new uint256[](0)
        );
    }

    function buy(uint256 topicSeed, uint256 price) external {
        uint256 topics = graph.topicCount();
        if (topics == 0) return;
        uint256 topicId = 1 + topicSeed % topics;
        vm.prank(admin);
        access.setParams(bound(price, 0, 1_000_000 ether));
        price = access.priceOf(topicId);

        address buyer = address(uint160(0xB0000 + ++buyerNonce));
        vm.deal(buyer, price);
        vm.prank(buyer);
        access.buy{value: price}(topicId);
        received += price;
    }

    function claim(uint256 nodeSeed) external {
        uint256 nodes = graph.nextNodeId() - 1;
        if (nodes == 0) return;
        uint256 nodeId = 1 + nodeSeed % nodes;
        address who = graph.getNode(nodeId).contributor;
        uint256 before = _bal(who);
        uint256[] memory ids = new uint256[](1);
        ids[0] = nodeId;
        vm.prank(who);
        royalties.claim(ids);
        claimed += _bal(who) - before;
    }

    function _bal(address who) internal view returns (uint256) {
        return who.balance;
    }

    function setCut(uint256 bps) external {
        vm.prank(admin);
        royalties.setPlatformCutBps(bound(bps, 0, 10_000));
    }
}

contract RoyaltiesInvariantTest is Base {
    RoyaltiesHandler handler;

    function setUp() public override {
        super.setUp();
        handler = new RoyaltiesHandler(graph, access, royalties, admin);
        graph.setSubmissions(address(handler));
        access.setParams(access.nodeValue());
        for (uint256 i; i < 4; ++i) {
            _ensurePerson(handler.contributors(i));
        }
        targetContract(address(handler));
    }

    function _sumClaimable() internal view returns (uint256 total) {
        uint256 nodes = graph.nextNodeId() - 1;
        for (uint256 i = 1; i <= nodes; ++i) {
            total += royalties.claimable(i);
        }
    }

    /// @notice Total paid out (treasury + contributor claims) never exceeds total received.
    function invariant_paidOutNeverExceedsReceived() public view {
        uint256 paidOut = _bal(treasury) + handler.claimed();
        assertLe(paidOut, handler.received());
    }

    /// @notice Royalties holds exactly what contributors can still claim: nothing is lost or owed beyond its balance.
    function invariant_balanceEqualsOutstanding() public view {
        assertEq(_bal(address(royalties)), _sumClaimable());
        assertEq(_bal(treasury) + handler.claimed() + _bal(address(royalties)), handler.received());
    }

    /// @notice Stateless fuzz of one sale: shares + treasury add up to the price exactly.
    function testFuzz_distributeConserves(uint256 price, uint8 extraNodes, uint16 cutBps) public {
        cutBps = uint16(bound(cutBps, 0, 10_000));
        royalties.setPlatformCutBps(cutBps);
        access.setParams(bound(price, 0, 50_000_000 ether));

        graph.setSubmissions(address(this));
        _ensurePerson(makeAddr("a"));
        (uint256 t,) = graph.addNode(0, makeAddr("a"), keccak256("n0"), 0, new uint256[](0));
        uint256 n = 1 + uint256(extraNodes) % 20;
        for (uint256 i = 1; i < n; ++i) {
            graph.addNode(t, makeAddr("a"), keccak256(abi.encode(i)), 0, new uint256[](0));
        }

        price = access.priceOf(t);
        assertEq(price, access.nodeValue() * n);
        _buy(makeAddr("buyer"), t);
        uint256 shares = royalties.claimable(1) * n;
        assertEq(shares + _bal(treasury), price);
        assertLt(_bal(treasury) - price * cutBps / 10_000, n); // dust < nodeCount
    }
}
