// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Base} from "./Base.t.sol";
import {Submissions} from "../src/Submissions.sol";
import {LoreBase} from "../src/LoreBase.sol";
import {TopicGraph} from "../src/TopicGraph.sol";
import {ISubmissions} from "../src/interfaces/ILore.sol";

contract SubmissionsTest is Base {
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    function setUp() public override {
        super.setUp();
        _ensurePerson(alice); // World ID: needed to submit, and to vote
        _ensurePerson(bob);
    }

    // ---------- submit ----------

    function test_submit_pullsStake() public {
        _fund(alice, STAKE);
        vm.prank(alice);
        uint256 subId = subs.submit{value: STAKE}(0, keccak256("x"), keccak256("root"));
        assertEq(subId, 1);
        assertEq(_bal(alice), 0);
        assertEq(_bal(address(subs)), STAKE);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Pending));
    }

    function test_submit_exactDuplicateReverts() public {
        _fund(alice, STAKE * 2);
        vm.startPrank(alice);
        subs.submit{value: STAKE}(0, keccak256("x"), keccak256("root"));
        vm.expectRevert(abi.encodeWithSelector(Submissions.DuplicateContent.selector, keccak256("x")));
        subs.submit{value: STAKE}(0, keccak256("x"), keccak256("root2"));
        vm.stopPrank();
    }

    function test_submit_unknownTopicReverts() public {
        _fund(alice, STAKE);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(Submissions.UnknownTopic.selector, 7));
        subs.submit{value: STAKE}(7, keccak256("x"), keccak256("root"));
    }

    // ---------- postReport ----------

    function test_postReport_onlyVerifier() public {
        uint256 subId = _submit(alice, 0);
        vm.prank(alice);
        vm.expectRevert(LoreBase.NotAuthorized.selector);
        subs.postReport(subId, true, new uint256[](0), bytes32(0));
    }

    function test_postReport_onlyOnce() public {
        uint256 subId = _submit(alice, 0);
        _report(subId, true);
        vm.prank(verifier);
        vm.expectRevert(abi.encodeWithSelector(Submissions.WrongStatus.selector, ISubmissions.Status.Reported));
        subs.postReport(subId, true, new uint256[](0), bytes32(0));
    }

    function test_postReport_snapshotsEligibleVoters() public {
        (uint256 topicId,) = _topicWith(3);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        Submissions.Submission memory s = subs.getSubmission(subId);
        assertEq(s.eligibleVoters, 3);
        assertEq(s.reportedAt, block.timestamp);
        assertEq(subs.votingEndsAt(subId), block.timestamp + WINDOW);
    }

    function test_postReport_invalidParentsRevert() public {
        (uint256 topicA, uint256 nodeA) = _newTopic(alice);
        (, uint256 nodeB) = _newTopic(bob);
        uint256 subId = _submit(makeAddr("carol"), topicA);
        vm.startPrank(verifier);
        vm.expectRevert(abi.encodeWithSelector(TopicGraph.ParentNotInTopic.selector, nodeB));
        subs.postReport(subId, true, _arr(nodeB), bytes32(0));
        vm.expectRevert(abi.encodeWithSelector(TopicGraph.DuplicateParent.selector, nodeA));
        subs.postReport(subId, true, _arr(nodeA, nodeA), bytes32(0));
        vm.stopPrank();
    }

    function test_postReport_newTopicWithParentsReverts() public {
        (, uint256 nodeA) = _newTopic(alice);
        uint256 subId = _submit(bob, 0);
        vm.prank(verifier);
        vm.expectRevert(abi.encodeWithSelector(TopicGraph.ParentNotInTopic.selector, nodeA));
        subs.postReport(subId, true, _arr(nodeA), bytes32(0));
    }

    // ---------- new topic ----------

    function test_newTopic_accept() public {
        uint256 subId = _submit(alice, 0);
        _report(subId, true);
        assertTrue(subs.canFinalize(subId)); // right after the report
        subs.finalize(subId);

        Submissions.Submission memory s = subs.getSubmission(subId);
        assertEq(uint8(s.status), uint8(ISubmissions.Status.Accepted));
        assertEq(s.nodeId, 1);
        assertEq(graph.topicCount(), 1);
        assertEq(graph.nodeCount(1), 1);
        assertEq(graph.contributorSince(1, alice), block.timestamp);
        assertEq(graph.contributorCount(1), 1);
        assertEq(_bal(alice), STAKE * 10 - FEE);
        assertEq(_bal(treasury), FEE);
        assertEq(_bal(address(subs)), 0);
    }

    function test_newTopic_reject() public {
        uint256 subId = _submit(alice, 0);
        _report(subId, false);
        subs.finalize(subId);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Rejected));
        assertEq(graph.topicCount(), 0);
        assertEq(_bal(treasury), STAKE); // no voters on a new topic
        assertEq(_bal(alice), STAKE * 9);
    }

    function test_newTopic_noVoting() public {
        uint256 subId = _submit(alice, 0);
        _report(subId, true);
        vm.prank(bob);
        vm.expectRevert(Submissions.NewTopicNoVoting.selector);
        subs.vote(subId, true);
    }

    function test_finalize_pendingReverts() public {
        uint256 subId = _submit(alice, 0);
        vm.expectRevert(abi.encodeWithSelector(Submissions.WrongStatus.selector, ISubmissions.Status.Pending));
        subs.finalize(subId);
    }

    function test_finalize_twiceReverts() public {
        uint256 subId = _submit(alice, 0);
        _report(subId, true);
        subs.finalize(subId);
        vm.expectRevert(abi.encodeWithSelector(Submissions.WrongStatus.selector, ISubmissions.Status.Accepted));
        subs.finalize(subId);
    }

    // ---------- existing topic outcomes ----------

    function test_existing_quorumYes_overridesVerifier() public {
        (uint256 topicId, address[] memory c) = _topicWith(3); // threshold = 1
        uint256 subId = _submit(alice, topicId);
        _report(subId, false);
        _vote(c[0], subId, true);
        assertFalse(subs.canFinalize(subId)); // yes = 1, not > 1
        _vote(c[1], subId, true);
        assertTrue(subs.canFinalize(subId)); // early: yes = 2 > 1
        subs.finalize(subId);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Accepted));
    }

    function test_existing_quorumNo_overridesVerifier() public {
        (uint256 topicId, address[] memory c) = _topicWith(3);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        _vote(c[0], subId, false);
        _vote(c[1], subId, false);
        subs.finalize(subId);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Rejected));
    }

    function test_existing_tie_verifierDecides_reject() public {
        (uint256 topicId, address[] memory c) = _topicWith(4); // threshold = 2
        uint256 subId = _submit(alice, topicId);
        _report(subId, false);
        _vote(c[0], subId, true);
        _vote(c[1], subId, true);
        _vote(c[2], subId, false);
        _vote(c[3], subId, false);
        assertFalse(subs.canFinalize(subId)); // neither side > 2
        vm.warp(block.timestamp + WINDOW);
        subs.finalize(subId);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Rejected));
    }

    function test_existing_tie_verifierDecides_accept() public {
        (uint256 topicId, address[] memory c) = _topicWith(4);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        _vote(c[0], subId, true);
        _vote(c[1], subId, false);
        vm.warp(block.timestamp + WINDOW);
        subs.finalize(subId); // 2 of 4 voted = quorum, 1-1 tie => verifier
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Accepted));
    }

    function test_existing_quorumMajority_afterWindow() public {
        (uint256 topicId, address[] memory c) = _topicWith(4);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        _vote(c[0], subId, false);
        _vote(c[1], subId, false);
        _vote(c[2], subId, true);
        vm.warp(block.timestamp + WINDOW);
        subs.finalize(subId); // 3 of 4 voted, 2 no vs 1 yes
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Rejected));
    }

    function test_existing_noQuorum_verifierDecides() public {
        (uint256 topicId, address[] memory c) = _topicWith(4);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        _vote(c[0], subId, false); // 1 of 4 < 50%
        vm.warp(block.timestamp + WINDOW);
        subs.finalize(subId);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Accepted));
    }

    function test_existing_noVotes_verifierRejects() public {
        (uint256 topicId,) = _topicWith(2);
        uint256 subId = _submit(alice, topicId);
        _report(subId, false);
        vm.warp(block.timestamp + WINDOW);
        subs.finalize(subId);
        assertEq(uint8(_status(subId)), uint8(ISubmissions.Status.Rejected));
        assertEq(_bal(address(subs)), 0);
    }

    function test_existing_beforeWindow_undecidedReverts() public {
        (uint256 topicId, address[] memory c) = _topicWith(3);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        _vote(c[0], subId, true);
        vm.warp(block.timestamp + WINDOW - 1);
        vm.expectRevert(Submissions.NotFinalizable.selector);
        subs.finalize(subId);
        vm.warp(block.timestamp + 1);
        subs.finalize(subId);
    }

    function test_existing_acceptAddsNodeWithParents() public {
        (uint256 topicId, uint256 node1) = _newTopic(alice);
        vm.warp(block.timestamp + 1);
        uint256 subId = _submit(bob, topicId);
        _report(subId, true, _arr(node1));
        _vote(alice, subId, true); // 1 eligible, threshold 0 => early
        subs.finalize(subId);
        uint256 nodeId = subs.getSubmission(subId).nodeId;
        assertEq(graph.parentsOf(nodeId), _arr(node1));
        assertEq(graph.contributorCount(topicId), 2);
    }

    // ---------- vote eligibility, one rule per test ----------

    function _openVote() internal returns (uint256 topicId, address[] memory c, uint256 subId) {
        (topicId, c) = _topicWith(3);
        subId = _submit(alice, topicId);
        _report(subId, true);
    }

    function test_vote_requiresReported() public {
        (uint256 topicId, address[] memory c) = _topicWith(2);
        uint256 subId = _submit(alice, topicId);
        vm.prank(c[0]);
        vm.expectRevert(abi.encodeWithSelector(Submissions.WrongStatus.selector, ISubmissions.Status.Pending));
        subs.vote(subId, true);
    }

    function test_vote_windowClosed() public {
        (, address[] memory c, uint256 subId) = _openVote();
        vm.warp(block.timestamp + WINDOW);
        assertFalse(subs.canVote(subId, c[0]));
        vm.prank(c[0]);
        vm.expectRevert(Submissions.VotingClosed.selector);
        subs.vote(subId, true);
    }

    function test_vote_nonContributor() public {
        (,, uint256 subId) = _openVote();
        assertFalse(subs.canVote(subId, bob));
        vm.prank(bob);
        vm.expectRevert(Submissions.NotEligible.selector);
        subs.vote(subId, true);
    }

    function test_vote_contributorAfterReport() public {
        (uint256 topicId, address[] memory c) = _topicWith(2);
        address late = makeAddr("late");
        uint256 lateSub = _submit(late, topicId);
        _report(lateSub, true);
        vm.warp(block.timestamp + 10);
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        vm.warp(block.timestamp + WINDOW - 5); // lateSub's window over, subId's still open
        subs.finalize(lateSub);
        assertGt(graph.contributorSince(topicId, late), 0);

        assertTrue(subs.canVote(subId, c[0]));
        assertFalse(subs.canVote(subId, late));
        vm.prank(late);
        vm.expectRevert(Submissions.NotEligible.selector);
        subs.vote(subId, true);
    }

    function test_vote_contributorSameTimestampAsReport() public {
        (uint256 topicId,) = _topicWith(1);
        address same = makeAddr("same");
        uint256 sameSub = _submit(same, topicId);
        _report(sameSub, true);
        vm.warp(block.timestamp + WINDOW);
        uint256 subId = _submit(alice, topicId);
        subs.finalize(sameSub); // same becomes contributor at t
        _report(subId, true); // report also at t => since == reportedAt, not < reportedAt
        vm.prank(same);
        vm.expectRevert(Submissions.NotEligible.selector);
        subs.vote(subId, true);
    }

    function test_vote_twiceReverts() public {
        (, address[] memory c, uint256 subId) = _openVote();
        _vote(c[0], subId, true);
        vm.prank(c[0]);
        vm.expectRevert(Submissions.AlreadyVoted.selector);
        subs.vote(subId, false);
    }

    function test_vote_submitterCannotVote() public {
        (uint256 topicId, address[] memory c) = _topicWith(2);
        uint256 subId = _submit(c[0], topicId);
        _report(subId, true);
        assertFalse(subs.canVote(subId, c[0]));
        vm.prank(c[0]);
        vm.expectRevert(Submissions.SubmitterCannotVote.selector);
        subs.vote(subId, true);
    }

    function test_vote_recordsAndEmits() public {
        (, address[] memory c, uint256 subId) = _openVote();
        assertTrue(subs.canVote(subId, c[1]));
        vm.expectEmit(address(subs));
        emit ISubmissions.Voted(subId, c[1], _personId(c[1]), false);
        _vote(c[1], subId, false);
        assertEq(subs.voteOf(subId, _personId(c[1])), 2);
        assertEq(subs.getSubmission(subId).no, 1);
    }

    // ---------- slash ----------

    function test_reject_sendsWholeStakeToTreasury_votersGetNothing() public {
        (uint256 topicId, address[] memory c) = _topicWith(4); // threshold 2
        uint256 subId = _submit(alice, topicId);
        _report(subId, true);
        _vote(c[0], subId, false);
        _vote(c[1], subId, false);
        _vote(c[2], subId, false); // early: no = 3 > 2
        _vote(c[3], subId, true);
        uint256 treasuryBefore = _bal(treasury);
        uint256[] memory votersBefore = new uint256[](4);
        for (uint256 i; i < 4; ++i) votersBefore[i] = _bal(c[i]);

        subs.finalize(subId);

        assertEq(uint256(subs.getSubmission(subId).status), uint256(ISubmissions.Status.Rejected));
        assertEq(_bal(treasury) - treasuryBefore, STAKE);
        for (uint256 i; i < 4; ++i) assertEq(_bal(c[i]), votersBefore[i]);
        assertEq(_bal(address(subs)), 0);
    }

    function test_reject_byVerifierWithoutVotes_sendsWholeStakeToTreasury() public {
        uint256 subId = _submit(alice, 0); // new topic: verifier decides alone
        _report(subId, false);
        uint256 before = _bal(treasury);
        subs.finalize(subId);
        assertEq(_bal(treasury) - before, STAKE);
        assertEq(_bal(address(subs)), 0);
    }

    // ---------- params ----------

    function test_setParams_stakeSnapshotPerSubmission() public {
        uint256 subId = _submit(alice, 0);
        subs.setParams(20e6, 1e6, 1 hours, 6000);
        _report(subId, true);
        uint256 before = _bal(alice);
        subs.finalize(subId);
        assertEq(_bal(alice) - before, STAKE - FEE); // old stake and fee
    }

    function test_setParams_guards() public {
        vm.expectRevert(LoreBase.InvalidParam.selector);
        subs.setParams(1e6, 2e6, 1 hours, 5000); // fee > stake
        vm.expectRevert(LoreBase.InvalidParam.selector);
        subs.setParams(1e6, 0, 1 hours, 4999); // quorum < 50%
        vm.prank(alice);
        vm.expectRevert(LoreBase.NotAuthorized.selector);
        subs.setParams(1e6, 0, 1 hours, 5000);
    }
}
