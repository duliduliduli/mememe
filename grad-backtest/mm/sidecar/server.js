'use strict';
// Minimal HTTP bridge: the Python engine decides, this process only builds, signs and
// sends Meteora DLMM transactions with the same WALLET_PRIVATE_KEY the executor uses.
// It listens on localhost only. Every endpoint returns JSON; errors carry {error}.
const http = require('http');
const { Connection, PublicKey, Keypair, sendAndConfirmTransaction } = require('@solana/web3.js');
const dlmmModule = require('@meteora-ag/dlmm');
const DLMM = dlmmModule.default || dlmmModule;
const { StrategyType, getPriceOfBinByBinId } = dlmmModule;
const BN = require('bn.js');
const bs58 = require('bs58').default || require('bs58');

const PORT = parseInt(process.env.MM_SIDECAR_PORT || '8787', 10);
const RPC_URLS = (process.env.MM_RPC_URLS || process.env.RPC_URLS || process.env.RPC_URL || 'https://api.mainnet-beta.solana.com')
  .split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
const DRY_RUN = process.env.MM_SIDECAR_DRY_RUN === '1';

let wallet = null;
if (process.env.WALLET_PRIVATE_KEY) {
  wallet = Keypair.fromSecretKey(bs58.decode(process.env.WALLET_PRIVATE_KEY.trim()));
}
const connection = new Connection(RPC_URLS[0], { commitment: 'confirmed' });
const pools = new Map();

function log(msg) { process.stdout.write(`${new Date().toISOString()} [sidecar] ${msg}\n`); }

async function pool(address) {
  const key = String(address);
  let entry = pools.get(key);
  if (!entry || Date.now() - entry.at > 60_000) {
    const dlmm = entry ? entry.dlmm : await DLMM.create(connection, new PublicKey(key));
    if (entry) await dlmm.refetchStates();
    entry = { dlmm, at: Date.now() };
    pools.set(key, entry);
  }
  return entry.dlmm;
}

function binPrice(dlmm, binId) {
  return Number(dlmm.fromPricePerLamport(Number(getPriceOfBinByBinId(binId, dlmm.lbPair.binStep))));
}

async function poolInfo(dlmm) {
  const active = await dlmm.getActiveBin();
  const fee = dlmm.getFeeInfo();
  return {
    pool: dlmm.pubkey.toBase58(),
    tokenX: dlmm.tokenX.publicKey.toBase58(), decimalsX: dlmm.tokenX.mint.decimals,
    tokenY: dlmm.tokenY.publicKey.toBase58(), decimalsY: dlmm.tokenY.mint.decimals,
    binStep: dlmm.lbPair.binStep, activeBin: active.binId,
    price: Number(active.pricePerToken),           // Y per X, human units
    baseFeePct: Number(fee.baseFeeRatePercentage), maxFeePct: Number(fee.maxFeeRatePercentage),
    protocolFeePct: Number(fee.protocolFeePercentage),
    dynamicFeePct: Number(dlmm.getDynamicFee ? dlmm.getDynamicFee() : fee.baseFeeRatePercentage),
  };
}

function positionView(dlmm, p) {
  const d = p.positionData;
  return {
    position: p.publicKey.toBase58(),
    lowerBinId: d.lowerBinId, upperBinId: d.upperBinId,
    lowerPrice: binPrice(dlmm, d.lowerBinId), upperPrice: binPrice(dlmm, d.upperBinId),
    tokenAmountRaw: String(d.totalXAmount), quoteAmountRaw: String(d.totalYAmount),
    feeTokenRaw: d.feeX.toString(), feeQuoteRaw: d.feeY.toString(),
    claimedFeeTokenRaw: d.totalClaimedFeeXAmount.toString(), claimedFeeQuoteRaw: d.totalClaimedFeeYAmount.toString(),
  };
}

async function send(tx, signers) {
  if (DRY_RUN) {
    const sim = await connection.simulateTransaction(tx, signers);
    if (sim.value.err) throw new Error(`simulation failed: ${JSON.stringify(sim.value.err)} ${(sim.value.logs || []).slice(-4).join(' | ')}`);
    return 'DRY_RUN';
  }
  return sendAndConfirmTransaction(connection, tx, signers, { commitment: 'confirmed', skipPreflight: false, maxRetries: 3 });
}

