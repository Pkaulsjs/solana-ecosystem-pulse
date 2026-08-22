# Solana Ecosystem Pulse 🟣🟢

An auto-updating report on the current state of the Solana ecosystem:
network performance, validator health, and economic indicators — collected
with **Python stdlib only**, **zero API keys**, zero third-party packages.

## What it produces

| Output | Description |
|---|---|
| `reports/dashboard.html` | Self-contained interactive dark-theme dashboard (open in any browser, also hosted via GitHub Pages) |
| `reports/report.md` | Human-readable markdown report |
| `reports/report.json` | Machine-readable structured snapshot |
| `data/history.jsonl` | Append-only metric history powering anomaly detection |

## Metrics collected

**Network** — TPS (60-sample avg), slot time, block height, epoch progress,
total transactions, median priority fee, RPC health.

**Validators** — active/delinquent counts, delinquent stake %, total stake,
average commission, top-10 validators by stake, decentralization proxy
(validators needed to hold ≥33% of stake).

**Economy** — SOL price + 24h change + market cap (CoinGecko), DeFi TVL with
7d/30d deltas and 90-day sparkline (DeFiLlama), DEX volume 24h + 7d average,
protocol fees 24h/30d, stablecoin supply, circulating/total SOL supply.

## Anomaly detection

Each run compares against trailing history and flags:

- TPS drop >50% vs recent median
- Slot time >0.60s (nominal ~0.4s)
- Delinquent stake >1%
- TVL move >±15% (24h), SOL price move >±15% (24h)
- Stablecoin supply jump >±8% between samples

## Usage

```bash
python pulse.py
```

That's it. Outputs land in `reports/`. Run it on a schedule (cron, systemd
timer, or the included GitHub Actions workflow) for a continuously current
report — this repo's dashboard refreshes automatically every 6 hours via
Actions and is served at:
**https://pkaulsjs.github.io/solana-ecosystem-pulse/**

## Data sources (all free, keyless)

- Solana public JSON-RPC — `api.mainnet-beta.solana.com`
  (`getEpochInfo`, `getRecentPerformanceSamples`, `getVoteAccounts`,
  `getSupply`, `getRecentPrioritizationFees`, `getHealth`)
- DeFiLlama — chain TVL history, stablecoin supply, DEX volume, protocol fees
- CoinGecko public price API (best-effort; report degrades gracefully if absent)

## Requirements

Python 3.8+. No pip installs. No API keys. Rate-limits respected (one gentle
pass per run, ~10 requests total).

## License

MIT
