// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Base} from "./Base.t.sol";
import {Access} from "../src/Access.sol";
import {Royalties} from "../src/Royalties.sol";
import {Reports} from "../src/Reports.sol";
import {LoreBase} from "../src/LoreBase.sol";
import {IAccess, IReports} from "../src/interfaces/ILore.sol";

contract AccessTest is Base {
    address alice = makeAddr("alice");
    address buyer = makeAddr("buyer");

    function test_buy_mintsAndGrantsAccess() public {
        (uint256 t,) = _newTopic(alice);
        assertFalse(access.hasAccess(t, buyer));
        uint256 cost = access.priceOf(t);
        _fund(buyer, cost);
        vm.expectEmit(address(access));
        emit IAccess.Bought(t, buyer);
        vm.prank(buyer);
        access.buy{value: cost}(t);
        assertTrue(access.hasAccess(t, buyer));
        assertEq(access.balanceOf(buyer, t), 1);
        assertEq(_bal(buyer), 0);
    }

    function test_buy_secondBuyReverts() public {
        (uint256 t,) = _newTopic(alice);
        _buy(buyer, t);
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Access.AlreadyHeld.selector, t));
        access.buy(t);
    }

    function test_buy_unknownTopicReverts() public {
        _fund(buyer, PRICE);
        vm.prank(buyer);
        vm.expectRevert(abi.encodeWithSelector(Access.UnknownTopic.selector, 1));
        access.buy(1);
    }

    function test_contributingDoesNotGrantAccess() public {
        (uint256 t,) = _newTopic(alice);
        assertFalse(access.hasAccess(t, alice));
    }

    function test_soulbound_transfersRevert() public {
        (uint256 t,) = _newTopic(alice);
        _buy(buyer, t);
        vm.startPrank(buyer);
        vm.expectRevert(Access.Soulbound.selector);
        access.safeTransferFrom(buyer, alice, t, 1, "");
        uint256[] memory ids = new uint256[](1);
        uint256[] memory vals = new uint256[](1);
        ids[0] = t;
        vals[0] = 1;
        vm.expectRevert(Access.Soulbound.selector);
        access.safeBatchTransferFrom(buyer, alice, ids, vals, "");
        vm.expectRevert(Access.Soulbound.selector);
        access.setApprovalForAll(alice, true);
        vm.stopPrank();
        assertTrue(access.hasAccess(t, buyer));
    }

    function test_accessCoversLaterNodes() public {
        (uint256 t,) = _newTopic(alice);
        _buy(buyer, t);
        _addNode(makeAddr("bob"), t);
        assertTrue(access.hasAccess(t, buyer)); // access is per topic, not per node
    }

    function test_setParams_onlyAdmin() public {
        vm.prank(buyer);
        vm.expectRevert(LoreBase.NotAuthorized.selector);
        access.setParams(1);
    }

    function test_price_isNodeValueTimesNodes() public {
        (uint256 t,) = _newTopic(alice);
        assertEq(access.priceOf(t), PRICE);
        _addNode(makeAddr("bob"), t);
        _addNode(makeAddr("carol"), t);
        assertEq(access.priceOf(t), 3 * PRICE);
        access.setParams(2 * PRICE);
        assertEq(access.priceOf(t), 6 * PRICE);
        uint256 before = _bal(address(royalties));
        _buy(buyer, t);
        assertEq(_bal(address(royalties)) - before, 6 * PRICE - 6 * PRICE / 10); // treasury took the 10% cut
    }

    function test_eachNodeEarnsNinetyPercentOfNodeValuePerSale() public {
        (uint256 t, uint256 n1) = _newTopic(alice);
        uint256 n2 = _addNode(makeAddr("bob"), t);
        uint256 n3 = _addNode(makeAddr("carol"), t);
        _buy(buyer, t);
        assertEq(royalties.claimable(n1), PRICE * 9 / 10);
        assertEq(royalties.claimable(n2), PRICE * 9 / 10);
        assertEq(royalties.claimable(n3), PRICE * 9 / 10);
    }
}

