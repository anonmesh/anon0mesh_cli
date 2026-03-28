#!/usr/bin/env node
/**
 * rescue_shim.mjs — Arcium helper shim for anon0mesh beacon
 * ===========================================================
 * Built from the actual anon0mesh contract:
 *   Program ID:  7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks  (declare_id! in lib.rs)
 *   Instruction: execute_payment(computation_offset, amount, nonce, pub_key)
 *   Purpose:     Log encrypted payment stats via Arcium MPC after a tx relays
 *
 * Install: npm install @arcium-hq/client @coral-xyz/anchor @solana/web3.js
 */

import {
    RescueCipher, x25519, getMXEPublicKey,
    getClockAccAddress, getClusterAccAddress,
    getCompDefAccAddress, getCompDefAccOffset,
    getComputationAccAddress, getExecutingPoolAccAddress,
    getFeePoolAccAddress, getMempoolAccAddress, getMXEAccAddress,
    getArciumEnv, getArciumProgramId,
} from "@arcium-hq/client";
import { randomBytes, createHash } from "crypto";
import { readFileSync } from "node:fs";
import BN from "bn.js";

// Read all of stdin synchronously — used for sensitive values (keys, secrets)
// that must not appear in the process argument list (ps aux / /proc/pid/cmdline).
function readStdin() {
    try { return readFileSync(0, "utf8").trim(); } catch { return ""; }
}

const [,, cmd, ...args] = process.argv;

// ── Constants from the real contract ──────────────────────────────────────────
// declare_id! in programs/ble-revshare/src/lib.rs + Anchor.toml [programs.devnet]
const MXE_PROGRAM_ID = "7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks";

// sign_pda_account: PDA derived from seeds=[b"ArciumSignerAccount"] on the MXE program
// find_program_address(["ArciumSignerAccount"], MXE_PROGRAM_ID)

// comp_def_offset("payment_stats")
const COMP_DEF_NAME = "payment_stats";

function u8a(hex)  { return Uint8Array.from(Buffer.from(hex, "hex")); }
function hex(u8)   { return Buffer.from(u8).toString("hex"); }
function out(data) { console.log(JSON.stringify({ ok: true,  ...data })); }
function fail(msg) { console.log(JSON.stringify({ ok: false, error: msg })); process.exit(1); }

function deserializeLE(bytes) {
    let result = 0n;
    for (let i = bytes.length - 1; i >= 0; i--) {
        result = (result << 8n) | BigInt(bytes[i]);
    }
    return result;
}

// Anchor instruction discriminator: sha256("global:<name>")[0:8]
function disc(name) {
    return createHash("sha256").update(`global:${name}`).digest().slice(0, 8);
}

