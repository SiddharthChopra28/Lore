// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Address} from "@openzeppelin/contracts/utils/Address.sol";
import {ISubmissions} from "./interfaces/ILore.sol";
import {LoreBase} from "./LoreBase.sol";
import {LoreRoles} from "./LoreRoles.sol";
import {TopicGraph} from "./TopicGraph.sol";
import {Constants} from "./Constants.sol";

/// @notice Staked submissions: verifier report, contributor vote, finalize. Rules from BUILD_PLAN.md section 3.2.
contract Submissions is ISubmissions, LoreBase {

    struct Submission {
        uint256 topicId; // 0 = new topic
        address submitter;
        bytes32 contentHash;
        bytes32 storageRoot;
        Status status;
        bool pass; // verifier verdict
        uint64 reportedAt;
        uint32 yes;
        uint32 no;
        uint256 eligibleVoters; // contributorCount snapshot at report time
        uint256 stake; // snapshot at submit time
        uint256 fee; // snapshot at submit time
        bytes32 reportHash;
        uint256 nodeId; // set on accept
        uint256[] parents;
    }

    /// @dev 0 = not voted, 1 = yes, 2 = no
    uint8 internal constant VOTE_NONE = 0;
    uint8 internal constant VOTE_YES = 1;
    uint8 internal constant VOTE_NO = 2;

    TopicGraph public immutable graph;

    uint256 public stake = Constants.STAKE;
    uint256 public stakeFee = Constants.STAKE_FEE;
    uint256 public votingWindow = Constants.VOTING_WINDOW;
    uint256 public quorumBps = Constants.QUORUM_BPS;

    uint256 public nextSubId = 1;
    mapping(uint256 => Submission) private _subs;
    mapping(bytes32 => bool) public contentHashUsed;
    /// @notice subId => person (World ID) => vote. One vote per person, whichever of their agents cast it.
    mapping(uint256 => mapping(bytes32 => uint8)) public voteOf;

    event ParamsSet(uint256 stake, uint256 stakeFee, uint256 votingWindow, uint256 quorumBps);

    error UnknownTopic(uint256 topicId);
    error UnknownSubmission(uint256 subId);
    error DuplicateContent(bytes32 contentHash);
    error WrongStatus(Status status);
    error NewTopicNoVoting();
    error VotingClosed();
    error NotEligible();
    error AlreadyVoted();
    error SubmitterCannotVote();
    error NotFinalizable();
    error NotRegistered(address agent);
    error Banned(address agent);
    error WrongStake(uint256 stake, uint256 sent);

    constructor(LoreRoles roles_, TopicGraph graph_) LoreBase(roles_) {
        if (address(graph_) == address(0)) revert ZeroAddress();
        graph = graph_;
    }

    // ---------- admin ----------

    /// @notice quorumBps must be >= 50%: early finalize relies on "one side alone above quorum" meaning
    ///         that side already holds a strict majority of every eligible voter, so the result can't change.
    function setParams(
        uint256 stake_,
        uint256 stakeFee_,
        uint256 votingWindow_,
        uint256 quorumBps_
    ) external onlyAdmin {
        if (
            stakeFee_ > stake_ || votingWindow_ == 0 || quorumBps_ < 5000
                || quorumBps_ > Constants.BPS
        ) revert InvalidParam();
        stake = stake_;
        stakeFee = stakeFee_;
        votingWindow = votingWindow_;
        quorumBps = quorumBps_;
        emit ParamsSet(stake_, stakeFee_, votingWindow_, quorumBps_);
    }

    // ---------- flow ----------

    /// @inheritdoc ISubmissions
    /// @dev Send exactly `stake` 0G with the call.
    function submit(uint256 topicId, bytes32 contentHash, bytes32 storageRoot) external payable returns (uint256 subId) {
        if (topicId != 0 && !graph.topicExists(topicId)) revert UnknownTopic(topicId);
        if (_personOf(msg.sender) == bytes32(0)) revert NotRegistered(msg.sender); // World ID first
        if (graph.registry().isBanned(msg.sender)) revert Banned(msg.sender);
        if (msg.value != stake) revert WrongStake(stake, msg.value);
        if (contentHashUsed[contentHash]) revert DuplicateContent(contentHash);
        contentHashUsed[contentHash] = true;

        subId = nextSubId++;
        Submission storage s = _subs[subId];
        s.topicId = topicId;
        s.submitter = msg.sender;
        s.contentHash = contentHash;
        s.storageRoot = storageRoot;
        s.stake = stake;
        s.fee = stakeFee;

        emit Submitted(subId, topicId, msg.sender, contentHash, storageRoot);
    }

    /// @inheritdoc ISubmissions
    /// @dev Parents are validated here (same rules as TopicGraph.addNode), so finalize can't revert on them later.
    ///      A new-topic submission must report no parents.
    function postReport(uint256 subId, bool pass, uint256[] calldata parents, bytes32 reportHash)
        external
        onlyRole(roles.VERIFIER_ROLE())
    {
        Submission storage s = _get(subId);
        if (s.status != Status.Pending) revert WrongStatus(s.status);
        graph.checkParents(s.topicId, parents);

        s.status = Status.Reported;
        s.pass = pass;
        s.parents = parents;
        s.reportHash = reportHash;
        s.reportedAt = uint64(block.timestamp);
        s.eligibleVoters = s.topicId == 0 ? 0 : graph.personCount(s.topicId); // people, not agents

        emit Reported(subId, pass, parents, reportHash, s.eligibleVoters);
    }

    /// @inheritdoc ISubmissions
    function vote(uint256 subId, bool approve) external {
        Submission storage s = _get(subId);
        bytes32 person = _checkVote(s, subId, msg.sender);

        voteOf[subId][person] = approve ? VOTE_YES : VOTE_NO;
        if (approve) s.yes++;
        else s.no++;
        emit Voted(subId, msg.sender, person, approve);
    }

    /// @inheritdoc ISubmissions
    function finalize(uint256 subId) external {
        Submission storage s = _get(subId);
        if (s.status != Status.Reported) revert WrongStatus(s.status);
        if (!_finalizable(s)) revert NotFinalizable();

        bool accepted = _outcome(s);
        address treasury = _treasury();

        if (accepted) {
            s.status = Status.Accepted;
            (, uint256 nodeId) = graph.addNode(s.topicId, s.submitter, s.contentHash, s.storageRoot, s.parents);
            s.nodeId = nodeId;
            // ponytail: a submitter contract that rejects 0G blocks only its own finalize. Pull payments if needed.
            if (s.stake - s.fee > 0) Address.sendValue(payable(s.submitter), s.stake - s.fee);
            if (s.fee > 0) Address.sendValue(payable(treasury), s.fee);
            emit Finalized(subId, true, nodeId);
        } else {
            s.status = Status.Rejected;
            // The whole stake goes to the platform. Voters are never paid, so no vote is bought.
            if (s.stake > 0) Address.sendValue(payable(treasury), s.stake);
            emit Finalized(subId, false, 0);
        }
    }

    // ---------- views (for the gateway, so its checks mirror the contract exactly) ----------

    function getSubmission(uint256 subId) external view returns (Submission memory) {
        return _get(subId);
    }

    function submissionCount() external view returns (uint256) {
        return nextSubId - 1;
    }

    /// @notice True iff `voter` could call vote(subId, ...) right now.
    function canVote(uint256 subId, address voter) external view returns (bool) {
        if (subId == 0 || subId >= nextSubId) return false;
        Submission storage s = _subs[subId];
        if (s.status != Status.Reported || s.topicId == 0) return false;
        if (block.timestamp >= uint256(s.reportedAt) + votingWindow) return false;
        bytes32 person = _personOf(voter);
        if (person == bytes32(0) || graph.registry().isBanned(voter)) return false;
        uint64 since = graph.personSince(s.topicId, person);
        if (since == 0 || since >= s.reportedAt) return false;
        if (voteOf[subId][person] != VOTE_NONE) return false;
        return person != _personOf(s.submitter);
    }

    /// @notice True iff finalize(subId) would succeed right now. Used by the gateway's finalizer.
    function canFinalize(uint256 subId) external view returns (bool) {
        if (subId == 0 || subId >= nextSubId) return false;
        Submission storage s = _subs[subId];
        return s.status == Status.Reported && _finalizable(s);
    }

    /// @notice One side's vote count must exceed this for an early finalize.
    function quorumThreshold(uint256 subId) public view returns (uint256) {
        return _get(subId).eligibleVoters * quorumBps / Constants.BPS;
    }

    function votingEndsAt(uint256 subId) external view returns (uint256) {
        Submission storage s = _get(subId);
        return s.reportedAt == 0 ? 0 : uint256(s.reportedAt) + votingWindow;
    }

    // ---------- internals ----------

    function _get(uint256 subId) internal view returns (Submission storage s) {
        if (subId == 0 || subId >= nextSubId) revert UnknownSubmission(subId);
        s = _subs[subId];
    }

    /// @dev Eligibility is per person: a person who contributed to the topic before the report can vote once,
    ///      from any of their agents, and never on a submission by one of their own agents.
    function _checkVote(Submission storage s, uint256 subId, address voter) internal view returns (bytes32 person) {
        if (s.status != Status.Reported) revert WrongStatus(s.status);
        if (s.topicId == 0) revert NewTopicNoVoting();
        if (block.timestamp >= uint256(s.reportedAt) + votingWindow) revert VotingClosed();
        person = _personOf(voter);
        if (person == bytes32(0)) revert NotRegistered(voter);
        if (graph.registry().isBanned(voter)) revert Banned(voter);
        uint64 since = graph.personSince(s.topicId, person);
        if (since == 0 || since >= s.reportedAt) revert NotEligible();
        if (voteOf[subId][person] != VOTE_NONE) revert AlreadyVoted();
        if (person == _personOf(s.submitter)) revert SubmitterCannotVote();
    }

    function _personOf(address agent) internal view returns (bytes32) {
        return graph.registry().personOf(agent);
    }

    function _finalizable(Submission storage s) internal view returns (bool) {
        if (s.topicId == 0) return true; // new topic: verifier is the only voter
        if (block.timestamp >= uint256(s.reportedAt) + votingWindow) return true;
        uint256 threshold = s.eligibleVoters * quorumBps / Constants.BPS;
        return s.yes > threshold || s.no > threshold;
    }

    /// @dev Quorum is reached when at least quorumBps of the eligible voters voted.
    ///      Reached: majority decides, a tie goes to the verifier. Not reached: the verifier decides.
    function _outcome(Submission storage s) internal view returns (bool) {
        if (s.topicId == 0) return s.pass;
        uint256 votes = uint256(s.yes) + s.no;
        bool quorum = votes > 0 && votes * Constants.BPS >= s.eligibleVoters * quorumBps;
        if (!quorum || s.yes == s.no) return s.pass;
        return s.yes > s.no;
    }
}
