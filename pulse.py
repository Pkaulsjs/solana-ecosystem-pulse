#!/usr/bin/env python3
"""Solana Ecosystem Pulse - auto-updating ecosystem report & dashboard.

Collects live network, validator, and ecosystem metrics using ONLY the
Python standard library plus free, keyless public endpoints:
  - Solana JSON-RPC (public mainnet-beta endpoint)
  - DeFiLlama public API (chain TVL, stablecoins, DEX volume, fees)
  - CoinGecko public simple-price API (best-effort, tolerated to fail)

Outputs:
  reports/report.json      machine-readable snapshot
  reports/report.md        human-readable markdown report
  reports/dashboard.html   self-contained interactive dark-theme dashboard
  data/history.jsonl       append-only metric history (anomaly detection)

Usage:
  python pulse.py                 # collect once, regenerate all outputs
  python pulse.py --no-anomaly    # skip anomaly comparison (first run)
No API keys, no third-party packages.
"""

import json
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
DATA = ROOT / "data"
HISTORY = DATA / "history.jsonl"

RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "solana-ecosystem-pulse/1.0 (public data aggregator)"}
SSL_CTX = ssl.create_default_context()

# Anomaly thresholds
TPS_DROP_FRAC = 0.50        # >50% below recent median
SLOT_TIME_SLOW_S = 0.60     # nominal ~0.4-0.45s
DELINQUENT_PCT_ALERT = 1.0  # % of stake delinquent
TVL_MOVE_PCT = 15.0
PRICE_MOVE_PCT = 15.0
STABLE_MOVE_PCT = 8.0


def http_json(url, payload=None, timeout=30):
    """GET (or POST if payload) a JSON URL. Returns parsed JSON or None."""
    data = None
    headers = dict(UA)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as e:
        print(f"  [warn] {url[:80]} -> {e}", file=sys.stderr)
        return None


