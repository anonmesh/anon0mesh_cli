#!/usr/bin/env node
/**
 * fetch_idl.mjs — fetch and display the on-chain IDL for the ble-revshare program
 * Usage: node fetch_idl.mjs [program_id] [rpc_url]
 */

const [,, programId = "7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks",
         rpcUrl    = "https://api.devnet.solana.com"] = process.argv;

const { Connection, PublicKey } = await import("@solana/web3.js");
const anchor = await import("@coral-xyz/anchor");

const connection = new anchor.AnchorProvider(
    new Connection(rpcUrl, "confirmed"),
    { publicKey: PublicKey.default, signTransaction: async t => t, signAllTransactions: async ts => ts },
    { commitment: "confirmed" },
);

try {
    const idl = await anchor.Program.fetchIdl(new PublicKey(programId), connection);
    if (!idl) {
        console.error("No IDL found on-chain for", programId);
        console.error("The program may not have had its IDL uploaded (anchor idl init/upgrade).");
        process.exit(1);
    }

    console.log("\n=== Instructions ===");
    for (const ix of idl.instructions) {
        console.log(`\n  ${ix.name}`);
        console.log(`    args:     ${ix.args.map(a => `${a.name}: ${JSON.stringify(a.type)}`).join(", ") || "(none)"}`);
        console.log(`    accounts: ${ix.accounts.map(a => a.name).join(", ")}`);
    }

    console.log("\n=== Full IDL (JSON) ===");
    console.log(JSON.stringify(idl, null, 2));
} catch (e) {
    console.error("Error:", e.message);
}