try {
    if (cmd === "keygen") {
        const privateKey = x25519.utils.randomSecretKey();
        const publicKey  = x25519.getPublicKey(privateKey);
        out({ privkey_hex: hex(privateKey), pubkey_hex: hex(publicKey) });

    } else if (cmd === "encrypt") {
        const [mxePubkeyHex, valuesJson, nonceHex] = args;
        if (!mxePubkeyHex || !valuesJson) fail("usage: encrypt <mxe_pubkey_hex> <values_json> [nonce_hex]");

        const mxePublicKey = u8a(mxePubkeyHex);
        const plaintext    = JSON.parse(valuesJson).map(BigInt);
        const nonce        = nonceHex ? u8a(nonceHex) : randomBytes(16);

        const privateKey   = x25519.utils.randomSecretKey();
        const publicKey    = x25519.getPublicKey(privateKey);
        const sharedSecret = x25519.getSharedSecret(privateKey, mxePublicKey);
        const cipher       = new RescueCipher(sharedSecret);
        const ciphertext   = cipher.encrypt(plaintext, nonce);

        out({
            ciphertexts:       ciphertext.map(ct => Array.from(ct)),
            pubkey_hex:        hex(publicKey),
            nonce_hex:         hex(nonce),
            nonce_bn:          deserializeLE(Buffer.from(nonce)).toString(),
            shared_secret_hex: hex(sharedSecret),
        });

    } else if (cmd === "decrypt") {
        // shared_secret_hex is passed via stdin to keep it out of the process arg list
        const sharedSecretHex = readStdin();
        const [ciphertextsJson, nonceHex] = args;
        if (!sharedSecretHex || !ciphertextsJson || !nonceHex)
            fail("usage: decrypt <ciphertexts_json> <nonce_hex>  (shared_secret_hex via stdin)");

        const sharedSecret = u8a(sharedSecretHex);
        const ciphertexts  = JSON.parse(ciphertextsJson).map(ct => Uint8Array.from(ct));
        const nonce        = u8a(nonceHex);
        const cipher       = new RescueCipher(sharedSecret);
        const plaintext    = cipher.decrypt(ciphertexts, nonce);
        out({ values: plaintext.map(v => v.toString()) });

    } else if (cmd === "shared_secret") {
        // privkeyHex is passed via stdin to keep it out of the process arg list
        const privkeyHex = readStdin();
        const [mxePubkeyHex] = args;
        if (!privkeyHex || !mxePubkeyHex) fail("usage: shared_secret <mxe_pubkey_hex>  (privkey_hex via stdin)");
        const sharedSecret = x25519.getSharedSecret(u8a(privkeyHex), u8a(mxePubkeyHex));
        out({ shared_secret_hex: hex(sharedSecret) });

    } else if (cmd === "mxe_pubkey") {
        // Fetch MXE x25519 pubkey from chain
        const [programId, rpcUrl] = args;
        const progId = programId || MXE_PROGRAM_ID;

        const { Connection, PublicKey, Keypair } = await import("@solana/web3.js");
        const anchor = await import("@coral-xyz/anchor");
        const connection = new Connection(rpcUrl || "https://api.devnet.solana.com", "confirmed");
        const dummyKp    = Keypair.generate();
        const provider   = new anchor.AnchorProvider(
            connection,
            { publicKey: dummyKp.publicKey, signTransaction: async t => t, signAllTransactions: async ts => ts },
            { commitment: "confirmed" }
        );
        const mxePubkey = await getMXEPublicKey(provider, new PublicKey(progId));
        out({ mxe_pubkey_hex: hex(mxePubkey) });

    } else if (cmd === "arcium_accounts") {
        // Return all PDAs for execute_payment
        const [programIdArg, clusterOffsetStr, computationOffsetStr] = args;
        const programId = programIdArg || MXE_PROGRAM_ID;
        let clusterOffset;
        try { clusterOffset = getArciumEnv().arciumClusterOffset; }
        catch { clusterOffset = Number.parseInt(clusterOffsetStr || "456"); }

        const { PublicKey } = await import("@solana/web3.js");
        const computationOffset = new BN(computationOffsetStr || "0");
        const progPubkey        = new PublicKey(programId);
        const compDefOffset     = Buffer.from(getCompDefAccOffset(COMP_DEF_NAME)).readUInt32LE();
        const [signPdaPubkey]   = PublicKey.findProgramAddressSync(
            [Buffer.from("ArciumSignerAccount")], progPubkey
        );

        out({
            computationAccount: getComputationAccAddress(clusterOffset, computationOffset).toBase58(),
            clusterAccount:     getClusterAccAddress(clusterOffset).toBase58(),
            mxeAccount:         getMXEAccAddress(progPubkey).toBase58(),
            mempoolAccount:     getMempoolAccAddress(clusterOffset).toBase58(),
            executingPool:      getExecutingPoolAccAddress(clusterOffset).toBase58(),
            compDefAccount:     getCompDefAccAddress(progPubkey, compDefOffset).toBase58(),
            poolAccount:        getFeePoolAccAddress().toBase58(),
            clockAccount:       getClockAccAddress().toBase58(),
            arciumProgramId:    getArciumProgramId().toBase58(),
            signPda:            signPdaPubkey.toBase58(),
            compDefOffset,
        });

    } else if (cmd === "execute_payment") {
        // ── Build + sign + send execute_payment instruction ────────────────────
        // Matches FIXED contract:
        //   execute_payment(computation_offset: u64, amount: u64, encrypted_amount: [u8;32],
        //                   nonce: u128, pub_key: [u8;32])
        //
        // Instruction data layout:
        //   [discriminator 8B][computation_offset 8B LE][amount 8B LE]
        //   [encrypted_amount 32B][nonce 16B LE][pub_key 32B]  = 104 bytes
        //
        // The shim encrypts amount locally using the MXE pubkey (x25519 + RescueCipher).
        //
        // Args JSON passed via stdin (contains payerKeypairHex — kept off arg list):
        // {
        //   rpcUrl, programId?, payerKeypairHex, clusterOffset?,
        //   amount, mxePubkeyHex,
        //   recipientB58, mintB58,
        //   payerTokenAccountB58, recipientTokenAccountB58, treasuryTokenAccountB58,
        //   broadcasterB58?, broadcasterKeypairHex?, broadcasterTokenAccountB58?
        // }
        const argsJson = readStdin();
        if (!argsJson) fail("usage: execute_payment  (json via stdin)");

        const p = JSON.parse(argsJson);
        const {
            rpcUrl,
            programId:              progIdArg,
            payerKeypairHex,
            clusterOffset:          clusterOffStr,
            amount,
            mxePubkeyHex,
            recipientB58,
            mintB58,
            payerTokenAccountB58,
            recipientTokenAccountB58,
            treasuryTokenAccountB58,
            broadcasterB58,
            broadcasterKeypairHex,
            broadcasterTokenAccountB58,
        } = p;

        if (!mxePubkeyHex) fail("mxePubkeyHex is required for encryption");

        const {
            Connection, PublicKey, Keypair,
            Transaction, TransactionInstruction, SystemProgram,
        } = await import("@solana/web3.js");
        const { TOKEN_PROGRAM_ID } = await import("@solana/spl-token");

        const programId  = progIdArg || MXE_PROGRAM_ID;
        const connection = new Connection(rpcUrl || "https://api.devnet.solana.com", "confirmed");
        const payerKp    = Keypair.fromSecretKey(u8a(payerKeypairHex));
        const progPubkey = new PublicKey(programId);
        let clusterOffset;
        try { clusterOffset = getArciumEnv().arciumClusterOffset; }
        catch { clusterOffset = Number.parseInt(clusterOffStr || "456"); }

        // Encrypt amount with x25519 + RescueCipher (client-side, using MXE pubkey)
        const mxePublicKey  = u8a(mxePubkeyHex);
        const clientPrivKey = x25519.utils.randomSecretKey();
        const clientPubKey  = x25519.getPublicKey(clientPrivKey);
        const sharedSecret  = x25519.getSharedSecret(clientPrivKey, mxePublicKey);
        const rescueCipher  = new RescueCipher(sharedSecret);
        const encNonce      = randomBytes(16);
        const ciphertexts   = rescueCipher.encrypt([BigInt(amount)], encNonce);
        const encryptedAmountBuf = Buffer.from(ciphertexts[0]);           // 32-byte field element
        const nonceBig      = deserializeLE(Buffer.from(encNonce));       // u128 as BigInt

        // Random 8-byte computation offset
        const compOffsetBN  = new BN(randomBytes(8), "hex");
        const compDefOffset = Buffer.from(getCompDefAccOffset(COMP_DEF_NAME)).readUInt32LE();

        // PDAs — exact match to getArciumAccounts() in the hook
        const computationAccount = getComputationAccAddress(clusterOffset, compOffsetBN);
        const clusterAccount     = getClusterAccAddress(clusterOffset);
        const mxeAccount         = getMXEAccAddress(progPubkey);
        const mempoolAccount     = getMempoolAccAddress(clusterOffset);
        const executingPool      = getExecutingPoolAccAddress(clusterOffset);
        const compDefAccount     = getCompDefAccAddress(progPubkey, compDefOffset);
        const poolAccount        = getFeePoolAccAddress();
        const clockAccount       = getClockAccAddress();
        const [signPda]          = PublicKey.findProgramAddressSync(
            [Buffer.from("ArciumSignerAccount")], progPubkey
        );

        // Whitelist PDA: seeds = [b"whitelist", mint]
        const mint = new PublicKey(mintB58);
        const [whitelistEntry] = PublicKey.findProgramAddressSync(
            [Buffer.from("whitelist"), mint.toBuffer()], progPubkey
        );

        // Instruction data layout (fixed contract — 104 bytes):
        //   [disc 8B][comp_offset 8B LE][amount 8B LE][encrypted_amount 32B][nonce 16B LE][pub_key 32B]
        const ix_data = Buffer.alloc(8 + 8 + 8 + 32 + 16 + 32);
        disc("execute_payment").copy(ix_data, 0);
        ix_data.writeBigUInt64LE(BigInt(compOffsetBN.toString()), 8);
        ix_data.writeBigUInt64LE(BigInt(amount), 16);
        encryptedAmountBuf.copy(ix_data, 24);                             // 32-byte ciphertext
        // nonce as u128 LE at offset 56 (two u64s)
        ix_data.writeBigUInt64LE(nonceBig & 0xFFFFFFFFFFFFFFFFn, 56);
        ix_data.writeBigUInt64LE(nonceBig >> 64n, 64);
        Buffer.from(clientPubKey).copy(ix_data, 72);                      // 32-byte x25519 pubkey

        const recipient      = new PublicKey(recipientB58);
        const payerTA        = new PublicKey(payerTokenAccountB58);
        const recipientTA    = new PublicKey(recipientTokenAccountB58);
        const broadcaster    = broadcasterB58 ? new PublicKey(broadcasterB58) : null;
        const broadcasterPub = broadcaster || payerKp.publicKey;

        // Derive broadcaster ATA — mirrors wallet.py: treasury_pk defaults to beacon_pubkey
        const ATA_PROGRAM_PK = new PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
        const [derivedBcasterTA] = PublicKey.findProgramAddressSync(
            [broadcasterPub.toBuffer(), TOKEN_PROGRAM_ID.toBuffer(), mint.toBuffer()],
            ATA_PROGRAM_PK
        );

        const broadcasterTA = broadcasterTokenAccountB58
            ? new PublicKey(broadcasterTokenAccountB58)
            : derivedBcasterTA;

        // Treasury defaults to broadcaster's ATA (mirrors wallet.py: treasury_pk = beacon_pubkey)
        const treasuryTA = new PublicKey(
            treasuryTokenAccountB58 || broadcasterTokenAccountB58 || derivedBcasterTA.toBase58()
        );

        // Account order matches on-chain IDL for execute_payment exactly (21 accounts):
        const keys = [
            { pubkey: payerKp.publicKey,                            isSigner: true,             isWritable: true  },
            { pubkey: broadcaster || payerKp.publicKey,             isSigner: !!broadcasterKeypairHex, isWritable: false },
            { pubkey: recipient,                                    isSigner: false,            isWritable: false },
            { pubkey: mint,                                         isSigner: false,            isWritable: false },
            { pubkey: whitelistEntry,                               isSigner: false,            isWritable: false },
            { pubkey: payerTA,                                      isSigner: false,            isWritable: true  },
            { pubkey: recipientTA,                                  isSigner: false,            isWritable: true  },
            { pubkey: treasuryTA,                                   isSigner: false,            isWritable: true  },
            { pubkey: broadcasterTA,                                isSigner: false,            isWritable: true  },
            { pubkey: signPda,                                      isSigner: false,            isWritable: true  },
            { pubkey: mxeAccount,                                   isSigner: false,            isWritable: false },
            { pubkey: mempoolAccount,                               isSigner: false,            isWritable: true  },
            { pubkey: executingPool,                                isSigner: false,            isWritable: true  },
            { pubkey: computationAccount,                           isSigner: false,            isWritable: true  },
            { pubkey: compDefAccount,                               isSigner: false,            isWritable: false },
            { pubkey: clusterAccount,                               isSigner: false,            isWritable: true  },
            { pubkey: poolAccount,                                  isSigner: false,            isWritable: true  },
            { pubkey: clockAccount,                                 isSigner: false,            isWritable: true  },
            { pubkey: TOKEN_PROGRAM_ID,                             isSigner: false,            isWritable: false },
            { pubkey: SystemProgram.programId,                      isSigner: false,            isWritable: false },
            { pubkey: getArciumProgramId(),                         isSigner: false,            isWritable: false },
        ];

        const ix  = new TransactionInstruction({ keys, programId: progPubkey, data: ix_data });
        const tx  = new Transaction().add(ix);
        const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash();
        tx.recentBlockhash = blockhash;
        tx.feePayer = payerKp.publicKey;

        const signers = [payerKp];
        if (broadcasterKeypairHex) {
            const broadcasterKp = Keypair.fromSecretKey(u8a(broadcasterKeypairHex));
            // Only add if different pubkey — beacon uses same keypair for payer + broadcaster
            if (broadcasterKp.publicKey.toBase58() !== payerKp.publicKey.toBase58()) {
                signers.push(broadcasterKp);
            }
        }
        tx.sign(...signers);

        const sig = await connection.sendRawTransaction(tx.serialize());
        await connection.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight });

        out({
            signature:          sig,
            computationAccount: computationAccount.toBase58(),
        });

    } else {
        fail(`Unknown command: ${cmd || "(none)"}. Use: keygen | encrypt | decrypt | shared_secret | mxe_pubkey | arcium_accounts | execute_payment`);
    }

} catch (err) {
    fail(err.message || String(err));
}