def rpc(method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    res = http_json(RPC, body)
    if not res or "result" not in res:
        return None
    return res["result"]


def lamports_to_sol(l):
    return l / 1_000_000_000


# ---------------------------------------------------------------- collectors

def collect_network():
    m = {}
    health = rpc("getHealth")
    m["rpc_healthy"] = health == "ok"
    epoch = rpc("getEpochInfo")
    if epoch:
        m["epoch"] = epoch.get("epoch")
        m["absolute_slot"] = epoch.get("absoluteSlot")
        m["block_height"] = epoch.get("blockHeight")
        m["slot_index"] = epoch.get("slotIndex")
        m["slots_in_epoch"] = epoch.get("slotsInEpoch")
        m["epoch_progress_pct"] = round(
            100.0 * epoch.get("slotIndex", 0) / max(epoch.get("slotsInEpoch", 1), 1), 2)
        m["transactions_count"] = epoch.get("transactionCount")
        slots_left = max(epoch.get("slotsInEpoch", 0) - epoch.get("slotIndex", 0), 0)
        m["epoch_hours_remaining"] = round(slots_left * 0.4 / 3600, 1)
    samples = rpc("getRecentPerformanceSamples", [60])
    if samples:
        tx = sum(s["numTransactions"] for s in samples)
        secs = sum(s["samplePeriodSecs"] for s in samples)
        slots = sum(s["numSlots"] for s in samples)
        if secs:
            m["tps_avg"] = round(tx / secs, 1)
        if slots:
            m["slot_time_avg_s"] = round(secs / slots, 4)
    fees = rpc("getRecentPrioritizationFees")
    if fees:
        vals = sorted(f.get("prioritizationFee", 0) for f in fees)
        m["median_priority_fee_microlamports"] = median(vals) if vals else 0
    return m


def collect_validators():
    m = {}
    va = rpc("getVoteAccounts")
    if not va:
        return m
    current, delinq = va.get("current", []), va.get("delinquent", [])
    m["validators_active"] = len(current)
    m["validators_delinquent"] = len(delinq)
    total_stake = sum(v.get("activatedStake", 0) for v in current + delinq)
    m["total_stake_sol"] = round(lamports_to_sol(total_stake), 0)
    delinq_stake = sum(v.get("activatedStake", 0) for v in delinq)
    m["delinquent_stake_pct"] = round(
        100.0 * delinq_stake / total_stake, 3) if total_stake else 0.0
    if current:
        commissions = [v.get("commission", 0) for v in current]
        m["avg_commission_pct"] = round(sum(commissions) / len(commissions), 1)
        top = sorted(current, key=lambda v: -v.get("activatedStake", 0))[:10]
        m["top_validators"] = [
            {"name": (v.get("votePubkey", "")[:8] + "…"),
             "stake_sol": round(lamports_to_sol(v.get("activatedStake", 0)), 0),
             "commission": v.get("commission", 0)}
            for v in top]
        # Nakamoto coefficient proxy: validators holding >33% cumulative stake
        cum, ncoeff = 0.0, 0
        for v in sorted(current, key=lambda v: -v.get("activatedStake", 0)):
            cum += v.get("activatedStake", 0)
            ncoeff += 1
            if total_stake and cum / total_stake >= 1 / 3:
                break
        m["validators_for_33pct_stake"] = ncoeff
    return m


def collect_supply():
    m = {}
    sup = rpc("getSupply", {"excludeNonCirculatingAccountsList": True})
    if sup:
        t = sup.get("value", {})
        m["sol_total"] = round(lamports_to_sol(t.get("total", 0)), 0)
        m["sol_circulating"] = round(lamports_to_sol(t.get("circulating", 0)), 0)
    return m


def collect_defillama():
    m = {}
    tvl_hist = http_json("https://api.llama.fi/v2/historicalChainTvl/Solana")
    if isinstance(tvl_hist, list) and tvl_hist and isinstance(tvl_hist[0], dict):
        m["tvl_history"] = [[p.get("date"), round(p.get("tvl", 0), 0)]
                            for p in tvl_hist[-90:]]
        latest = tvl_hist[-1].get("tvl", 0)
        m["tvl_usd"] = round(latest, 0)
        if len(tvl_hist) >= 8:
            wk = tvl_hist[-8].get("tvl", 0)
            m["tvl_7d_change_pct"] = round(100.0 * (latest - wk) / wk, 2) if wk else None
        if len(tvl_hist) >= 31:
            mo = tvl_hist[-31].get("tvl", 0)
            m["tvl_30d_change_pct"] = round(100.0 * (latest - mo) / mo, 2) if mo else None
    elif isinstance(tvl_hist, list) and tvl_hist and isinstance(tvl_hist[0], list):
        m["tvl_history"] = [[d, round(v, 0)] for d, v in tvl_hist[-90:]]
        latest = tvl_hist[-1][1]
        m["tvl_usd"] = round(latest, 0)
        if len(tvl_hist) >= 8:
            wk = tvl_hist[-8][1]
            m["tvl_7d_change_pct"] = round(100.0 * (latest - wk) / wk, 2) if wk else None
        if len(tvl_hist) >= 31:
            mo = tvl_hist[-31][1]
            m["tvl_30d_change_pct"] = round(100.0 * (latest - mo) / mo, 2) if mo else None
    stables = http_json("https://stablecoins.llama.fi/stablecoinchains")
    if stables:
        for c in stables:
            if c.get("name") == "Solana":
                usd = c.get("totalCirculatingUSD", {})
                m["stablecoin_supply_usd"] = round(sum(usd.values()), 0)
                m["stablecoin_supply_usd_pegged_usd"] = round(usd.get("peggedUSD", 0), 0)
                break
    dex = http_json("https://api.llama.fi/overview/dexs/Solana")
    if dex:
        chart = dex.get("totalDataChart", [])
        if chart:
            m["dex_volume_history"] = [[d, round(v, 0)] for d, v in chart[-30:]]
            m["dex_volume_24h_usd"] = round(chart[-1][1], 0)
            if len(chart) >= 8:
                wk = sum(v for _, v in chart[-8:-1]) / 7
                m["dex_volume_7d_avg_usd"] = round(wk, 0)
    fees = http_json("https://api.llama.fi/summary/fees/Solana")
    if fees:
        chart = fees.get("totalDataChart", [])
        if chart:
            m["fees_24h_usd"] = round(chart[-1][1], 0)
            m["fees_30d_usd"] = round(sum(v for _, v in chart[-30:]), 0)
    return m


def collect_price():
    """Best-effort CoinGecko; absence is fine (report degrades gracefully)."""
    m = {}
    px = http_json("https://api.coingecko.com/api/v3/simple/price"
                   "?ids=solana&vs_currencies=usd"
                   "&include_24hr_change=true&include_market_cap=true")
    if px and "solana" in px:
        s = px["solana"]
        m["sol_price_usd"] = s.get("usd")
        m["sol_price_24h_change_pct"] = (
            round(s["usd_24h_change"], 2) if s.get("usd_24h_change") is not None else None)
        m["sol_market_cap_usd"] = s.get("usd_market_cap")
    return m


# ------------------------------------------------------------------ anomaly

def load_history():
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def detect_anomalies(cur, hist):
    alerts = []
    recent = hist[-28:]
    tps_hist = [h["metrics"].get("tps_avg") for h in recent
                if h.get("metrics", {}).get("tps_avg")]
    if tps_hist and cur.get("tps_avg"):
        med = median(tps_hist)
        if med and cur["tps_avg"] < med * (1 - TPS_DROP_FRAC):
            alerts.append(f"TPS {cur['tps_avg']:,.0f} is >{int(TPS_DROP_FRAC*100)}% below "
                          f"recent median {med:,.0f}")
    st = cur.get("slot_time_avg_s")
    if st and st > SLOT_TIME_SLOW_S:
        alerts.append(f"Average slot time {st:.3f}s exceeds {SLOT_TIME_SLOW_S}s "
                      f"(nominal ~0.4s)")
    dp = cur.get("delinquent_stake_pct")
    if dp is not None and dp > DELINQUENT_PCT_ALERT:
        alerts.append(f"Delinquent stake {dp}% exceeds {DELINQUENT_PCT_ALERT}% "
                      f"({cur.get('validators_delinquent', '?')} validators)")
    tvlc = cur.get("tvl_24h_change_pct")
    if tvlc is not None and abs(tvlc) > TVL_MOVE_PCT:
        alerts.append(f"TVL moved {tvlc:+.1f}% in 24h")
    pc = cur.get("sol_price_24h_change_pct")
    if pc is not None and abs(pc) > PRICE_MOVE_PCT:
        alerts.append(f"SOL price moved {pc:+.1f}% in 24h")
    # stablecoin supply change vs previous sample
    if hist:
        prev = hist[-1].get("metrics", {}).get("stablecoin_supply_usd")
        cur_s = cur.get("stablecoin_supply_usd")
        if prev and cur_s:
            ch = 100.0 * (cur_s - prev) / prev
            if abs(ch) > STABLE_MOVE_PCT:
                alerts.append(f"Stablecoin supply changed {ch:+.1f}% since last sample")
    return alerts


def tvl_24h_from_history(cur, hist):
    """Compute 24h TVL change from our own history if present."""
    if not hist:
        return None
    now = cur.get("collected_at_ts", time.time())
    for h in reversed(hist):
        ts = h.get("metrics", {}).get("collected_at_ts", 0)
        tvl = h.get("metrics", {}).get("tvl_usd")
        if tvl and now - ts >= 20 * 3600:
            cur_tvl = cur.get("tvl_usd")
            if cur_tvl:
                return round(100.0 * (cur_tvl - tvl) / tvl, 2)
            break
    return None


# ------------------------------------------------------------------ outputs

def spark_svg(points, w=560, h=120, color="#14F195"):
    """Render an SVG sparkline polyline. points = list of numbers."""
    pts = [p for p in points if isinstance(p, (int, float))]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    step = w / (len(pts) - 1)
    coords = [(i * step, h - 8 - (p - lo) / rng * (h - 20)) for i, p in enumerate(pts)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"M0,{h} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords) + f" L{w},{h} Z"
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" preserveAspectRatio="none">'
            f'<path d="{area}" fill="{color}" opacity="0.12"/>'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2"/>'
            f'</svg>')


def fmt_usd(v):
    if v is None:
        return "n/a"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"${v/div:,.2f}{suf}"
    return f"${v:,.2f}"


def fmt_num(v):
    if v is None:
        return "n/a"
    return f"{v:,.0f}"


def build_dashboard(m, alerts):
    cards = []
    def card(label, value, sub="", spark=None, color="#14F195"):
        s = spark_svg(spark) if spark else ""
        cards.append(f'''<div class="card"><div class="label">{label}</div>
<div class="value" style="color:{color}">{value}</div>
<div class="sub">{sub}</div>{s}</div>''')

    card("Network TPS (60-sample avg)", fmt_num(m.get("tps_avg")),
         f"slot time {m.get('slot_time_avg_s','?')}s", m.get("tps_history"))
    card("Epoch", str(m.get("epoch", "?")),
         f"{m.get('epoch_progress_pct','?')}% complete · ~{m.get('epoch_hours_remaining','?')}h left")
    card("Validators", fmt_num(m.get("validators_active")),
         f"{m.get('validators_delinquent',0)} delinquent · avg commission "
         f"{m.get('avg_commission_pct','?')}%")
    card("Total stake", f"{fmt_num(m.get('total_stake_sol'))} SOL",
         f"{m.get('validators_for_33pct_stake','?')} validators hold ≥33% stake")
    card("SOL price", (f"${m['sol_price_usd']:,.2f}" if m.get("sol_price_usd") else "n/a"),
         f"24h: {m.get('sol_price_24h_change_pct','?')}%" if m.get(
             "sol_price_24h_change_pct") is not None else "CoinGecko unavailable",
         m.get("price_history"))
    card("DeFi TVL", fmt_usd(m.get("tvl_usd")),
         f"7d: {m.get('tvl_7d_change_pct','?')}% · 30d: {m.get('tvl_30d_change_pct','?')}%",
         m.get("tvl_history"), "#9945FF")
    card("DEX volume (24h)", fmt_usd(m.get("dex_volume_24h_usd")),
         f"7d avg {fmt_usd(m.get('dex_volume_7d_avg_usd'))}", m.get("dex_volume_history"))
    card("Protocol fees (24h)", fmt_usd(m.get("fees_24h_usd")),
         f"30d {fmt_usd(m.get('fees_30d_usd'))}")
    card("Stablecoin supply", fmt_usd(m.get("stablecoin_supply_usd")),
         f"{fmt_usd(m.get('stablecoin_supply_usd_pegged_usd'))} USD-pegged")
    card("SOL circulating", fmt_num(m.get("sol_circulating")),
         f"of {fmt_num(m.get('sol_total'))} total")
    card("Median priority fee", f"{fmt_num(m.get('median_priority_fee_microlamports'))} μlamport",
         "recent prioritization fees")
    card("RPC health", "OK" if m.get("rpc_healthy") else "DEGRADED",
         "api.mainnet-beta.solana.com",
         "#14F195" if m.get("rpc_healthy") else "#f87171")

    alert_html = ""
    if alerts:
        items = "".join(f"<li>{a}</li>" for a in alerts)
        alert_html = f'<div class="alerts"><h2>⚠ Anomalies</h2><ul>{items}</ul></div>'
    else:
        alert_html = '<div class="alerts ok"><h2>✓ No anomalies detected</h2></div>'

    top_rows = "".join(
        f"<tr><td>{v['name']}</td><td>{v['stake_sol']:,.0f}</td><td>{v['commission']}%</td></tr>"
        for v in m.get("top_validators", []))
    top_html = f'''<h2>Top validators by stake</h2>
<table><tr><th>Validator</th><th>Stake (SOL)</th><th>Commission</th></tr>{top_rows}</table>''' \
        if top_rows else ""

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solana Ecosystem Pulse</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; background:#0b0f17; color:#e5e7eb;
  font-family:ui-sans-serif,system-ui,'Segoe UI',Roboto,sans-serif; }}
