require("@nomicfoundation/hardhat-toolbox");

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.19",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      viaIR: true,
    },
  },
  networks: {
    // Local Hardhat network (used for tests — fast, no Besu needed)
    hardhat: {
      chainId: 1337,
    },
    // Your running Hyperledger Besu node (used for integration tests)
    besu: {
      url: process.env.BESU_RPC_URL || "http://localhost:8545",
      chainId: parseInt(process.env.BESU_CHAIN_ID || "1337"),
      accounts: [
        process.env.OWNER_PRIVATE_KEY      || "0x8f2a55949038a9610f50fb23b5883af3b4ecb3c3bb792cbcefbd1542c692be63",
        process.env.EMISSION_PRIVATE_KEY   || "0x8f2a55949038a9610f50fb23b5883af3b4ecb3c3bb792cbcefbd1542c692be63",
        process.env.GOVERNANCE_PRIVATE_KEY || "0x8f2a55949038a9610f50fb23b5883af3b4ecb3c3bb792cbcefbd1542c692be63",
      ],
      gasPrice: 0,
    },
  },
  // Copy contracts from parent directory so Hardhat can compile them
  paths: {
    sources:  "../contracts",
    tests:    "./test",
    cache:    "./cache",
    artifacts: "./artifacts",
  },
};
