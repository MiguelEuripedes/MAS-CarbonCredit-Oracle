const { expect } = require("chai");
const { ethers }  = require("hardhat");

/**
 * CarbonCredit (CCT) — ERC-721 Carbon Session Certificate
 * Test Suite — v2
 *
 * Covers:
 *  - Deployment and governance role
 *  - Minter role management (authorize, revoke, access control)
 *  - mintCertificate: happy path, field storage, events, access control
 *  - burn: happy path, non-owner cannot burn
 *  - ERC-721: transferFrom, safeTransferFrom, approve, getApproved
 *  - setApprovalForAll / isApprovedForAll
 *  - getTokensByOwner index maintenance (mint, transfer, burn)
 *  - ERC-165 supportsInterface
 *  - getCertificate view function
 *  - totalSupply counter
 *  - transferGovernance
 *  - Edge cases: mint to zero address, zero co2SavedMg, non-existent token
 */
describe("CarbonCredit (ERC-721)", function () {

  // ── Fixture ───────────────────────────────────────────────────────────────

  async function deployFixture() {
    const [governance, minter, alice, bob, stranger] = await ethers.getSigners();

    const CCT = await ethers.getContractFactory("CarbonCredit");
    const cct = await CCT.connect(governance).deploy();
    await cct.waitForDeployment();

    await cct.connect(governance).authorizeMinter(minter.address);

    // Helper: mint one certificate with sensible defaults
    const mint = (signer, to, overrides = {}) =>
      cct.connect(signer).mintCertificate(
        to,
        overrides.vehicleId          ?? "VIN-001",
        overrides.emissionRecordId   ?? 42n,
        overrides.co2SavedMg         ?? 50_000n,        // 50 g saved
        overrides.creditsEquivalentWei ?? ethers.parseEther("0.05"),
        overrides.reason             ?? "Saved 50g CO2 vs baseline",
      );

    return { cct, governance, minter, alice, bob, stranger, mint };
  }

  // ── Deployment ────────────────────────────────────────────────────────────

  describe("Deployment", function () {
    it("sets deployer as governance", async function () {
      const { cct, governance } = await deployFixture();
      expect(await cct.governance()).to.equal(governance.address);
    });

    it("has correct name and symbol", async function () {
      const { cct } = await deployFixture();
      expect(await cct.name()).to.equal("Carbon Credit Certificate");
      expect(await cct.symbol()).to.equal("CCT");
    });

    it("starts with zero totalSupply", async function () {
      const { cct } = await deployFixture();
      expect(await cct.totalSupply()).to.equal(0n);
    });

    it("authorised minter is set correctly", async function () {
      const { cct, minter } = await deployFixture();
      expect(await cct.authorizedMinters(minter.address)).to.be.true;
    });
  });

  // ── Minter role ───────────────────────────────────────────────────────────

  describe("Minter role", function () {
    it("governance can authorise a minter", async function () {
      const { cct, governance, stranger } = await deployFixture();
      await cct.connect(governance).authorizeMinter(stranger.address);
      expect(await cct.authorizedMinters(stranger.address)).to.be.true;
    });

    it("governance can revoke a minter", async function () {
      const { cct, governance, minter } = await deployFixture();
      await cct.connect(governance).revokeMinter(minter.address);
      expect(await cct.authorizedMinters(minter.address)).to.be.false;
    });

    it("stranger cannot authorise a minter", async function () {
      const { cct, stranger } = await deployFixture();
      await expect(
        cct.connect(stranger).authorizeMinter(stranger.address)
      ).to.be.revertedWith("CCT: not governance");
    });

    it("emits MinterAuthorized", async function () {
      const { cct, governance, stranger } = await deployFixture();
      await expect(cct.connect(governance).authorizeMinter(stranger.address))
        .to.emit(cct, "MinterAuthorized").withArgs(stranger.address);
    });

    it("emits MinterRevoked", async function () {
      const { cct, governance, minter } = await deployFixture();
      await expect(cct.connect(governance).revokeMinter(minter.address))
        .to.emit(cct, "MinterRevoked").withArgs(minter.address);
    });
  });

  // ── mintCertificate ───────────────────────────────────────────────────────

  describe("mintCertificate", function () {
    it("authorised minter can mint a certificate", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address);
      expect(await cct.totalSupply()).to.equal(1n);
      expect(await cct.balanceOf(alice.address)).to.equal(1n);
      expect(await cct.ownerOf(0n)).to.equal(alice.address);
    });

    it("stranger cannot mint", async function () {
      const { alice, stranger, mint } = await deployFixture();
      await expect(mint(stranger, alice.address))
        .to.be.revertedWith("CCT: not authorized minter");
    });

    it("reverts on mint to zero address", async function () {
      const { cct, minter } = await deployFixture();
      await expect(
        cct.connect(minter).mintCertificate(
          ethers.ZeroAddress, "VIN", 0n, 1n, 1n, "test"
        )
      ).to.be.revertedWith("CCT: mint to zero address");
    });

    it("reverts when co2SavedMg is zero", async function () {
      const { alice, minter, mint } = await deployFixture();
      await expect(mint(minter, alice.address, { co2SavedMg: 0n }))
        .to.be.revertedWith("CCT: co2SavedMg must be > 0");
    });

    it("stores all certificate fields correctly", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      const credWei = ethers.parseEther("0.05");
      await cct.connect(minter).mintCertificate(
        alice.address, "VIN-001", 42n, 50_000n, credWei, "Test rationale"
      );
      const cert = await cct.getCertificate(0n);
      expect(cert.vehicleId).to.equal("VIN-001");
      expect(cert.emissionRecordId).to.equal(42n);
      expect(cert.co2SavedMg).to.equal(50_000n);
      expect(cert.creditsEquivalentWei).to.equal(credWei);
      expect(cert.reason).to.equal("Test rationale");
      expect(cert.recipient).to.equal(alice.address);
      expect(cert.issuedAt).to.be.gt(0n);
    });

    it("assigns sequential token IDs", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address);
      await mint(minter, bob.address);
      expect(await cct.ownerOf(0n)).to.equal(alice.address);
      expect(await cct.ownerOf(1n)).to.equal(bob.address);
      expect(await cct.totalSupply()).to.equal(2n);
    });

    it("emits Transfer (mint) and CertificateMinted events", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      const credWei = ethers.parseEther("0.05");
      await expect(cct.connect(minter).mintCertificate(
        alice.address, "VIN-001", 42n, 50_000n, credWei, "reason"
      ))
        .to.emit(cct, "Transfer").withArgs(ethers.ZeroAddress, alice.address, 0n)
        .and.to.emit(cct, "CertificateMinted")
        .withArgs(0n, alice.address, "VIN-001", 42n, 50_000n, credWei);
    });

    it("getTokensByOwner returns correct IDs after multiple mints", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address);
      await mint(minter, alice.address);
      await mint(minter, alice.address);
      const ids = await cct.getTokensByOwner(alice.address);
      expect(ids.length).to.equal(3);
      expect(ids.map(Number)).to.deep.equal([0, 1, 2]);
    });
  });

  // ── burn ──────────────────────────────────────────────────────────────────

  describe("burn", function () {
    it("owner can burn their certificate", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address);
      await cct.connect(alice).burn(0n);
      expect(await cct.balanceOf(alice.address)).to.equal(0n);
      expect(await cct.totalSupply()).to.equal(1n); // supply counter not decremented
      await expect(cct.ownerOf(0n)).to.be.revertedWith("CCT: token does not exist");
    });

    it("stranger cannot burn another owner's certificate", async function () {
      const { cct, minter, alice, stranger, mint } = await deployFixture();
      await mint(minter, alice.address);
      await expect(cct.connect(stranger).burn(0n))
        .to.be.revertedWith("CCT: not owner or approved");
    });

    it("approved address can burn on behalf of owner", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address);
      await cct.connect(alice).approve(bob.address, 0n);
      await expect(cct.connect(bob).burn(0n)).not.to.be.reverted;
    });

    it("emits CertificateBurned and Transfer events", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address);
      await expect(cct.connect(alice).burn(0n))
        .to.emit(cct, "CertificateBurned").withArgs(0n, alice.address)
        .and.to.emit(cct, "Transfer").withArgs(alice.address, ethers.ZeroAddress, 0n);
    });

    it("getTokensByOwner is updated correctly after burn", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address); // token 0
      await mint(minter, alice.address); // token 1
      await mint(minter, alice.address); // token 2
      await cct.connect(alice).burn(1n); // burn middle token
      const ids = await cct.getTokensByOwner(alice.address);
      expect(ids.length).to.equal(2);
      // token 1 replaced by token 2 in the array (swap-and-pop pattern)
      expect(ids.map(Number).sort()).to.deep.equal([0, 2]);
    });
  });

  // ── ERC-721 transfers ─────────────────────────────────────────────────────

  describe("ERC-721: transferFrom", function () {
    it("owner can transfer their certificate", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address);
      await cct.connect(alice).transferFrom(alice.address, bob.address, 0n);
      expect(await cct.ownerOf(0n)).to.equal(bob.address);
      expect(await cct.balanceOf(alice.address)).to.equal(0n);
      expect(await cct.balanceOf(bob.address)).to.equal(1n);
    });

    it("approved address can transfer", async function () {
      const { cct, minter, alice, bob, stranger, mint } = await deployFixture();
      await mint(minter, alice.address);
      await cct.connect(alice).approve(stranger.address, 0n);
      await cct.connect(stranger).transferFrom(alice.address, bob.address, 0n);
      expect(await cct.ownerOf(0n)).to.equal(bob.address);
    });

    it("reverts transfer from non-owner", async function () {
      const { cct, minter, alice, bob, stranger, mint } = await deployFixture();
      await mint(minter, alice.address);
      await expect(
        cct.connect(stranger).transferFrom(alice.address, bob.address, 0n)
      ).to.be.revertedWith("CCT: not owner or approved");
    });

    it("reverts transfer to zero address", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address);
      await expect(
        cct.connect(alice).transferFrom(alice.address, ethers.ZeroAddress, 0n)
      ).to.be.revertedWith("CCT: transfer to zero address");
    });

    it("clears approval after transfer", async function () {
      const { cct, minter, alice, bob, stranger, mint } = await deployFixture();
      await mint(minter, alice.address);
      await cct.connect(alice).approve(stranger.address, 0n);
      await cct.connect(alice).transferFrom(alice.address, bob.address, 0n);
      expect(await cct.getApproved(0n)).to.equal(ethers.ZeroAddress);
    });

    it("emits Transfer event", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address);
      await expect(cct.connect(alice).transferFrom(alice.address, bob.address, 0n))
        .to.emit(cct, "Transfer").withArgs(alice.address, bob.address, 0n);
    });

    it("getTokensByOwner is updated correctly after transfer", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address); // 0
      await mint(minter, alice.address); // 1
      await cct.connect(alice).transferFrom(alice.address, bob.address, 0n);
      const aliceIds = await cct.getTokensByOwner(alice.address);
      const bobIds   = await cct.getTokensByOwner(bob.address);
      expect(aliceIds.map(Number)).to.deep.equal([1]);
      expect(bobIds.map(Number)).to.deep.equal([0]);
    });
  });

  // ── approve / setApprovalForAll ───────────────────────────────────────────

  describe("Approval", function () {
    it("owner can approve another address for a token", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address);
      await cct.connect(alice).approve(bob.address, 0n);
      expect(await cct.getApproved(0n)).to.equal(bob.address);
    });

    it("emits Approval event", async function () {
      const { cct, minter, alice, bob, mint } = await deployFixture();
      await mint(minter, alice.address);
      await expect(cct.connect(alice).approve(bob.address, 0n))
        .to.emit(cct, "Approval").withArgs(alice.address, bob.address, 0n);
    });

    it("setApprovalForAll lets operator manage all tokens", async function () {
      const { cct, minter, alice, bob, stranger, mint } = await deployFixture();
      await mint(minter, alice.address);
      await mint(minter, alice.address);
      await cct.connect(alice).setApprovalForAll(bob.address, true);
      expect(await cct.isApprovedForAll(alice.address, bob.address)).to.be.true;
      await cct.connect(bob).transferFrom(alice.address, stranger.address, 0n);
      await cct.connect(bob).transferFrom(alice.address, stranger.address, 1n);
      expect(await cct.balanceOf(alice.address)).to.equal(0n);
    });

    it("reverts setApprovalForAll to self", async function () {
      const { cct, alice } = await deployFixture();
      await expect(
        cct.connect(alice).setApprovalForAll(alice.address, true)
      ).to.be.revertedWith("CCT: approve to caller");
    });

    it("emits ApprovalForAll event", async function () {
      const { cct, alice, bob } = await deployFixture();
      await expect(cct.connect(alice).setApprovalForAll(bob.address, true))
        .to.emit(cct, "ApprovalForAll").withArgs(alice.address, bob.address, true);
    });
  });

  // ── ERC-165 ───────────────────────────────────────────────────────────────

  describe("ERC-165 supportsInterface", function () {
    it("supports ERC-721 interface", async function () {
      const { cct } = await deployFixture();
      expect(await cct.supportsInterface("0x80ac58cd")).to.be.true;
    });

    it("supports ERC-721Metadata interface", async function () {
      const { cct } = await deployFixture();
      expect(await cct.supportsInterface("0x5b5e139f")).to.be.true;
    });

    it("supports ERC-165 interface", async function () {
      const { cct } = await deployFixture();
      expect(await cct.supportsInterface("0x01ffc9a7")).to.be.true;
    });

    it("does not support random interface", async function () {
      const { cct } = await deployFixture();
      expect(await cct.supportsInterface("0xdeadbeef")).to.be.false;
    });
  });

  // ── View functions ────────────────────────────────────────────────────────

  describe("View functions", function () {
    it("ownerOf reverts on non-existent token", async function () {
      const { cct } = await deployFixture();
      await expect(cct.ownerOf(99n)).to.be.revertedWith("CCT: token does not exist");
    });

    it("getCertificate reverts on non-existent token", async function () {
      const { cct } = await deployFixture();
      await expect(cct.getCertificate(99n)).to.be.revertedWith("CCT: token does not exist");
    });

    it("balanceOf reverts on zero address", async function () {
      const { cct } = await deployFixture();
      await expect(cct.balanceOf(ethers.ZeroAddress))
        .to.be.revertedWith("CCT: balance query for zero address");
    });

    it("getTokensByOwner returns empty for address with no tokens", async function () {
      const { cct, stranger } = await deployFixture();
      const ids = await cct.getTokensByOwner(stranger.address);
      expect(ids.length).to.equal(0);
    });

    it("totalSupply increments per mint (not decremented by burn)", async function () {
      const { cct, minter, alice, mint } = await deployFixture();
      await mint(minter, alice.address);
      await mint(minter, alice.address);
      expect(await cct.totalSupply()).to.equal(2n);
      await cct.connect(alice).burn(0n);
      // totalSupply is a counter (_tokenCounter), not a live supply
      expect(await cct.totalSupply()).to.equal(2n);
    });
  });

  // ── transferGovernance ────────────────────────────────────────────────────

  describe("transferGovernance", function () {
    it("governance can transfer role", async function () {
      const { cct, governance, alice } = await deployFixture();
      await cct.connect(governance).transferGovernance(alice.address);
      expect(await cct.governance()).to.equal(alice.address);
    });

    it("reverts on zero address", async function () {
      const { cct, governance } = await deployFixture();
      await expect(
        cct.connect(governance).transferGovernance(ethers.ZeroAddress)
      ).to.be.revertedWith("CCT: zero address");
    });

    it("stranger cannot transfer governance", async function () {
      const { cct, stranger, alice } = await deployFixture();
      await expect(
        cct.connect(stranger).transferGovernance(alice.address)
      ).to.be.revertedWith("CCT: not governance");
    });

    it("emits GovernanceTransferred", async function () {
      const { cct, governance, alice } = await deployFixture();
      await expect(cct.connect(governance).transferGovernance(alice.address))
        .to.emit(cct, "GovernanceTransferred")
        .withArgs(governance.address, alice.address);
    });
  });
});
