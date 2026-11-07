// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC1155} from "@openzeppelin/contracts/token/ERC1155/ERC1155.sol";
import {Address} from "@openzeppelin/contracts/utils/Address.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IAccess} from "./interfaces/ILore.sol";
import {LoreBase} from "./LoreBase.sol";
import {LoreRoles} from "./LoreRoles.sol";
import {TopicGraph} from "./TopicGraph.sol";
import {Royalties} from "./Royalties.sol";
import {Constants} from "./Constants.sol";

/// @notice Soulbound ERC-1155 access pass, token id = topicId. Buying is the only way to get access.
///         Access is permanent and covers nodes added to the topic later.
///
///         A purchase costs the base price (nodeValue x nodes), paid to the topic's contributors as royalties.
///         Anything sent above it is refunded.
contract Access is IAccess, ERC1155, LoreBase, ReentrancyGuard {
    TopicGraph public immutable graph;
    Royalties public immutable royalties;

    /// @notice Base price per node, in 0G (wei). It goes to the topic's contributors as royalties.
    uint256 public nodeValue = Constants.NODE_VALUE;

    event ParamsSet(uint256 nodeValue);
    event Paid(uint256 indexed topicId, address indexed buyer, uint256 base, uint256 refund);

    error Soulbound();
    error Banned(address wallet);
    error AlreadyHeld(uint256 topicId);
    error UnknownTopic(uint256 topicId);
    error Underpaid(uint256 needed, uint256 sent);

    constructor(LoreRoles roles_, TopicGraph graph_, Royalties royalties_) ERC1155("") LoreBase(roles_) {
        if (address(graph_) == address(0) || address(royalties_) == address(0)) revert ZeroAddress();
        graph = graph_;
        royalties = royalties_;
    }

    function setParams(uint256 nodeValue_) external onlyAdmin {
        nodeValue = nodeValue_;
        emit ParamsSet(nodeValue_);
    }

    function setURI(string calldata newuri) external onlyAdmin {
        _setURI(newuri);
    }

    /// @inheritdoc IAccess
    function priceOf(uint256 topicId) public view returns (uint256) {
        return nodeValue * graph.nodeCount(topicId);
    }

    /// @inheritdoc IAccess
    function buy(uint256 topicId) external payable nonReentrant {
        if (!graph.topicExists(topicId)) revert UnknownTopic(topicId);
        if (balanceOf(msg.sender, topicId) != 0) revert AlreadyHeld(topicId);
        if (graph.registry().isBanned(msg.sender)) revert Banned(msg.sender);

        uint256 base = priceOf(topicId);
        if (msg.value < base) revert Underpaid(base, msg.value);
        royalties.distribute{value: base}(topicId, base);

        uint256 refund = msg.value - base;
        // Mint before refunding: the balance is set before any external call that could re-enter (nonReentrant too).
        _mint(msg.sender, topicId, 1, "");
        emit Bought(topicId, msg.sender);
        emit Paid(topicId, msg.sender, base, refund);
        if (refund > 0) Address.sendValue(payable(msg.sender), refund);
    }

    /// @inheritdoc IAccess
    function hasAccess(uint256 topicId, address a) external view returns (bool) {
        return balanceOf(a, topicId) != 0 && !graph.registry().isBanned(a);
    }

    // ---------- soulbound ----------

    /// @dev Minting (from == 0) is the only allowed balance change. Transfers and burns revert.
    function _update(address from, address to, uint256[] memory ids, uint256[] memory values) internal override {
        if (from != address(0)) revert Soulbound();
        super._update(from, to, ids, values);
    }

    function setApprovalForAll(address, bool) public pure override {
        revert Soulbound();
    }
}