contract RoyaltiesTest is Base {
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    function test_distribute_onlyAccess() public {
        vm.expectRevert(LoreBase.NotAuthorized.selector);
        royalties.distribute(1, 1);
        vm.expectRevert(LoreBase.NotAuthorized.selector);
        royalties.onNodeAdded(1, 1);
    }

    function test_sale_splitsCutAndNodes() public {
        (uint256 t, uint256 n1) = _newTopic(alice);
        uint256 tBefore = _bal(treasury);
        _buy(makeAddr("b1"), t);
        assertEq(_bal(treasury) - tBefore, PRICE / 10); // 10% cut, no dust
        assertEq(royalties.claimable(n1), PRICE * 9 / 10);
    }

    function test_nodeAcceptedAfterSale_earnsNothingFromIt() public {
        (uint256 t, uint256 n1) = _newTopic(alice);
        _buy(makeAddr("b1"), t); // 1 node: price 1, n1 gets 0.9
        uint256 n2 = _addNode(bob, t);
        assertEq(royalties.claimable(n2), 0);
        _buy(makeAddr("b2"), t); // 2 nodes: price 2, 0.9 each
        assertEq(royalties.claimable(n1), 2 * (PRICE * 9 / 10));
        assertEq(royalties.claimable(n2), PRICE * 9 / 10);
    }

    function test_dustToTreasury() public {
        access.setParams(7);
        (uint256 t, uint256 n1) = _newTopic(alice);
        uint256 n2 = _addNode(bob, t);
        uint256 n3 = _addNode(makeAddr("carol"), t);
        uint256 tBefore = _bal(treasury);
        _buy(makeAddr("b1"), t);
        // price 21; cut floor(2.1) = 2; rest 19; per node 6; dust 1
        assertEq(_bal(treasury) - tBefore, 3);
        assertEq(royalties.claimable(n1), 6);
        assertEq(royalties.claimable(n2), 6);
        assertEq(royalties.claimable(n3), 6);
        assertEq(_bal(address(royalties)), 18);
    }

    function test_claim_paysContributorOnce() public {
        (uint256 t, uint256 n1) = _newTopic(alice);
        _buy(makeAddr("b1"), t);
        uint256 before = _bal(alice);
        vm.prank(alice);
        royalties.claim(_arr(n1, n1)); // duplicate id pays once
        assertEq(_bal(alice) - before, PRICE * 9 / 10); // 1 node: 90% of its base price
        assertEq(royalties.claimable(n1), 0);
        vm.prank(alice);
        royalties.claim(_arr(n1)); // nothing left, no revert
        assertEq(_bal(alice) - before, PRICE * 9 / 10); // 1 node: 90% of its base price
    }

    function test_claim_notContributorReverts() public {
        (uint256 t, uint256 n1) = _newTopic(alice);
        _buy(makeAddr("b1"), t);
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(Royalties.NotContributor.selector, n1));
        royalties.claim(_arr(n1));
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(Royalties.NotContributor.selector, 42));
        royalties.claim(_arr(42));
    }

    function test_claimableMany() public {
        (uint256 t, uint256 n1) = _newTopic(alice);
        uint256 n2 = _addNode(alice, t);
        _buy(makeAddr("b1"), t);
        assertEq(royalties.claimableMany(_arr(n1, n2)), 2 * (PRICE * 9 / 10)); // 2 nodes: 90% of the base each
    }

    function test_setPlatformCut_guard() public {
        vm.expectRevert(LoreBase.InvalidParam.selector);
        royalties.setPlatformCutBps(10_001);
    }
}

contract ReportsTest is Base {
    address alice = makeAddr("alice");

    function test_reportAndMatch() public {
        (uint256 t,) = _newTopic(alice);
        vm.expectEmit(address(reports));
        emit IReports.LeakReported(1, t, alice, keccak256("ev"));
        vm.prank(alice);
        uint256 id = reports.report(t, keccak256("ev"));
        assertEq(id, 1);

        vm.prank(alice);
        vm.expectRevert(LoreBase.NotAuthorized.selector);
        reports.recordMatch(id, alice, 9000);

        vm.prank(detector);
        reports.recordMatch(id, makeAddr("leaker"), 9000);
        Reports.Report memory r = reports.getReport(id);
        assertEq(r.matchedWallet, makeAddr("leaker"));
        assertEq(r.scoreBps, 9000);

        vm.prank(detector);
        vm.expectRevert(abi.encodeWithSelector(Reports.AlreadyMatched.selector, id));
        reports.recordMatch(id, alice, 1);
    }

    function test_report_unknownTopicReverts() public {
        vm.expectRevert(abi.encodeWithSelector(Reports.UnknownTopic.selector, 3));
        reports.report(3, bytes32(0));
    }

    function test_recordMatch_guards() public {
        (uint256 t,) = _newTopic(alice);
        uint256 id = reports.report(t, bytes32(0));
        vm.startPrank(detector);
        vm.expectRevert(abi.encodeWithSelector(Reports.UnknownReport.selector, 2));
        reports.recordMatch(2, alice, 1);
        vm.expectRevert(LoreBase.InvalidParam.selector);
        reports.recordMatch(id, alice, 10_001);
        vm.stopPrank();
    }
}