header {{ padding:28px 32px 8px; }}
h1 {{ margin:0; font-size:26px; letter-spacing:.5px; }}
h1 .grad {{ background:linear-gradient(90deg,#9945FF,#14F195);
  -webkit-background-clip:text; background-clip:text; color:transparent; }}
.updated {{ color:#6b7280; font-size:13px; padding:0 32px 12px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:14px; padding:8px 32px 24px; }}
.card {{ background:#111827; border:1px solid #1f2937; border-radius:14px;
  padding:16px 18px; }}
.card .label {{ color:#9ca3af; font-size:12px; text-transform:uppercase;
  letter-spacing:.8px; }}
.card .value {{ font-size:26px; font-weight:700; margin:6px 0 2px; }}
.card .sub {{ color:#9ca3af; font-size:12.5px; min-height:16px; }}
.spark {{ width:100%; height:56px; margin-top:10px; }}
.alerts {{ margin:0 32px 20px; padding:14px 18px; background:#1f1215;
  border:1px solid #7f1d1d; border-radius:12px; }}
.alerts.ok {{ background:#0f1a14; border-color:#14532d; }}
.alerts h2 {{ margin:0 0 8px; font-size:15px; }}
.alerts ul {{ margin:0; padding-left:20px; color:#fca5a5; }}
h2 {{ font-size:16px; padding:0 32px; }}
table {{ margin:8px 32px 32px; border-collapse:collapse; font-size:14px; }}
th,td {{ padding:7px 14px; border-bottom:1px solid #1f2937; text-align:left; }}
th {{ color:#9ca3af; font-size:12px; text-transform:uppercase; }}
footer {{ color:#4b5563; font-size:12px; padding:0 32px 28px; }}
a {{ color:#14F195; }}
</style></head><body>
<header><h1>🟣🟢 Solana Ecosystem <span class="grad">Pulse</span></h1></header>
<div class="updated">Snapshot: {m.get('collected_at')} UTC · auto-updating ·
data: Solana RPC + DeFiLlama + CoinGecko · zero API keys</div>
{alert_html}
<div class="grid">{''.join(cards)}</div>
{top_html}
<footer>Generated by <a href="https://github.com/Pkaulsjs/solana-ecosystem-pulse">
pulse.py</a> — Python stdlib only. Refreshes automatically via GitHub Actions.</footer>
</body></html>'''


def build_markdown(m, alerts):
    L = []
    L.append("# Solana Ecosystem Pulse")
    L.append(f"\n_Snapshot: {m.get('collected_at')} UTC — auto-generated, zero API keys._\n")
    if alerts:
        L.append("## ⚠ Anomalies\n")
        L += [f"- {a}" for a in alerts]
    else:
        L.append("## ✓ No anomalies detected\n")
    L.append("## Network\n")
    L.append(f"- **Epoch:** {m.get('epoch','?')} ({m.get('epoch_progress_pct','?')}% complete, "
             f"~{m.get('epoch_hours_remaining','?')}h remaining)")
    L.append(f"- **Throughput:** {fmt_num(m.get('tps_avg'))} TPS (60-sample avg), "
             f"slot time {m.get('slot_time_avg_s','?')}s")
    L.append(f"- **Blocks:** height {fmt_num(m.get('block_height'))}, "
             f"total tx {fmt_num(m.get('transactions_count'))}")
    L.append(f"- **Median priority fee:** "
             f"{fmt_num(m.get('median_priority_fee_microlamports'))} μlamports")
    L.append("\n## Validators\n")
    L.append(f"- **Active:** {fmt_num(m.get('validators_active'))} · "
             f"**delinquent:** {m.get('validators_delinquent',0)} "
             f"({m.get('delinquent_stake_pct',0)}% of stake)")
    L.append(f"- **Total stake:** {fmt_num(m.get('total_stake_sol'))} SOL · "
             f"avg commission {m.get('avg_commission_pct','?')}%")
    L.append(f"- **Decentralization:** {m.get('validators_for_33pct_stake','?')} validators "
             f"hold ≥33% of stake")
    L.append("\n## Economy\n")
    L.append(f"- **SOL price:** ${m.get('sol_price_usd','?')} "
             f"(24h {m.get('sol_price_24h_change_pct','?')}%)")
    L.append(f"- **DeFi TVL:** {fmt_usd(m.get('tvl_usd'))} "
             f"(7d {m.get('tvl_7d_change_pct','?')}%, 30d {m.get('tvl_30d_change_pct','?')}%)")
    L.append(f"- **DEX volume 24h:** {fmt_usd(m.get('dex_volume_24h_usd'))}")
    L.append(f"- **Protocol fees 24h:** {fmt_usd(m.get('fees_24h_usd'))} · "
             f"30d {fmt_usd(m.get('fees_30d_usd'))}")
    L.append(f"- **Stablecoin supply:** {fmt_usd(m.get('stablecoin_supply_usd'))}")
    L.append(f"- **SOL supply:** {fmt_num(m.get('sol_circulating'))} circulating / "
             f"{fmt_num(m.get('sol_total'))} total")
    if m.get("top_validators"):
        L.append("\n## Top validators by stake\n")
        L.append("| Validator | Stake (SOL) | Commission |")
        L.append("|---|---|---|")
        for v in m["top_validators"]:
            L.append(f"| {v['name']} | {v['stake_sol']:,.0f} | {v['commission']}% |")
    L.append("\n---\n_Data: Solana JSON-RPC, DeFiLlama, CoinGecko. "
             "See [dashboard](dashboard.html) for the interactive view._")
    return "\n".join(L)


# --------------------------------------------------------------------- main

def main():
    REPORTS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    print("Collecting network metrics…")
    m = {}
    m.update(collect_network())
    print("Collecting validators…")
    m.update(collect_validators())
    print("Collecting supply…")
    m.update(collect_supply())
    print("Collecting DeFiLlama ecosystem data…")
    m.update(collect_defillama())
    print("Collecting price (best-effort)…")
    m.update(collect_price())
    m["collected_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    m["collected_at_ts"] = int(time.time())

    hist = load_history()
    m["tvl_24h_change_pct"] = tvl_24h_from_history(m, hist)
    alerts = detect_anomalies(m, hist)
    print(f"Anomalies: {len(alerts)}")

    # sparkline series from our own history (last 30 samples)
    def series(key):
        vals = [h["metrics"].get(key) for h in hist[-30:]]
        return [v for v in vals if isinstance(v, (int, float))]
    m["tps_history"] = series("tps_avg")
    m["price_history"] = series("sol_price_usd")

    # append history (keep metrics only, trimmed of bulky series)
    slim = {k: v for k, v in m.items()
            if not k.endswith("_history") and k != "top_validators"}
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": m["collected_at_ts"], "metrics": slim}) + "\n")

    (REPORTS / "report.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    (REPORTS / "report.md").write_text(build_markdown(m, alerts), encoding="utf-8")
    (REPORTS / "dashboard.html").write_text(build_dashboard(m, alerts), encoding="utf-8")
    print(f"Done. reports/report.md, report.json, dashboard.html written. "
          f"History samples: {len(hist) + 1}")


if __name__ == "__main__":
    main()
