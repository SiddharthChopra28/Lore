// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// Frozen interfaces from BUILD_PLAN.md section 3.2. Changing anything here needs P1, P2 and P3 to approve.
// Implementations may add extra functions (views, wiring, admin setters); they never change these.

interface ITopicGraph {
    struct Node {
        uint256 topicId;
        address contributor;
        bytes32 contentHash;
        bytes32 storageRoot;
        bytes32 metaHash;
        uint64 acceptedAt;
    }

    event TopicCreated(uint256 indexed topicId);
    event NodeAdded(uint256 indexed topicId, uint256 indexed nodeId, address indexed contributor, uint256[] parents);
    event NodeMetaSet(uint256 indexed nodeId, bytes32 metaHash);
    event TopicMetaSet(uint256 indexed topicId, bytes32 metaHash);

    function addNode(
        uint256 topicId,
        address contributor,
        bytes32 contentHash,
        bytes32 storageRoot,
        uint256[] calldata parents
    ) external returns (uint256 topicIdOut, uint256 nodeId); // only Submissions; topicId 0 => creates topic
    function setNodeMeta(uint256 nodeId, bytes32 metaHash) external; // only CURATOR
    function setTopicMeta(uint256 topicId, bytes32 metaHash) external; // only CURATOR
    function nodeCount(uint256 topicId) external view returns (uint256);
    function contributorSince(uint256 topicId, address a) external view returns (uint64); // 0 = not a contributor
    function contributorCount(uint256 topicId) external view returns (uint256);
    function personSince(uint256 topicId, bytes32 person) external view returns (uint64); // 0 = not a contributor
    function personCount(uint256 topicId) external view returns (uint256); // distinct people; the voter base
    function parentsOf(uint256 nodeId) external view returns (uint256[] memory);
}

interface ISubmissions {
    enum Status {
        Pending,
        Reported,
        Accepted,
        Rejected
    }

    event Submitted(
        uint256 indexed subId, uint256 indexed topicId, address indexed submitter, bytes32 contentHash, bytes32 storageRoot
    );
    event Reported(uint256 indexed subId, bool pass, uint256[] parents, bytes32 reportHash, uint256 eligibleVoters);
    event Voted(uint256 indexed subId, address indexed voter, bytes32 indexed person, bool approve); // one vote per person
    event Finalized(uint256 indexed subId, bool accepted, uint256 nodeId);

    function submit(uint256 topicId, bytes32 contentHash, bytes32 storageRoot) external payable returns (uint256 subId); // msg.value = stake (0G); needs a linked author token; topicId 0 = new topic
    function postReport(uint256 subId, bool pass, uint256[] calldata parents, bytes32 reportHash) external; // only VERIFIER, once
    function vote(uint256 subId, bool approve) external;
    function finalize(uint256 subId) external; // callable by anyone
}

interface IAccess {
    // ERC1155, id = topicId
    event Bought(uint256 indexed topicId, address indexed buyer);

    function buy(uint256 topicId) external payable; // msg.value = max 0G: base to Royalties, refunds the rest; reverts if held or banned
    function priceOf(uint256 topicId) external view returns (uint256); // the base price: nodeValue x the topic's node count
    function hasAccess(uint256 topicId, address a) external view returns (bool);
}

interface IRoyalties {
    function distribute(uint256 topicId, uint256 amount) external payable; // only Access, msg.value == amount
    function claimable(uint256 nodeId) external view returns (uint256);
    function claim(uint256[] calldata nodeIds) external; // pays msg.sender for nodes they contributed
}

interface IReports {
    event LeakReported(uint256 indexed reportId, uint256 indexed topicId, address reporter, bytes32 evidenceHash);
    event LeakMatched(uint256 indexed reportId, address indexed wallet, uint16 scoreBps);

    function report(uint256 topicId, bytes32 evidenceHash) external returns (uint256);
    function recordMatch(uint256 reportId, address wallet, uint16 scoreBps) external; // only LEAK_DETECTOR
}