const routes = {
  'GET /health': async () => ({
    ok: true, wallet: wallet ? wallet.publicKey.toBase58() : null, rpc: RPC_URLS[0].replace(/api-key=[^&]+/, 'api-key=***'),
    dryRun: DRY_RUN, sdk: '1.9.14',
  }),
  'GET /pool': async (q) => poolInfo(await pool(q.address)),
  'GET /balance': async () => {
    if (!wallet) throw new Error('no wallet');
    return { lamports: await connection.getBalance(wallet.publicKey, 'confirmed') };
  },
  'GET /positions': async (q) => {
    // Tracked keys are read directly (works on every RPC); discovery needs getProgramAccounts.
    const dlmm = await pool(q.address);
    const out = { tracked: [], discovered: null, activeBin: (await dlmm.getActiveBin()).binId };
    for (const key of (q.keys || '').split(',').filter(Boolean)) {
      try { out.tracked.push(positionView(dlmm, await dlmm.getPosition(new PublicKey(key)))); }
      catch (e) { out.tracked.push({ position: key, missing: true, error: String(e.message).slice(0, 120) }); }
    }
    if (wallet && q.discover === '1') {
      try {
        const { userPositions } = await dlmm.getPositionsByUserAndLbPair(wallet.publicKey);
        out.discovered = userPositions.map((p) => positionView(dlmm, p));
      } catch (e) { out.discoverError = String(e.message).slice(0, 160); }
    }
    return out;
  },
  'POST /open': async (_q, body) => {
    if (!wallet) throw new Error('no wallet');
    const dlmm = await pool(body.pool);
    const active = await dlmm.getActiveBin();
    const price = Number(active.pricePerToken);
    const half = Number(body.halfWidth);
    if (!(half > 0 && half < 1)) throw new Error('halfWidth must be a fraction in (0, 1)');
    const lowerPrice = price / (1 + half), upperPrice = price * (1 + half);
    const minBinId = dlmm.getBinIdFromPrice(Number(dlmm.toPricePerLamport(lowerPrice)), true);
    const maxBinId = dlmm.getBinIdFromPrice(Number(dlmm.toPricePerLamport(upperPrice)), false);
    if (maxBinId - minBinId + 1 > 69) throw new Error(`range spans ${maxBinId - minBinId + 1} bins > 69 (one position)`);
    const quote = await dlmm.quoteCreatePosition({ strategy: { minBinId, maxBinId, strategyType: StrategyType.Spot } });
    const positionKeypair = Keypair.generate();
    const tx = await dlmm.initializePositionAndAddLiquidityByStrategy({
      positionPubKey: positionKeypair.publicKey, user: wallet.publicKey,
      totalXAmount: new BN(String(body.tokenAmountRaw || '0')), totalYAmount: new BN(String(body.quoteAmountRaw || '0')),
      strategy: { minBinId, maxBinId, strategyType: StrategyType.Spot },
      slippage: Number(body.slippagePct || 1),
    });
    const signature = await send(tx, [wallet, positionKeypair]);
    log(`OPEN ${dlmm.pubkey.toBase58()} position=${positionKeypair.publicKey.toBase58()} bins=[${minBinId},${maxBinId}] sig=${signature}`);
    return {
      position: positionKeypair.publicKey.toBase58(), signature, minBinId, maxBinId, activeBin: active.binId,
      activePrice: price, lowerPrice: binPrice(dlmm, minBinId), upperPrice: binPrice(dlmm, maxBinId),
      rentSol: Number(quote.positionCost) + Number(quote.binArrayCost || 0),
    };
  },
  'POST /close': async (_q, body) => {
    if (!wallet) throw new Error('no wallet');
    const dlmm = await pool(body.pool);
    const positionKey = new PublicKey(body.position);
    const before = positionView(dlmm, await dlmm.getPosition(positionKey));
    const txs = await dlmm.removeLiquidity({
      user: wallet.publicKey, position: positionKey, fromBinId: before.lowerBinId, toBinId: before.upperBinId,
      bps: new BN(10_000), shouldClaimAndClose: true,
    });
    const signatures = [];
    for (const tx of Array.isArray(txs) ? txs : [txs]) signatures.push(await send(tx, [wallet]));
    log(`CLOSE ${dlmm.pubkey.toBase58()} position=${body.position} sigs=${signatures.join(',')}`);
    return { ...before, signatures };
  },
  'POST /claim': async (_q, body) => {
    if (!wallet) throw new Error('no wallet');
    const dlmm = await pool(body.pool);
    const position = await dlmm.getPosition(new PublicKey(body.position));
    const txs = await dlmm.claimAllSwapFee({ owner: wallet.publicKey, positions: [position] });
    const signatures = [];
    for (const tx of Array.isArray(txs) ? txs : [txs]) signatures.push(await send(tx, [wallet]));
    return { position: body.position, signatures, feeTokenRaw: position.positionData.feeX.toString(), feeQuoteRaw: position.positionData.feeY.toString() };
  },
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const route = routes[`${req.method} ${url.pathname}`];
  const reply = (code, payload) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(payload)); };
  if (!route) return reply(404, { error: 'not found' });
  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', async () => {
    try {
      const params = Object.fromEntries(url.searchParams.entries());
      const payload = body ? JSON.parse(body) : {};
      reply(200, await route(params, payload));
    } catch (e) {
      log(`ERROR ${req.method} ${url.pathname}: ${e.message}`);
      reply(500, { error: String(e.message || e).slice(0, 400) });
    }
  });
});

server.listen(PORT, '127.0.0.1', () => log(`listening on 127.0.0.1:${PORT} wallet=${wallet ? wallet.publicKey.toBase58() : 'none'} rpc=${RPC_URLS[0].replace(/api-key=[^&]+/, 'api-key=***')} dryRun=${DRY_RUN}`));
