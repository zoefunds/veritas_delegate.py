// Live smoke test against the deployed Veritas contract on GenLayer StudioNet.
//
// Uses a dedicated throwaway test account ("veritas_tester", created solely
// for this run and funded with 2 GEN from the deployer's own account) so we
// never touch the user's primary private key. Talks to genlayer-js directly
// because the installed `genlayer` CLI (0.39.2) hardcodes `value: 0n` on
// `write`, so it cannot exercise this contract's payable methods
// (register_delegate / delegate_stake / top_up_bond / withdraw paths).

import fs from "node:fs";
import { ethers } from "ethers";
import path from "node:path";

const gl = await import("genlayer-js");

const CONTRACT_ADDRESS = "0xdb18abE502829D0Ff2FE5856ab1EB3BC6a5a5783";
const KEYSTORE_PATH = path.join(
  process.env.HOME,
  ".genlayer/keystores/veritas_tester.json",
);
const KEYSTORE_PASSWORD = process.env.VERITAS_TEST_KEYSTORE_PASSWORD;
if (!KEYSTORE_PASSWORD) {
  throw new Error(
    "Set VERITAS_TEST_KEYSTORE_PASSWORD to the veritas_tester keystore password before running this script.",
  );
}

const ONE_GEN = 10n ** 18n;

function log(label, value) {
  console.log(`\n=== ${label} ===`);
  console.log(
    typeof value === "string"
      ? value
      : JSON.stringify(value, (_k, v) => (typeof v === "bigint" ? v.toString() : v), 2),
  );
}

async function main() {
  const keystoreJson = fs.readFileSync(KEYSTORE_PATH, "utf8");
  const wallet = await ethers.Wallet.fromEncryptedJson(keystoreJson, KEYSTORE_PASSWORD);
  const account = gl.createAccount(wallet.privateKey);
  console.log("Signing with test account:", account.address);

  const client = gl.createClient({
    chain: gl.chains.studionet,
    account,
  });
  await client.initializeConsensusSmartContract();

  const delegateId = `veritas_smoke_${Date.now()}`;
  const proposalId = `prop_smoke_${Date.now()}`;

  // ---- 1. Register delegate with 1 GEN bond (payable) ----------------------
  let hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "register_delegate",
    args: [delegateId, "ipfs://veritas-smoke-test-profile"],
    value: ONE_GEN,
  });
  let receipt = await client.waitForTransactionReceipt({ hash, retries: 100, interval: 5000 });
  log("register_delegate receipt", receipt);

  let delegate = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_delegate",
    args: [delegateId],
  });
  log("delegate after registration", delegate);

  // ---- 2. Delegate stake to itself (payable) -------------------------------
  hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "delegate_stake",
    args: [delegateId],
    value: 2n * ONE_GEN,
  });
  receipt = await client.waitForTransactionReceipt({ hash, retries: 100, interval: 5000 });
  log("delegate_stake receipt", receipt);

  delegate = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_delegate",
    args: [delegateId],
  });
  log("delegate after delegation", delegate);

  // ---- 3. Create a proposal --------------------------------------------
  const now = Math.floor(Date.now() / 1000);
  hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "create_proposal",
    args: [
      proposalId,
      "Veritas smoke-test proposal",
      "Live smoke test of the AI delegate accountability loop.",
      ["https://docs.genlayer.com/"],
      now + 5, // voting_deadline (short window for the smoke test)
      now + 10, // resolution_deadline
      now,
    ],
    value: 0n,
  });
  receipt = await client.waitForTransactionReceipt({ hash, retries: 100, interval: 5000 });
  log("create_proposal receipt", receipt);

  // ---- 4. Cast a vote with a falsifiable rationale -------------------------
  hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "cast_vote",
    args: [
      proposalId,
      delegateId,
      "YES",
      "This proposal references GenLayer's own documentation site, which is a stable, live, well-maintained resource.",
      ["https://docs.genlayer.com/"],
      now + 1,
    ],
    value: 0n,
  });
  receipt = await client.waitForTransactionReceipt({ hash, retries: 100, interval: 5000 });
  log("cast_vote receipt", receipt);

  const vote = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_vote",
    args: [proposalId, delegateId],
  });
  log("vote after casting", vote);

  const proposal = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_proposal",
    args: [proposalId],
  });
  log("proposal after voting", proposal);

  // ---- 5. Wait for the resolution window, then resolve via real consensus --
  const waitMs = (now + 12 - Math.floor(Date.now() / 1000)) * 1000;
  if (waitMs > 0) {
    console.log(`\nWaiting ${Math.ceil(waitMs / 1000)}s for resolution window to open...`);
    await new Promise((r) => setTimeout(r, waitMs));
  }

  hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "resolve_vote_outcome",
    args: [proposalId, delegateId, Math.floor(Date.now() / 1000), new Uint8Array()],
    value: 0n,
  });
  receipt = await client.waitForTransactionReceipt({ hash, retries: 150, interval: 5000 });
  log("resolve_vote_outcome receipt", receipt);

  const resolvedVote = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_vote",
    args: [proposalId, delegateId],
  });
  log("vote after resolution (live consensus verdict)", resolvedVote);

  delegate = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_delegate",
    args: [delegateId],
  });
  log("delegate after resolution (integrity score updated)", delegate);

  const stats = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_protocol_stats",
    args: [],
  });
  log("protocol stats", stats);

  console.log("\n✅ Live smoke test completed.");
}

main().catch((err) => {
  console.error("\n❌ Smoke test failed:", err);
  process.exit(1);
});
