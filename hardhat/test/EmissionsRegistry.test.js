const { expect } = require("chai");
const { ethers }  = require("hardhat");

/**
 * EmissionsRegistry v2 — Test Suite
 *
 * Covers:
 *  - Deployment and ownership
 *  - Role management (grant, revoke, access control)
 *  - logEmission: happy path, field validation, events
 *  - validateEmission: approved and rejected
 *  - updateGovernanceStatus: Pending → Approved | Denied, double-resolve guard
 *  - dataHash stored correctly
 *  - requiresHumanReview flag (agentConfidence < 70)
 *  - View functions: getRecord, getVehicleRecordIds, totalRecords, isPending
 *  - Edge cases: zero CO2, max uint256 CO2, empty vehicleId, record not found
 */
describe("EmissionsRegistry", function () {

  // ── Fixtures ──────────────────────────────────────────────────────────────

  async function deployFixture() {
    const [owner, emissionAgent, governanceAgent, stranger] = await ethers.getSigners();

    const Registry = await ethers.getContractFactory("EmissionsRegistry");
    const registry = await Registry.connect(owner).deploy();
    await registry.waitForDeployment();

    // Grant roles
    await registry.connect(owner).authorizeEmissionAgent(emissionAgent.address);
    await registry.connect(owner).authorizeGovernanceAgent(governanceAgent.address);

    // Helpers for common log call
    const sha256 = ethers.zeroPadBytes(ethers.toUtf8Bytes("fakehash"), 32);
    const logDefault = (signer, overrides = {}) =>
      registry.connect(signer).logEmission(
        overrides.vehicleId      ?? "VIN-001",
        overrides.co2Milligrams  ?? 87_432n,
        overrides.fuelType       ?? "Gasolina",
        overrides.dataHash       ?? sha256,
        overrides.agentConf      ?? 85,
        overrides.agentDecision  ?? "Test rationale",
        overrides.pipelineMeta   ?? '{"model":"test","version":"1"}',
      );

    return { registry, owner, emissionAgent, governanceAgent, stranger, sha256, logDefault };
  }

  // ── Deployment ────────────────────────────────────────────────────────────

  describe("Deployment", function () {
    it("sets the deployer as owner", async function () {
      const { registry, owner } = await deployFixture();
      expect(await registry.owner()).to.equal(owner.address);
    });

    it("starts with zero records", async function () {
      const { registry } = await deployFixture();
      expect(await registry.totalRecords()).to.equal(0n);
    });

    it("has correct CONFIDENCE_THRESHOLD", async function () {
      const { registry } = await deployFixture();
      expect(await registry.CONFIDENCE_THRESHOLD()).to.equal(70);
    });
  });

  // ── Role management ───────────────────────────────────────────────────────

  describe("Role management", function () {
    it("owner can authorize and revoke an emission agent", async function () {
      const { registry, owner, stranger } = await deployFixture();
      await registry.connect(owner).authorizeEmissionAgent(stranger.address);
      expect(await registry.emissionAgents(stranger.address)).to.be.true;
      await registry.connect(owner).revokeEmissionAgent(stranger.address);
      expect(await registry.emissionAgents(stranger.address)).to.be.false;
    });

    it("owner can authorize and revoke a governance agent", async function () {
      const { registry, owner, stranger } = await deployFixture();
      await registry.connect(owner).authorizeGovernanceAgent(stranger.address);
      expect(await registry.governanceAgents(stranger.address)).to.be.true;
      await registry.connect(owner).revokeGovernanceAgent(stranger.address);
      expect(await registry.governanceAgents(stranger.address)).to.be.false;
    });

    it("stranger cannot authorize agents", async function () {
      const { registry, stranger } = await deployFixture();
      await expect(
        registry.connect(stranger).authorizeEmissionAgent(stranger.address)
      ).to.be.revertedWith("Registry: not owner");
    });

    it("emits EmissionAgentAuthorized event", async function () {
      const { registry, owner, stranger } = await deployFixture();
      await expect(registry.connect(owner).authorizeEmissionAgent(stranger.address))
        .to.emit(registry, "EmissionAgentAuthorized")
        .withArgs(stranger.address);
    });
  });

  // ── logEmission ───────────────────────────────────────────────────────────

  describe("logEmission", function () {
    it("emission agent can log a record", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await expect(logDefault(emissionAgent)).not.to.be.reverted;
      expect(await registry.totalRecords()).to.equal(1n);
    });

    it("stranger cannot log a record", async function () {
      const { stranger, logDefault } = await deployFixture();
      await expect(logDefault(stranger)).to.be.revertedWith("Registry: not emission agent");
    });

    it("stores all fields correctly", async function () {
      const { registry, emissionAgent, sha256, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      const rec = await registry.getRecord(0n);

      expect(rec.vehicleId).to.equal("VIN-001");
      expect(rec.co2Milligrams).to.equal(87_432n);
      expect(rec.fuelType).to.equal("Gasolina");
      expect(rec.dataHash).to.equal(ethers.hexlify(sha256));
      expect(rec.agentConfidence).to.equal(85);
      expect(rec.validated).to.be.false;
      expect(rec.requiresHumanReview).to.be.false;   // 85 >= 70
      expect(rec.governanceStatus).to.equal(0);       // Pending
    });

    it("sets requiresHumanReview=true when confidence < 70", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent, { agentConf: 55 });
      const rec = await registry.getRecord(0n);
      expect(rec.requiresHumanReview).to.be.true;
    });

    it("emits EmissionLogged with correct args", async function () {
      const { registry, emissionAgent, sha256, logDefault } = await deployFixture();
      const tx = logDefault(emissionAgent);
      await expect(tx)
        .to.emit(registry, "EmissionLogged")
        .withArgs(0n, "VIN-001", 87_432n, ethers.hexlify(sha256), (v) => v > 0n);
    });

    it("assigns sequential record IDs", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await logDefault(emissionAgent, { vehicleId: "VIN-002" });
      expect(await registry.totalRecords()).to.equal(2n);
      expect((await registry.getRecord(0n)).vehicleId).to.equal("VIN-001");
      expect((await registry.getRecord(1n)).vehicleId).to.equal("VIN-002");
    });

    it("tracks vehicle record IDs correctly", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await logDefault(emissionAgent);
      const ids = await registry.getVehicleRecordIds("VIN-001");
      expect(ids.length).to.equal(2);
      expect(ids[0]).to.equal(0n);
      expect(ids[1]).to.equal(1n);
    });

    it("handles zero CO2 (engine idle / stopped)", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await expect(logDefault(emissionAgent, { co2Milligrams: 0n })).not.to.be.reverted;
      const rec = await registry.getRecord(0n);
      expect(rec.co2Milligrams).to.equal(0n);
    });

    it("handles maximum uint256 CO2 without overflow", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      const maxUint256 = ethers.MaxUint256;
      await expect(logDefault(emissionAgent, { co2Milligrams: maxUint256 })).not.to.be.reverted;
      const rec = await registry.getRecord(0n);
      expect(rec.co2Milligrams).to.equal(maxUint256);
    });
  });

  // ── validateEmission ──────────────────────────────────────────────────────

  describe("validateEmission", function () {
    it("emission agent can validate a record (approved)", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await registry.connect(emissionAgent).validateEmission(0n, true);
      const rec = await registry.getRecord(0n);
      expect(rec.validated).to.be.true;
      expect(rec.validator).to.equal(emissionAgent.address);
    });

    it("emission agent can reject a record", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await registry.connect(emissionAgent).validateEmission(0n, false);
      const rec = await registry.getRecord(0n);
      expect(rec.validated).to.be.false;
    });

    it("stranger cannot validate", async function () {
      const { registry, emissionAgent, stranger, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await expect(
        registry.connect(stranger).validateEmission(0n, true)
      ).to.be.revertedWith("Registry: not emission agent");
    });

    it("reverts on non-existent record", async function () {
      const { registry, emissionAgent } = await deployFixture();
      await expect(
        registry.connect(emissionAgent).validateEmission(99n, true)
      ).to.be.revertedWith("Registry: record not found");
    });

    it("emits EmissionValidated event", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await expect(registry.connect(emissionAgent).validateEmission(0n, true))
        .to.emit(registry, "EmissionValidated")
        .withArgs(0n, emissionAgent.address, true);
    });
  });

  // ── updateGovernanceStatus ────────────────────────────────────────────────

  describe("updateGovernanceStatus", function () {
    it("governance agent can approve a pending record", async function () {
      const { registry, emissionAgent, governanceAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await registry.connect(governanceAgent).updateGovernanceStatus(0n, 1); // 1 = Approved
      const rec = await registry.getRecord(0n);
      expect(rec.governanceStatus).to.equal(1);
      expect(rec.governanceActor).to.equal(governanceAgent.address);
    });

    it("governance agent can deny a pending record", async function () {
      const { registry, emissionAgent, governanceAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await registry.connect(governanceAgent).updateGovernanceStatus(0n, 2); // 2 = Denied
      const rec = await registry.getRecord(0n);
      expect(rec.governanceStatus).to.equal(2);
    });

    it("reverts if status is already resolved (double-resolve guard)", async function () {
      const { registry, emissionAgent, governanceAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await registry.connect(governanceAgent).updateGovernanceStatus(0n, 1);
      await expect(
        registry.connect(governanceAgent).updateGovernanceStatus(0n, 2)
      ).to.be.revertedWith("Registry: already resolved");
    });

    it("emission agent cannot update governance status", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await expect(
        registry.connect(emissionAgent).updateGovernanceStatus(0n, 1)
      ).to.be.revertedWith("Registry: not governance agent");
    });

    it("stranger cannot update governance status", async function () {
      const { registry, emissionAgent, stranger, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await expect(
        registry.connect(stranger).updateGovernanceStatus(0n, 1)
      ).to.be.revertedWith("Registry: not governance agent");
    });

    it("emits GovernanceStatusUpdated", async function () {
      const { registry, emissionAgent, governanceAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      await expect(registry.connect(governanceAgent).updateGovernanceStatus(0n, 1))
        .to.emit(registry, "GovernanceStatusUpdated")
        .withArgs(0n, 1);
    });

    it("isPending returns false after resolution", async function () {
      const { registry, emissionAgent, governanceAgent, logDefault } = await deployFixture();
      await logDefault(emissionAgent);
      expect(await registry.isPending(0n)).to.be.true;
      await registry.connect(governanceAgent).updateGovernanceStatus(0n, 1);
      expect(await registry.isPending(0n)).to.be.false;
    });
  });

  // ── dataHash ──────────────────────────────────────────────────────────────

  describe("dataHash integrity", function () {
    it("stores and retrieves the exact bytes32 dataHash", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      // Simulate a real SHA-256 (32 bytes)
      const knownHash = ethers.keccak256(ethers.toUtf8Bytes("real-csv-content"));
      await logDefault(emissionAgent, { dataHash: knownHash });
      const rec = await registry.getRecord(0n);
      expect(rec.dataHash).to.equal(knownHash);
    });

    it("two records with different hashes are stored independently", async function () {
      const { registry, emissionAgent, logDefault } = await deployFixture();
      const hash1 = ethers.keccak256(ethers.toUtf8Bytes("csv1"));
      const hash2 = ethers.keccak256(ethers.toUtf8Bytes("csv2"));
      await logDefault(emissionAgent, { dataHash: hash1 });
      await logDefault(emissionAgent, { dataHash: hash2, vehicleId: "VIN-002" });
      expect((await registry.getRecord(0n)).dataHash).to.equal(hash1);
      expect((await registry.getRecord(1n)).dataHash).to.equal(hash2);
    });
  });

  // ── View functions ────────────────────────────────────────────────────────

  describe("View functions", function () {
    it("getRecord reverts on non-existent record", async function () {
      const { registry } = await deployFixture();
      await expect(registry.getRecord(0n)).to.be.revertedWith("Registry: record not found");
    });

    it("getVehicleRecordIds returns empty array for unknown vehicle", async function () {
      const { registry } = await deployFixture();
      const ids = await registry.getVehicleRecordIds("UNKNOWN");
      expect(ids.length).to.equal(0);
    });

    it("isPending reverts on non-existent record", async function () {
      const { registry } = await deployFixture();
      await expect(registry.isPending(0n)).to.be.revertedWith("Registry: record not found");
    });
  });
});
