// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title CarbonCredit (CCT) — ERC-721 Carbon Session Certificate
 * @notice Non-fungible token representing a unique driving session's CO2 savings.
 *
 * Design rationale (ERC-721 vs ERC-20):
 *   Each driving session is a unique event — vehicle A at 09:00 is not the same
 *   as vehicle B at 14:00, even if they save the same CO2 mass. ERC-721 preserves
 *   this provenance chain: one session → one certificate → one immutable record.
 *
 * Each token (CarbonCertificate) stores:
 *   - vehicleId              off-chain vehicle identifier
 *   - emissionRecordId       linked EmissionsRegistry record (audit link)
 *   - co2SavedMg             milligrams of CO2 saved vs the session baseline
 *   - creditsEquivalentWei   credits value in wei units (18 decimals) for reference
 *   - reason                 governance rationale (on-chain, written by LLM agent)
 *   - issuedAt               block timestamp
 *   - recipient              original recipient wallet
 *
 * Role separation:
 *   governance     — Account 1: authorises minters, transfers governance
 *   authorizedMinters — Account 3: calls mintCertificate()
 *
 * ERC-721 standard implemented from scratch (no OpenZeppelin dependency).
 * Implements ERC-165 supportsInterface for tooling compatibility.
 */
contract CarbonCredit {

    // ── ERC-721 metadata ──────────────────────────────────────────────────────

    string public constant name   = "Carbon Credit Certificate";
    string public constant symbol = "CCT";

    // ── Certificate data ──────────────────────────────────────────────────────

    struct CarbonCertificate {
        string  vehicleId;
        uint256 emissionRecordId;
        uint256 co2SavedMg;             // milligrams saved vs baseline
        uint256 creditsEquivalentWei;   // 18-decimal reference value
        string  reason;                 // governance rationale
        uint256 issuedAt;
        address recipient;
    }

    // ── State ─────────────────────────────────────────────────────────────────

    uint256 private _tokenCounter;

    mapping(uint256 => address)           private _owners;
    mapping(address => uint256)           private _balances;
    mapping(uint256 => address)           private _tokenApprovals;
    mapping(address => mapping(address => bool)) private _operatorApprovals;
    mapping(uint256 => CarbonCertificate) private _certificates;

    // Index: owner address → list of owned token IDs (for portfolio queries)
    mapping(address => uint256[]) private _ownedTokens;
    // Index: tokenId → position in _ownedTokens[owner]
    mapping(uint256 => uint256)   private _ownedTokensIndex;

    address                   public governance;
    mapping(address => bool)  public authorizedMinters;

    // ── Events ────────────────────────────────────────────────────────────────

    // ERC-721 standard events
    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);

    // Carbon-specific events
    event CertificateMinted(
        uint256 indexed tokenId,
        address indexed recipient,
        string  vehicleId,
        uint256 emissionRecordId,
        uint256 co2SavedMg,
        uint256 creditsEquivalentWei
    );
    event CertificateBurned(uint256 indexed tokenId, address indexed owner);
    event GovernanceTransferred(address indexed previous, address indexed next);
    event MinterAuthorized(address indexed minter);
    event MinterRevoked(address indexed minter);

    // ── Modifiers ─────────────────────────────────────────────────────────────

    modifier onlyGovernance() {
        require(msg.sender == governance, "CCT: not governance");
        _;
    }

    modifier onlyMinter() {
        require(authorizedMinters[msg.sender], "CCT: not authorized minter");
        _;
    }

    // ── Constructor ───────────────────────────────────────────────────────────

    constructor() {
        governance = msg.sender;
    }

    // ── Role management (Account 1) ───────────────────────────────────────────

    function authorizeMinter(address minter) external onlyGovernance {
        authorizedMinters[minter] = true;
        emit MinterAuthorized(minter);
    }

    function revokeMinter(address minter) external onlyGovernance {
        authorizedMinters[minter] = false;
        emit MinterRevoked(minter);
    }

    function transferGovernance(address newGovernance) external onlyGovernance {
        require(newGovernance != address(0), "CCT: zero address");
        emit GovernanceTransferred(governance, newGovernance);
        governance = newGovernance;
    }

    // ── Minting (Account 3) ───────────────────────────────────────────────────

    function mintCertificate(
        address to,
        string  calldata vehicleId,
        uint256 emissionRecordId,
        uint256 co2SavedMg,
        uint256 creditsEquivalentWei,
        string  calldata reason
    ) external onlyMinter returns (uint256 tokenId) {
        require(to != address(0), "CCT: mint to zero address");
        require(co2SavedMg > 0,   "CCT: co2SavedMg must be > 0");

        tokenId = _tokenCounter;
        unchecked { _tokenCounter++; }

        _owners[tokenId]   = to;
        _balances[to]     += 1;

        _ownedTokensIndex[tokenId] = _ownedTokens[to].length;
        _ownedTokens[to].push(tokenId);

        _certificates[tokenId] = CarbonCertificate({
            vehicleId:            vehicleId,
            emissionRecordId:     emissionRecordId,
            co2SavedMg:           co2SavedMg,
            creditsEquivalentWei: creditsEquivalentWei,
            reason:               reason,
            issuedAt:             block.timestamp,
            recipient:            to
        });

        emit Transfer(address(0), to, tokenId);
        emit CertificateMinted(
            tokenId, to, vehicleId, emissionRecordId, co2SavedMg, creditsEquivalentWei
        );
    }

    // ── Burning ───────────────────────────────────────────────────────────────

    function burn(uint256 tokenId) external {
        address owner = _requireOwned(tokenId);
        require(
            msg.sender == owner ||
            _tokenApprovals[tokenId] == msg.sender ||
            _operatorApprovals[owner][msg.sender],
            "CCT: not owner or approved"
        );
        _burn(tokenId, owner);
        emit CertificateBurned(tokenId, owner);
    }

    // ── ERC-721 standard ──────────────────────────────────────────────────────

    function balanceOf(address owner) external view returns (uint256) {
        require(owner != address(0), "CCT: balance query for zero address");
        return _balances[owner];
    }

    function ownerOf(uint256 tokenId) external view returns (address) {
        return _requireOwned(tokenId);
    }

    function approve(address to, uint256 tokenId) external {
        address owner = _requireOwned(tokenId);
        require(
            msg.sender == owner || _operatorApprovals[owner][msg.sender],
            "CCT: not owner or operator"
        );
        _tokenApprovals[tokenId] = to;
        emit Approval(owner, to, tokenId);
    }

    function getApproved(uint256 tokenId) external view returns (address) {
        _requireOwned(tokenId);
        return _tokenApprovals[tokenId];
    }

    function setApprovalForAll(address operator, bool approved) external {
        require(operator != msg.sender, "CCT: approve to caller");
        _operatorApprovals[msg.sender][operator] = approved;
        emit ApprovalForAll(msg.sender, operator, approved);
    }

    function isApprovedForAll(address owner, address operator) external view returns (bool) {
        return _operatorApprovals[owner][operator];
    }

    function transferFrom(address from, address to, uint256 tokenId) external {
        _transfer(from, to, tokenId);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId) external {
        _transfer(from, to, tokenId);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId, bytes calldata) external {
        _transfer(from, to, tokenId);
    }

    // ── ERC-165 ───────────────────────────────────────────────────────────────

    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return
            interfaceId == 0x80ac58cd || // ERC-721
            interfaceId == 0x5b5e139f || // ERC-721Metadata
            interfaceId == 0x01ffc9a7;   // ERC-165
    }

    // ── Views ─────────────────────────────────────────────────────────────────

    function getCertificate(uint256 tokenId)
        external view returns (CarbonCertificate memory)
    {
        _requireOwned(tokenId);
        return _certificates[tokenId];
    }

    function getTokensByOwner(address owner)
        external view returns (uint256[] memory)
    {
        return _ownedTokens[owner];
    }

    function totalSupply() external view returns (uint256) {
        return _tokenCounter;
    }

    // ── Internal helpers ──────────────────────────────────────────────────────

    function _requireOwned(uint256 tokenId) internal view returns (address owner) {
        owner = _owners[tokenId];
        require(owner != address(0), "CCT: token does not exist");
    }

    function _transfer(address from, address to, uint256 tokenId) internal {
        address owner = _requireOwned(tokenId);
        require(owner == from, "CCT: transfer from non-owner");
        require(to != address(0), "CCT: transfer to zero address");
        require(
            msg.sender == owner ||
            _tokenApprovals[tokenId] == msg.sender ||
            _operatorApprovals[owner][msg.sender],
            "CCT: not owner or approved"
        );

        // Clear approval
        delete _tokenApprovals[tokenId];

        // Update owned-tokens index
        _removeTokenFromOwner(from, tokenId);
        _ownedTokensIndex[tokenId] = _ownedTokens[to].length;
        _ownedTokens[to].push(tokenId);

        _balances[from] -= 1;
        _balances[to]   += 1;
        _owners[tokenId] = to;

        emit Transfer(from, to, tokenId);
    }

    function _burn(uint256 tokenId, address owner) internal {
        delete _tokenApprovals[tokenId];
        _removeTokenFromOwner(owner, tokenId);
        _balances[owner] -= 1;
        delete _owners[tokenId];
        delete _certificates[tokenId];
        emit Transfer(owner, address(0), tokenId);
    }

    function _removeTokenFromOwner(address owner, uint256 tokenId) internal {
        uint256 lastIndex = _ownedTokens[owner].length - 1;
        uint256 tokenIndex = _ownedTokensIndex[tokenId];

        if (tokenIndex != lastIndex) {
            uint256 lastTokenId = _ownedTokens[owner][lastIndex];
            _ownedTokens[owner][tokenIndex] = lastTokenId;
            _ownedTokensIndex[lastTokenId]  = tokenIndex;
        }

        _ownedTokens[owner].pop();
        delete _ownedTokensIndex[tokenId];
    }
}
