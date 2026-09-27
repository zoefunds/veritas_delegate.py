// Resolve an existing unresolved vote after the resolution window opens.
// Supply the target identifiers explicitly; this script is not tied to a
// historical smoke-test deployment.

import fs from "node:fs";
import { ethers } from "ethers";
import path from "node:path";

const gl = await import("genlayer-js");

const CONTRACT_ADDRESS = process.env.VERITAS_CONTRACT_ADDRESS || "0xdb18abE502829D0Ff2FE5856ab1EB3BC6a5a5783";
const KEYSTORE_PATH = path.join(process.env.HOME, ".genlayer/keystores/veritas_tester.json");
const KEYSTORE_PASSWORD = process.env.VERITAS_TEST_KEYSTORE_PASSWORD;
if (!KEYSTORE_PASSWORD) {
  throw new Error(
    "Set VERITAS_TEST_KEYSTORE_PASSWORD to the veritas_tester keystore password before running this script.",
  );
}

const PROPOSAL_ID = process.env.VERITAS_PROPOSAL_ID;
const DELEGATE_ID = process.env.VERITAS_DELEGATE_ID;

if (!PROPOSAL_ID || !DELEGATE_ID) {
  throw new Error("Set VERITAS_PROPOSAL_ID and VERITAS_DELEGATE_ID before running this script.");
}

function log(label, value) {
  console.log(`\n=== ${label} ===`);
  console.log(JSON.stringify(value, (_k, v) => (typeof v === "bigint" ? v.toString() : v), 2));
}

async function main() {
  const keystoreJson = fs.readFileSync(KEYSTORE_PATH, "utf8");
  const wallet = await ethers.Wallet.fromEncryptedJson(keystoreJson, KEYSTORE_PASSWORD);
  const account = gl.createAccount(wallet.privateKey);

  const client = gl.createClient({ chain: gl.chains.studionet, account });
  await client.initializeConsensusSmartContract();

  const hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "resolve_vote_outcome",
    args: [PROPOSAL_ID, DELEGATE_ID, Math.floor(Date.now() / 1000), new Uint8Array()],
    value: 0n,
  });
  const receipt = await client.waitForTransactionReceipt({ hash, retries: 150, interval: 5000 });
  log("resolve_vote_outcome receipt", receipt);

  const vote = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_vote",
    args: [PROPOSAL_ID, DELEGATE_ID],
  });
  log("vote after resolution", vote);

  const delegate = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_delegate",
    args: [DELEGATE_ID],
  });
  log("delegate after resolution", delegate);

  console.log("\n✅ Resolution retry completed.");
}

main().catch((err) => {
  console.error("\n❌ Failed:", err);
  process.exit(1);
});
