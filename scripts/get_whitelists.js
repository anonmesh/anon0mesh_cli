import { Connection, PublicKey } from "@solana/web3.js";

const prog = new PublicKey("7fvHNYVuZP6EYt68GLUa4kU8f8dCBSaGafL9aDhhtMZN");

async function check(url, name) {
    const c = new Connection(url);
    console.log(`Checking ${name}...`);
    try {
        const accs = await c.getProgramAccounts(prog, {
            dataSlice: { offset: 0, length: 0 }
        });
        console.log(`Found ${accs.length} total accounts on ${name}.`);
        for (let a of accs) {
            const info = await c.getAccountInfo(a.pubkey);
            if (!info) continue;
            if (info.data.length === 40) { 
                 const mint = new PublicKey(info.data.subarray(8, 40));
                 console.log(` -> Whitelist entry ${a.pubkey.toBase58()} for mint ${mint.toBase58()}`);
            }
        }
    } catch (e) { console.error(e); }
}

(async () => {
    await check("https://api.devnet.solana.com", "Devnet");
    await check("https://api.mainnet-beta.solana.com", "Mainnet");
})();
