// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title EmissionsRegistry v2
 * @notice Immutable CO2 emission ledger on Hyperledger Besu (gas-free).
 *
 * Role separation (3 accounts):
 *   owner            — Account 1: deploys, authorises roles, no operational access
 *   emissionAgents   — Account 2: logEmission() + validateEmission()
 *   governanceAgents — Account 3: updateGovernanceStatus()
 */
contract EmissionsRegistry {

    // ── Types ─────────────────────────────────────────────────────────────────

    enum GovernanceStatus { Pending, Approved, Denied }

    uint8 public constant CONFIDENCE_THRESHOLD = 70;

    struct EmissionRecord {
        string           vehicleId;
        uint256          timestamp;
        uint256          co2Milligrams;
        string           fuelType;
        bytes32          dataHash;            // SHA-256 of raw CSV
        bool             validated;
        address          validator;
        uint8            agentConfidence;     // 0-100
        bool             requiresHumanReview;
        GovernanceStatus governanceStatus;
        address          governanceActor;
        string           agentDecision;       // LLM rationale (on-chain)
        string           pipelineMetadata;    // model fingerprint JSON
    }

    // ── State ─────────────────────────────────────────────────────────────────

    mapping(uint256 => EmissionRecord)  public records;
    uint256                             public recordCount;
    mapping(string => uint256[])        private _vehicleRecords;

    address                         public owner;
    mapping(address => bool)        public emissionAgents;    // Account 2
    mapping(address => bool)        public governanceAgents;  // Account 3

    // ── Events ────────────────────────────────────────────────────────────────

    event EmissionLogged(
        uint256 indexed recordId,
        string  indexed vehicleId,
        uint256 co2Milligrams,
        bytes32 dataHash,
        uint256 timestamp
    );
    event EmissionValidated(uint256 indexed recordId, address validator, bool approved);
    event GovernanceStatusUpdated(uint256 indexed recordId, GovernanceStatus status);
    event EmissionAgentAuthorized(address indexed agent);
    event GovernanceAgentAuthorized(address indexed agent);
    event EmissionAgentRevoked(address indexed agent);
    event GovernanceAgentRevoked(address indexed agent);

    // ── Modifiers ─────────────────────────────────────────────────────────────

    modifier onlyOwner() {
        require(msg.sender == owner, "Registry: not owner");
        _;
    }
    modifier onlyEmissionAgent() {
        require(emissionAgents[msg.sender] || msg.sender == owner, "Registry: not emission agent");
        _;
    }
    modifier onlyGovernanceAgent() {
        require(governanceAgents[msg.sender] || msg.sender == owner, "Registry: not governance agent");
        _;
    }

    // ── Constructor ───────────────────────────────────────────────────────────

    constructor() { owner = msg.sender; }

    // ── Role management ───────────────────────────────────────────────────────

    function authorizeEmissionAgent(address agent) external onlyOwner {
        emissionAgents[agent] = true;
        emit EmissionAgentAuthorized(agent);
    }
    function revokeEmissionAgent(address agent) external onlyOwner {
        emissionAgents[agent] = false;
        emit EmissionAgentRevoked(agent);
    }
    function authorizeGovernanceAgent(address agent) external onlyOwner {
        governanceAgents[agent] = true;
        emit GovernanceAgentAuthorized(agent);
    }
    function revokeGovernanceAgent(address agent) external onlyOwner {
        governanceAgents[agent] = false;
        emit GovernanceAgentRevoked(agent);
    }

    // ── Core: Account 2 ───────────────────────────────────────────────────────
    function logEmission(
        string  calldata vehicleId,
        uint256 co2Milligrams,
        string  calldata fuelType,
        bytes32 dataHash,
        uint8   agentConfidence,
        string  calldata agentDecision,
        string  calldata pipelineMetadata
    ) external onlyEmissionAgent returns (uint256 recordId) {
        recordId = recordCount;
        unchecked { recordCount++; }

        records[recordId] = EmissionRecord({
            vehicleId:           vehicleId,
            timestamp:           block.timestamp,
            co2Milligrams:       co2Milligrams,
            fuelType:            fuelType,
            dataHash:            dataHash,
            validated:           false,
            validator:           address(0),
            agentConfidence:     agentConfidence,
            requiresHumanReview: agentConfidence < CONFIDENCE_THRESHOLD,
            governanceStatus:    GovernanceStatus.Pending,
            governanceActor:     address(0),
            agentDecision:       agentDecision,
            pipelineMetadata:    pipelineMetadata
        });

        _vehicleRecords[vehicleId].push(recordId);
        emit EmissionLogged(recordId, vehicleId, co2Milligrams, dataHash, block.timestamp);
    }

    
    function validateEmission(uint256 recordId, bool approved) external onlyEmissionAgent {
        require(recordId < recordCount, "Registry: record not found");
        records[recordId].validated = approved;
        records[recordId].validator = msg.sender;
        emit EmissionValidated(recordId, msg.sender, approved);
    }

    // ── Core: Account 3 ───────────────────────────────────────────────────────

    
    function updateGovernanceStatus(uint256 recordId, GovernanceStatus status)
        external onlyGovernanceAgent
    {
        require(recordId < recordCount, "Registry: record not found");
        require(
            records[recordId].governanceStatus == GovernanceStatus.Pending,
            "Registry: already resolved"
        );
        records[recordId].governanceStatus = status;
        records[recordId].governanceActor  = msg.sender;
        emit GovernanceStatusUpdated(recordId, status);
    }

    // ── Views ─────────────────────────────────────────────────────────────────

    function getRecord(uint256 recordId) external view returns (EmissionRecord memory) {
        require(recordId < recordCount, "Registry: record not found");
        return records[recordId];
    }
    function getVehicleRecordIds(string calldata vehicleId) external view returns (uint256[] memory) {
        return _vehicleRecords[vehicleId];
    }
    function totalRecords() external view returns (uint256) { return recordCount; }
    function isPending(uint256 recordId) external view returns (bool) {
        require(recordId < recordCount, "Registry: record not found");
        return records[recordId].governanceStatus == GovernanceStatus.Pending;
    }
}
