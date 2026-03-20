#!/usr/bin/env node
/**
 * rescue_shim.mjs — Arcium helper shim for anon0mesh beacon
 * ===========================================================
 * Matches useBleRevshareContract.ts exactly — no anchorpy, raw TransactionInstruction.
 *
 * Install: npm install @arcium-hq/client @coral-xyz/anchor @solana/web3.js
 */

import {
    RescueCipher, x25519, getMXEPublicKey,
    getClockAccAddress, getClusterAccAddress,
    getCompDefAccAddress, getCompDefAccOffset,
    getComputationAccAddress, getExecutingPoolAccAddress,
    getFeePoolAccAddress, getMempoolAccAddress, getMXEAccAddress,
} from "@arcium-hq/client";
import { randomBytes } from "crypto";

const [,, cmd, ...args] = process.argv;

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
async function discriminator(name) {
    const { createHash } = await import("crypto");
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
        const [sharedSecretHex, ciphertextsJson, nonceHex] = args;
        if (!sharedSecretHex || !ciphertextsJson || !nonceHex)
            fail("usage: decrypt <shared_secret_hex> <ciphertexts_json> <nonce_hex>");

        const sharedSecret = u8a(sharedSecretHex);
        const ciphertexts  = JSON.parse(ciphertextsJson).map(ct => Uint8Array.from(ct));
        const nonce        = u8a(nonceHex);
        const cipher       = new RescueCipher(sharedSecret);
        const plaintext    = cipher.decrypt(ciphertexts, nonce);
        out({ values: plaintext.map(v => v.toString()) });

    } else if (cmd === "shared_secret") {
        const [privkeyHex, mxePubkeyHex] = args;
        if (!privkeyHex || !mxePubkeyHex) fail("usage: shared_secret <privkey_hex> <mxe_pubkey_hex>");
        const sharedSecret = x25519.getSharedSecret(u8a(privkeyHex), u8a(mxePubkeyHex));
        out({ shared_secret_hex: hex(sharedSecret) });

    } else if (cmd === "mxe_pubkey") {
        const [programId, rpcUrl] = args;
        if (!programId) fail("usage: mxe_pubkey <program_id> [rpc_url]");

        const { Connection, PublicKey, Keypair } = await import("@solana/web3.js");
        const anchor = await import("@coral-xyz/anchor");
        const connection = new Connection(rpcUrl || "https://api.devnet.solana.com", "confirmed");
        const dummyKp    = Keypair.generate();
        const provider   = new anchor.AnchorProvider(
            connection,
            { publicKey: dummyKp.publicKey, signTransaction: async t => t, signAllTransactions: async ts => ts },
            { commitment: "confirmed" }
        );
        const mxePubkey = await getMXEPublicKey(provider, new PublicKey(programId));
        out({ mxe_pubkey_hex: hex(mxePubkey) });

    } else if (cmd === "arcium_accounts") {
        const [programId, compDefName, clusterOffsetStr, computationOffsetStr] = args;
        if (!programId || !compDefName || !clusterOffsetStr || !computationOffsetStr)
            fail("usage: arcium_accounts <program_id> <comp_def_name> <cluster_offset> <computation_offset>");

        const { PublicKey } = await import("@solana/web3.js");
        const anchor        = await import("@coral-xyz/anchor");
        const clusterOffset     = parseInt(clusterOffsetStr);
        const computationOffset = new anchor.BN(computationOffsetStr);
        const progPubkey        = new PublicKey(programId);
        const compDefOffset     = Buffer.from(getCompDefAccOffset(compDefName)).readUInt32LE();

        out({
            computationAccount: getComputationAccAddress(clusterOffset, computationOffset).toBase58(),
            clusterAccount:     getClusterAccAddress(clusterOffset).toBase58(),
            mxeAccount:         getMXEAccAddress(progPubkey).toBase58(),
            mempoolAccount:     getMempoolAccAddress(clusterOffset).toBase58(),
            executingPool:      getExecutingPoolAccAddress(clusterOffset).toBase58(),
            compDefAccount:     getCompDefAccAddress(progPubkey, compDefOffset).toBase58(),
            poolAccount:        getFeePoolAccAddress().toBase58(),
            clockAccount:       getClockAccAddress().toBase58(),
            compDefOffset,
        });

    } else if (cmd === "queue_computation") {
        // ── Build + sign + send the Arcium computation transaction ──────────────
        // Mirrors createPaymentTransaction() from useBleRevshareContract.ts exactly.
        // No anchorpy — raw TransactionInstruction like the real hook.
        //
        // Args JSON: { rpcUrl, programId, payerKeypairHex, compDefName,
        //              clusterOffset, computationOffset, pubKeyHex,
        //              nonceBn, encryptedAddress }
        const argsJson = args[0];
        if (!argsJson) fail("usage: queue_computation <json>");

        const p = JSON.parse(argsJson);
        const {
            rpcUrl, programId, payerKeypairHex, compDefName,
            clusterOffset, computationOffset: compOffsetStr,
            pubKeyHex, nonceBn, encryptedAddress,
        } = p;

        const {
            Connection, PublicKey, Keypair,
            Transaction, TransactionInstruction, SystemProgram,
        } = await import("@solana/web3.js");
        const anchor = await import("@coral-xyz/anchor");

        const connection        = new Connection(rpcUrl, "confirmed");
        const payerKp           = Keypair.fromSecretKey(u8a(payerKeypairHex));
        const progPubkey        = new PublicKey(programId);
        const compOffset        = new anchor.BN(compOffsetStr);
        const clusterOff        = parseInt(clusterOffset);
        const compDefOffset     = Buffer.from(getCompDefAccOffset(compDefName)).readUInt32LE();

        // PDAs — exact mirror of getArciumAccounts()
        const computationAccount = getComputationAccAddress(clusterOff, compOffset);
        const clusterAccount     = getClusterAccAddress(clusterOff);
        const mxeAccount         = getMXEAccAddress(progPubkey);
        const mempoolAccount     = getMempoolAccAddress(clusterOff);
        const executingPool      = getExecutingPoolAccAddress(clusterOff);
        const compDefAccount     = getCompDefAccAddress(progPubkey, compDefOffset);
        const poolAccount        = getFeePoolAccAddress();
        const clockAccount       = getClockAccAddress();

        const [signPda] = PublicKey.findProgramAddressSync(
            [Buffer.from("ArciumSignerAccount")], progPubkey
        );

        // Instruction discriminator: sha256("global:queue_confidential_balance")[0:8]
        const disc = await discriminator("queue_confidential_balance");

        // Instruction data layout (matches Anchor serialization):
        //   [disc 8B][computation_offset 8B LE][pub_key 32B][nonce 16B LE][enc_address 32B]
        const data = Buffer.alloc(8 + 8 + 32 + 16 + 32);
        disc.copy(data, 0);
        data.writeBigUInt64LE(BigInt(compOffsetStr), 8);
        Buffer.from(u8a(pubKeyHex)).copy(data, 16);
        // nonce as u128 LE (16 bytes)
        const nonceBig = BigInt(nonceBn);
        data.writeBigUInt64LE(nonceBig & 0xFFFFFFFFFFFFFFFFn, 48);
        data.writeBigUInt64LE(nonceBig >> 64n,                 56);
        Buffer.from(encryptedAddress).copy(data, 64);

        const keys = [
            { pubkey: payerKp.publicKey, isSigner: true,  isWritable: true  },
            { pubkey: signPda,           isSigner: false, isWritable: true  },
            { pubkey: mxeAccount,        isSigner: false, isWritable: false },
            { pubkey: mempoolAccount,    isSigner: false, isWritable: true  },
            { pubkey: executingPool,     isSigner: false, isWritable: true  },
            { pubkey: computationAccount,isSigner: false, isWritable: true  },
            { pubkey: compDefAccount,    isSigner: false, isWritable: false },
            { pubkey: clusterAccount,    isSigner: false, isWritable: true  },
            { pubkey: poolAccount,       isSigner: false, isWritable: true  },
            { pubkey: clockAccount,      isSigner: false, isWritable: true  },
            { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
        ];

        const ix = new TransactionInstruction({ keys, programId: progPubkey, data });
        const tx = new Transaction().add(ix);

        const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash();
        tx.recentBlockhash = blockhash;
        tx.feePayer = payerKp.publicKey;
        tx.sign(payerKp);

        const sig = await connection.sendRawTransaction(tx.serialize());
        await connection.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight });

        out({
            signature:          sig,
            computationAccount: computationAccount.toBase58(),
        });

    } else {
        fail(`Unknown command: ${cmd || "(none)"}. Use: keygen | encrypt | decrypt | shared_secret | mxe_pubkey | arcium_accounts | queue_computation`);
    }

} catch (err) {
    fail(err.message || String(err));
}
