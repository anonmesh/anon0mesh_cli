#!/usr/bin/env node
import * as anchor from "@coral-xyz/anchor";
import { PublicKey, Keypair, Transaction, TransactionInstruction, SystemProgram } from "@solana/web3.js";
import { readFileSync } from "node:fs";

const PROGRAM_ID = new PublicKey("7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks");
const ARCIUM_PROGRAM_ID = new PublicKey("Arcj82pX7HxYKLR92qvgZUAd7vGS1k4hQvAFcPATFdEQ");
const INIT_PAYMENT_STATS_COMP_DEF_IX = Buffer.from([96, 78, 230, 203, 169, 42, 127, 99]);

const keyPath = process.env.ARCIUM_PAYER_KEYPAIR || process.env.ANCHOR_WALLET;
if (!keyPath) {
  console.error("Set ARCIUM_PAYER_KEYPAIR or ANCHOR_WALLET to your payer keypair path");
  process.exit(1);
}

const secret = Uint8Array.from(JSON.parse(readFileSync(keyPath, "utf8")));
const wallet = new anchor.Wallet(Keypair.fromSecretKey(secret));
const rpcUrl = process.env.ARCIUM_RPC_URL || "https://api.devnet.solana.com";
const connection = new anchor.web3.Connection(rpcUrl, "confirmed");

const arcium = await import("@arcium-hq/client");
const mxeAccount = arcium.getMXEAccAddress(PROGRAM_ID);
const compDefOffsetBuf = arcium.getCompDefAccOffset("payment_stats");
const compDefOffset = Buffer.from(compDefOffsetBuf).readUInt32LE(0);
const compDefAccount = arcium.getCompDefAccAddress(PROGRAM_ID, compDefOffset);
const clusterOffsetRaw = Number.parseInt(process.env.ARCIUM_CLUSTER_OFFSET ?? "456", 10);
const clusterOffset = Number.isNaN(clusterOffsetRaw) ? 456 : clusterOffsetRaw;
const mempoolAccount = arcium.getMempoolAccAddress(clusterOffset);
const executingPool = arcium.getExecutingPoolAccAddress(clusterOffset);
const clusterAccount = arcium.getClusterAccAddress(clusterOffset);
const poolAccount = arcium.getFeePoolAccAddress();
const clockAccount = arcium.getClockAccAddress();

console.log("Payer:", wallet.publicKey.toBase58());
console.log("clusterOffset:", clusterOffset);
console.log("mxeAccount:", mxeAccount.toBase58());
console.log("compDefAccount:", compDefAccount.toBase58());

const existing = await connection.getAccountInfo(compDefAccount, "confirmed");
if (existing) {
  console.log("compDefAccount already initialized; nothing to do.");
  process.exit(0);
}

const ix = new TransactionInstruction({
  programId: PROGRAM_ID,
  keys: [
    { pubkey: wallet.publicKey, isSigner: true, isWritable: true },
    { pubkey: mxeAccount, isSigner: false, isWritable: true },
    { pubkey: mempoolAccount, isSigner: false, isWritable: true },
    { pubkey: executingPool, isSigner: false, isWritable: true },
    { pubkey: compDefAccount, isSigner: false, isWritable: true },
    { pubkey: clusterAccount, isSigner: false, isWritable: true },
    { pubkey: poolAccount, isSigner: false, isWritable: true },
    { pubkey: clockAccount, isSigner: false, isWritable: true },
    { pubkey: ARCIUM_PROGRAM_ID, isSigner: false, isWritable: false },
    { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
  ],
  data: INIT_PAYMENT_STATS_COMP_DEF_IX,
});

const tx = new Transaction().add(ix);
tx.feePayer = wallet.publicKey;

try {
  const sig = await connection.sendTransaction(tx, [wallet.payer], {
    skipPreflight: false,
    preflightCommitment: "confirmed",
  });
  await connection.confirmTransaction(sig, "confirmed");
  console.log("init_payment_stats_comp_def tx:", sig);
} catch (err) {
  const logs = err?.transactionLogs || err?.logs;
  if (Array.isArray(logs)) {
    console.error("Simulation logs:");
    for (const line of logs) console.error("  ", line);
  }
  throw err;
}
