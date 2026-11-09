"""
core.py - logic for the Bank Nifty Box Spread Finder (no Streamlit dependency).
Scan engine + cost model + Upstox live data + Telegram alerts.
Education / research only. Not investment advice.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
import requests
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# CONFIG (all constants are configurable from the UI sidebar as well)
# ----------------------------------------------------------------------------
OPTIONAL_COLS = ["ce_ltp", "pe_ltp"]   # last traded price, used only for the after-hours fallback

LOT_SIZE = 30  # BANKNIFTY lot size (as specified; change here if NSE revises)

REQUIRED_COLS = [
    "strike", "ce_bid", "ce_ask", "ce_oi", "ce_volume",
    "pe_bid", "pe_ask", "pe_oi", "pe_volume", "expiry_date",
]


@dataclass
class CostConfig:
    brokerage_per_order: float = 20.0   # Rs per order (flat), 1 order per leg
    stt_sell_pct: float = 0.0125        # % of premium, sell side only
    exch_pct: float = 0.03503           # % of premium, all legs (NSE)
    sebi_per_crore: float = 10.0        # Rs per crore of premium
    gst_pct: float = 18.0               # % on brokerage + exchange + SEBI
    stamp_buy_pct: float = 0.003        # % of premium, buy side only
    exercise_stt_pct: float = 0.125     # % of intrinsic value on ITM long legs
    #                                     exercised/settled at expiry (see README)
    slippage: float = 0.05              # Rs per leg beyond bid/ask (1 tick)


@dataclass
class FilterConfig:
    min_oi: int = 500
    min_volume: int = 100
    max_spread_pct: float = 1.5         # % of mid, per leg
    min_edge_pct: float = 0.1           # min NET profit as % of strike width
    min_width: float = 200.0
    max_width: float = 1000.0


# ----------------------------------------------------------------------------
# DATA LOADING & VALIDATION
# ----------------------------------------------------------------------------
def load_data(source) -> pd.DataFrame:
    """Load an option chain from a CSV path / file-like object / DataFrame."""
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        df = pd.read_csv(source)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def validate(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Clean the raw chain. Returns (clean_df, warnings).
    Raises ValueError (friendly message) if the data is unusable.
    """
    warnings: list[str] = []
    if df is None or df.empty:
        raise ValueError("The option chain is empty. Upload a CSV with at least a few strikes.")

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")

    keep = REQUIRED_COLS + [c for c in OPTIONAL_COLS if c in df.columns]
    df = df[keep].copy()
    num_cols = [c for c in keep if c != "expiry_date"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce").dt.date

    before = len(df)
    df = df.dropna(subset=["strike", "expiry_date"])
    if len(df) < before:
        warnings.append(f"Dropped {before - len(df)} row(s) with missing strike/expiry.")

    before = len(df)
    df = df.drop_duplicates(subset=["expiry_date", "strike"], keep="first")
    if len(df) < before:
        warnings.append(f"Removed {before - len(df)} duplicate strike row(s).")

    if df.empty:
        raise ValueError("No usable rows after cleaning. Check strike and expiry_date columns.")
    return df.sort_values(["expiry_date", "strike"]).reset_index(drop=True), warnings


def monthly_from_list(dates) -> list[date]:
    """
    Monthly expiry = the LAST expiry date of each calendar month in the list.
    This handles holiday shifts (e.g. Tuesday holiday -> Monday expiry) that a
    fixed 'last Tuesday' rule would miss.
    """
    last: dict[tuple[int, int], date] = {}
    for d in sorted(set(dates)):
        last[(d.year, d.month)] = d
    return sorted(last.values())


def is_monthly_expiry(d: date) -> bool:
    """True if d is the last Tuesday of its month (Bank Nifty monthly expiry)."""
    last_day = calendar.monthrange(d.year, d.month)[1]
    last = date(d.year, d.month, last_day)
    offset = (last.weekday() - 1) % 7  # Tuesday == 1
    return d == date(d.year, d.month, last_day - offset)


# ----------------------------------------------------------------------------
# COSTS
# ----------------------------------------------------------------------------
def compute_costs(legs: list[dict], width: float, cfg: CostConfig, lot: int = LOT_SIZE) -> dict:
    """
    Fees for the 4 legs (position is held to expiry, so no closing orders).

    legs: [{"side": "BUY"|"SELL", "price": execution price per unit}, ...]
    Returns a dict with a component breakdown and "total".
    """
    brokerage = stt = exch = sebi = stamp = 0.0
    for leg in legs:
        prem = leg["price"] * lot
        brokerage += cfg.brokerage_per_order
        exch += prem * cfg.exch_pct / 100
        sebi += prem * cfg.sebi_per_crore / 1e7
        if leg["side"] == "SELL":
            stt += prem * cfg.stt_sell_pct / 100
        else:
            stamp += prem * cfg.stamp_buy_pct / 100
    gst = (brokerage + exch + sebi) * cfg.gst_pct / 100
    # Long ITM legs settle at expiry: their combined intrinsic ~= strike width
    # for any expiry level inside [L, H] (and >= width outside). STT is levied
    # on the buyer on that intrinsic value.
    exercise_stt = width * lot * cfg.exercise_stt_pct / 100
    total = brokerage + stt + exch + sebi + gst + stamp + exercise_stt
    return {
        "brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi,
        "gst": gst, "stamp": stamp, "exercise_stt": exercise_stt, "total_fees": total,
    }


# ----------------------------------------------------------------------------
# CORE SCAN
# ----------------------------------------------------------------------------
def _leg_ok(bid: float, ask: float, max_spread_pct: float) -> bool:
    if np.isnan(bid) or np.isnan(ask) or bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 <= max_spread_pct


def _strike_ok(r: pd.Series, f: FilterConfig) -> bool:
    """Strike-level liquidity + price sanity (both CE and PE must pass)."""
    vals = [r.ce_oi, r.pe_oi, r.ce_volume, r.pe_volume]
    if any(pd.isna(v) for v in vals):
        return False
    if min(r.ce_oi, r.pe_oi) < f.min_oi or min(r.ce_volume, r.pe_volume) < f.min_volume:
        return False
    return True


def _liquidity_score(rows: list[pd.Series], f: FilterConfig) -> float:
    """0-100. Half from depth (min OI/volume vs thresholds), half from tight spreads."""
    depth = min(
        min(min(r.ce_oi, r.pe_oi) / (10 * f.min_oi) for r in rows),
        min(min(r.ce_volume, r.pe_volume) / (10 * f.min_volume) for r in rows),
        1.0,
    )
    tight = []
    for r in rows:
        for b, a in ((r.ce_bid, r.ce_ask), (r.pe_bid, r.pe_ask)):
            m = (a + b) / 2
            tight.append(max(0.0, 1 - ((a - b) / m * 100) / f.max_spread_pct))
    return round(50 * depth + 50 * float(np.mean(tight)), 1)


def find_opportunities(
    chain: pd.DataFrame,
    expiry: date,
    f: FilterConfig,
    c: CostConfig,
    valuation_date: date,
    lot: int = LOT_SIZE,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Evaluate every (L, H) pair for both DEBIT and CREDIT boxes.

    Returns (all_evaluated, viable, stats)
      all_evaluated: every pair that passed liquidity/price filters (with NET profit,
                     possibly negative) - used for "closest candidates".
      viable:        subset with NET profit > 0 and >= min_edge_pct of width.
    """
    d = chain[chain["expiry_date"] == expiry].sort_values("strike")
    days = (expiry - valuation_date).days
    stats = {"strikes_total": len(d), "strikes_liquid": 0, "pairs_checked": 0, "days_to_expiry": days}

    liquid = [r for _, r in d.iterrows() if _strike_ok(r, f)]
    stats["strikes_liquid"] = len(liquid)

    rows: list[dict] = []
    for i, lo in enumerate(liquid):
        for hi in liquid[i + 1:]:
            L, H = float(lo.strike), float(hi.strike)
            width = H - L
            if H <= L or width < f.min_width or width > f.max_width:
                continue
            stats["pairs_checked"] += 1

            for kind in ("DEBIT", "CREDIT"):
                if kind == "DEBIT":
                    # Buy CE@L, Sell CE@H, Buy PE@H, Sell PE@L
                    spec = [
                        ("BUY", "CE", L, lo.ce_bid, lo.ce_ask),
                        ("SELL", "CE", H, hi.ce_bid, hi.ce_ask),
                        ("BUY", "PE", H, hi.pe_bid, hi.pe_ask),
                        ("SELL", "PE", L, lo.pe_bid, lo.pe_ask),
                    ]
                else:
                    # Reverse box: Sell CE@L, Buy CE@H, Sell PE@H, Buy PE@L
                    spec = [
                        ("SELL", "CE", L, lo.ce_bid, lo.ce_ask),
                        ("BUY", "CE", H, hi.ce_bid, hi.ce_ask),
                        ("SELL", "PE", H, hi.pe_bid, hi.pe_ask),
                        ("BUY", "PE", L, lo.pe_bid, lo.pe_ask),
                    ]
                if not all(_leg_ok(b, a, f.max_spread_pct) for _, _, _, b, a in spec):
                    continue

                legs, bidask_cash, mid_cash, slip_cost = [], 0.0, 0.0, 0.0
                for side, typ, k, bid, ask in spec:
                    mid = (bid + ask) / 2
                    base = ask if side == "BUY" else bid          # realistic quote
                    exe = base + c.slippage if side == "BUY" else max(base - c.slippage, 0.0)
                    sign = 1 if side == "BUY" else -1             # + = cash out
                    bidask_cash += sign * base
                    mid_cash += sign * mid
                    slip_cost += c.slippage * lot
                    legs.append({"side": side, "type": typ, "strike": k, "bid": bid, "ask": ask,
                                 "mid": mid, "exec_price": exe})

                # bidask_cash > 0 => net debit ; < 0 => net credit
                if kind == "DEBIT":
                    net_amt = bidask_cash                  # debit paid
                    gross = (width - net_amt) * lot        # per lot, at bid/ask
                    mid_amt = mid_cash
                    mid_gross = (width - mid_amt) * lot
                else:
                    net_amt = -bidask_cash                 # credit received
                    gross = (net_amt - width) * lot
                    mid_amt = -mid_cash
                    mid_gross = (mid_amt - width) * lot

                costs = compute_costs([{"side": l["side"], "price": l["exec_price"]} for l in legs],
                                      width, c, lot)
                fees = costs["total_fees"]
                net_profit = gross - slip_cost - fees

                mid_costs = compute_costs([{"side": l["side"], "price": l["mid"]} for l in legs],
                                          width, c, lot)
                mid_net = mid_gross - mid_costs["total_fees"]

                # Capital base for ROI: debit paid (+fees) for debit box;
                # strike-width notional as a margin proxy for credit box.
                base_cap = (net_amt * lot + fees) if kind == "DEBIT" else width * lot
                roi = net_profit / base_cap * 100 if base_cap > 0 else np.nan
                ann = roi * 365 / days if days > 0 and not np.isnan(roi) else np.nan

                rows.append({
                    "Type": kind, "Expiry": expiry, "L": L, "H": H, "Strike Width": width,
                    "Net Debit/Credit": round(net_amt, 2),
                    "Gross Profit": round(gross, 2),
                    "Total Costs": round(slip_cost + fees, 2),
                    "NET Profit": round(net_profit, 2),
                    "ROI %": round(roi, 3) if not np.isnan(roi) else np.nan,
                    "Annualized %": round(ann, 2) if not np.isnan(ann) else np.nan,
                    "Liquidity Score": _liquidity_score([lo, hi], f),
                    "Mid Gross Profit": round(mid_gross, 2),
                    "Mid NET Profit": round(mid_net, 2),
                    "_legs": legs, "_costs": {**costs, "slippage": slip_cost},
                    "_edge_pct": net_profit / (width * lot) * 100,
                })

    cols = ["Type", "Expiry", "L", "H", "Strike Width", "Net Debit/Credit", "Gross Profit",
            "Total Costs", "NET Profit", "ROI %", "Annualized %", "Liquidity Score",
            "Mid Gross Profit", "Mid NET Profit", "_legs", "_costs", "_edge_pct"]
    all_df = pd.DataFrame(rows, columns=cols)
    all_df = rank(all_df)
    viable = all_df[(all_df["NET Profit"] > 0) & (all_df["_edge_pct"] >= f.min_edge_pct)]
    return all_df, viable.reset_index(drop=True), stats


def rank(df: pd.DataFrame) -> pd.DataFrame:
    """Sort by NET profit (desc), ties broken by liquidity score."""
    if df.empty:
        return df
    return df.sort_values(["NET Profit", "Liquidity Score"], ascending=False).reset_index(drop=True)



# ----------------------------------------------------------------------------
# LIVE DATA (Upstox Put/Call Option Chain API)
# ----------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
UPSTOX_BASE = "https://api.upstox.com/v2"
BANKNIFTY_KEY = "NSE_INDEX|Nifty Bank"


def now_ist() -> datetime:
    return datetime.now(IST)


def market_open_now(now: datetime | None = None) -> bool:
    """NSE cash/F&O session: Mon-Fri 09:15-15:30 IST (holidays not checked)."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) <= (15, 30)


def _upstox_get(path: str, token: str, params: dict) -> dict:
    if not token:
        raise RuntimeError("Upstox access token is missing.")
    try:
        r = requests.get(f"{UPSTOX_BASE}{path}", params=params, timeout=10,
                         headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
    except requests.RequestException as e:
        raise RuntimeError(f"Network error talking to Upstox: {e}") from e
    if r.status_code == 401:
        raise RuntimeError("Upstox rejected the token (401). Tokens expire daily; generate a fresh one.")
    if r.status_code == 429:
        raise RuntimeError("Upstox rate limit hit (429). Increase the refresh interval.")
    if not r.ok:
        raise RuntimeError(f"Upstox error {r.status_code}: {r.text[:200]}")
    return r.json()


def _to_date(v) -> date | None:
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, IST).date()
        return pd.to_datetime(v).date()
    except Exception:
        return None


def last_tuesdays(n: int = 3, today: date | None = None) -> list[date]:
    """Next n monthly (last-Tuesday) expiry dates on/after today. Used as a fallback."""
    today = today or now_ist().date()
    out, y, m = [], today.year, today.month
    while len(out) < n:
        last_day = calendar.monthrange(y, m)[1]
        last = date(y, m, last_day)
        d = date(y, m, last_day - (last.weekday() - 1) % 7)
        if d >= today:
            out.append(d)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def get_monthly_expiries(token: str, n: int = 3) -> list[date]:
    """Monthly Bank Nifty expiries from Upstox; falls back to the last-Tuesday rule."""
    today = now_ist().date()
    try:
        js = _upstox_get("/option/contract", token, {"instrument_key": BANKNIFTY_KEY})
        exps = {_to_date(c.get("expiry")) for c in js.get("data", [])}
        exps = monthly_from_list(e for e in exps if e and e >= today)
        if exps:
            return exps[:n]
    except RuntimeError as e:
        if "token" in str(e).lower() or "401" in str(e):
            raise
    return last_tuesdays(n)


def _num(x):
    try:
        return float(x) if x is not None else np.nan
    except (TypeError, ValueError):
        return np.nan


def parse_upstox_chain(js: dict, expiry: date) -> tuple[pd.DataFrame, float]:
    """Convert the Upstox option-chain JSON into our CSV schema. Returns (df, spot)."""
    rows, spots = [], []
    for item in js.get("data", []) or []:
        ce = (item.get("call_options") or {}).get("market_data") or {}
        pe = (item.get("put_options") or {}).get("market_data") or {}
        spot = _num(item.get("underlying_spot_price"))
        if not np.isnan(spot):
            spots.append(spot)
        rows.append({
            "strike": _num(item.get("strike_price")),
            "ce_bid": _num(ce.get("bid_price")), "ce_ask": _num(ce.get("ask_price")),
            "ce_oi": _num(ce.get("oi")), "ce_volume": _num(ce.get("volume")),
            "pe_bid": _num(pe.get("bid_price")), "pe_ask": _num(pe.get("ask_price")),
            "pe_oi": _num(pe.get("oi")), "pe_volume": _num(pe.get("volume")),
            "expiry_date": expiry,
            "ce_ltp": _num(ce.get("ltp")), "pe_ltp": _num(pe.get("ltp")),
        })
    spot = float(np.median(spots)) if spots else float("nan")
    return pd.DataFrame(rows, columns=REQUIRED_COLS + OPTIONAL_COLS), spot


def fetch_live_chain(token: str, expiry: date) -> tuple[pd.DataFrame, float]:
    js = _upstox_get("/option/chain", token,
                     {"instrument_key": BANKNIFTY_KEY, "expiry_date": expiry.isoformat()})
    df, spot = parse_upstox_chain(js, expiry)
    if df.empty:
        raise RuntimeError("Upstox returned an empty option chain for this expiry.")
    return df, spot


def atm_band(chain: pd.DataFrame, spot: float, band: float) -> tuple[pd.DataFrame, float, float]:
    """Keep strikes within +/- band points of ATM. Strike interval is read from the data."""
    strikes = np.sort(chain["strike"].dropna().unique())
    if len(strikes) < 2 or np.isnan(spot):
        raise ValueError("Cannot determine ATM (need spot price and at least 2 strikes).")
    interval = float(np.median(np.diff(strikes)))
    atm = round(spot / interval) * interval
    out = chain[(chain["strike"] >= atm - band) & (chain["strike"] <= atm + band)]
    return out.reset_index(drop=True), atm, interval


# ----------------------------------------------------------------------------
# TELEGRAM ALERTS
# ----------------------------------------------------------------------------
def send_telegram(bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Telegram bot token / chat id missing."
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                          data={"chat_id": chat_id, "text": text}, timeout=10)
        if r.ok and r.json().get("ok"):
            return True, "sent"
        return False, f"Telegram error {r.status_code}: {r.text[:150]}"
    except requests.RequestException as e:
        return False, f"Network error: {e}"


def format_alert(row: pd.Series, spot: float, atm: float, lot: int = LOT_SIZE) -> str:
    legs = "\n".join(f"  {l['side']:<4} {l['type']} {int(l['strike'])} @ bid {l['bid']:.2f} / ask {l['ask']:.2f}"
                     for l in row["_legs"])
    return (
        f"BOX SPREAD ({row['Type']}) - Bank Nifty {row['Expiry']:%d-%b-%Y}\n"
        f"Spot {spot:.0f} | ATM {atm:.0f} | Strikes {int(row['L'])}/{int(row['H'])} "
        f"(width {int(row['Strike Width'])})\n"
        f"NET profit/lot ({lot}): Rs {row['NET Profit']:.2f} after costs "
        f"(gross {row['Gross Profit']:.2f}, costs {row['Total Costs']:.2f})\n"
        f"ROI {row['ROI %']}% | Annualized {row['Annualized %']}% | Liquidity {row['Liquidity Score']}\n"
        f"{legs}\n"
        f"Quotes at {now_ist():%H:%M:%S} IST. Verify live prices before acting. Not advice."
    )


def select_new_alerts(viable: pd.DataFrame, last_sent: dict, cooldown_s: float,
                      now_ts: float | None = None) -> pd.DataFrame:
    """Rows not alerted within cooldown (key = type, expiry, L, H). Updates last_sent."""
    now_ts = time.time() if now_ts is None else now_ts
    keep = []
    for i, r in viable.iterrows():
        key = (r["Type"], str(r["Expiry"]), r["L"], r["H"])
        if now_ts - last_sent.get(key, -1e18) >= cooldown_s:
            last_sent[key] = now_ts
            keep.append(i)
    return viable.loc[keep]


# ----------------------------------------------------------------------------
# LIVE DATA #2: ANGEL ONE (SmartAPI)
# ----------------------------------------------------------------------------
ANGEL_MASTER_URLS = [
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
]
ANGEL_BANKNIFTY_SPOT = ("NSE", "99926009")   # Nifty Bank index token


def totp_now(secret: str, t: float | None = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 TOTP (SHA1) using only the standard library (no pyotp needed)."""
    clean = secret.strip().replace(" ", "").upper()
    try:
        key = base64.b32decode(clean + "=" * (-len(clean) % 8))
    except Exception as e:
        raise RuntimeError("TOTP secret is not valid (it should be letters A-Z and digits 2-7).") from e
    counter = int((time.time() if t is None else t) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


class AngelREST:
    """Minimal Angel One SmartAPI client over plain REST (no SDK dependency)."""
    BASE = "https://apiconnect.angelone.in"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.jwt: str | None = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
             "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.api_key}
        if self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = requests.post(self.BASE + path, json=body, headers=self._headers(), timeout=15)
            return r.json()
        except requests.RequestException as e:
            raise RuntimeError(f"Network error talking to Angel One: {e}") from e
        except ValueError as e:
            raise RuntimeError("Angel One returned a non-JSON reply (check API key / IP restrictions).") from e

    def generateSession(self, client_code: str, pin: str, totp: str) -> dict:
        js = self._post("/rest/auth/angelbroking/user/v1/loginByPassword",
                        {"clientcode": client_code, "password": pin, "totp": totp})
        if js.get("status") and (js.get("data") or {}).get("jwtToken"):
            self.jwt = js["data"]["jwtToken"]
        return js

    def getMarketData(self, mode: str, exchangeTokens: dict) -> dict:
        return self._post("/rest/secure/angelbroking/market/v1/quote/",
                          {"mode": mode, "exchangeTokens": exchangeTokens})


def angel_login(api_key: str, client_code: str, pin: str, totp_secret: str = "", totp_code: str = "") -> AngelREST:
    """Log in to Angel One. Provide the TOTP secret (auto) or the current 6-digit code."""
    if totp_secret:
        totp_code = totp_now(totp_secret)
    if not (api_key and client_code and pin and totp_code):
        raise RuntimeError("Angel One needs API key, client code, PIN and TOTP (secret or current code).")
    obj = AngelREST(api_key.strip())
    resp = obj.generateSession(client_code.strip(), pin.strip(), totp_code.strip())
    if not resp.get("status"):
        raise RuntimeError(f"Angel One login failed: {resp.get('message', 'unknown error')}")
    return obj


def load_angel_master() -> list[dict]:
    """Download Angel One's instrument master (~40 MB JSON; can be slow). Tries both known URLs."""
    last = "unknown error"
    for url in ANGEL_MASTER_URLS:
        try:
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data
            last = "empty reply"
        except (requests.RequestException, ValueError) as e:
            last = str(e)
    raise RuntimeError(f"Could not download Angel One instrument master: {last}")


_OPTS_CACHE: dict = {}


def get_angel_banknifty_options(force: bool = False) -> pd.DataFrame:
    """BANKNIFTY option contracts, downloaded once per day. Only the small filtered table is kept in memory."""
    today = now_ist().date()
    if not force and _OPTS_CACHE.get("date") == today:
        return _OPTS_CACHE["opts"]
    master = load_angel_master()
    opts = angel_banknifty_options(master)
    del master
    if opts.empty:
        raise RuntimeError("Angel One instrument list had no BANKNIFTY option contracts.")
    _OPTS_CACHE.update(date=today, opts=opts)
    return opts


def angel_banknifty_options(master: list[dict]) -> pd.DataFrame:
    """BANKNIFTY index options from the master: columns expiry, strike, type, token, symbol."""
    rows = []
    for m in master:
        if m.get("exch_seg") != "NFO" or m.get("name") != "BANKNIFTY" or m.get("instrumenttype") != "OPTIDX":
            continue
        sym = str(m.get("symbol", ""))
        typ = sym[-2:]
        if typ not in ("CE", "PE"):
            continue
        try:
            exp = datetime.strptime(str(m["expiry"]).title(), "%d%b%Y").date()
            strike = float(m["strike"]) / 100.0       # master stores strike x 100
        except (KeyError, ValueError):
            continue
        rows.append({"expiry": exp, "strike": strike, "type": typ, "token": str(m["token"]), "symbol": sym})
    return pd.DataFrame(rows, columns=["expiry", "strike", "type", "token", "symbol"])


def angel_monthly_expiries(opts: pd.DataFrame, n: int = 3) -> list[date]:
    today = now_ist().date()
    return monthly_from_list(e for e in opts["expiry"].unique() if e >= today)[:n]


def _angel_quotes(conn, exchange: str, tokens: list[str]) -> dict[str, dict]:
    """FULL-mode quotes keyed by token. Falls back to smaller batches if a batch is rejected."""
    out: dict[str, dict] = {}

    def run(chunk):
        resp = conn.getMarketData("FULL", {exchange: chunk})
        if not resp or not resp.get("status"):
            msg = str((resp or {}).get("message", "no response"))
            if any(k in msg.lower() for k in ("invalid token", "session", "login", "expired", "unauthor")):
                raise RuntimeError(f"Angel One session problem: {msg}")
            raise ValueError(msg)
        for q in (resp.get("data") or {}).get("fetched", []) or []:
            out[str(q.get("symbolToken"))] = q

    for size in (50, 10, 1):
        todo = [t for t in tokens if t not in out]
        if not todo:
            break
        try:
            for i in range(0, len(todo), size):
                run(todo[i:i + size])
                time.sleep(0.4)                     # stay clear of rate limits
        except ValueError:
            continue
    return out


def _best(depth: dict, side: str) -> float:
    lvl = (depth or {}).get(side) or []
    p = _num(lvl[0].get("price")) if lvl else np.nan
    return np.nan if (np.isnan(p) or p <= 0) else p


def fetch_angel_chain(conn, opts: pd.DataFrame, expiry: date, band: float) -> tuple[pd.DataFrame, float]:
    """Spot -> ATM -> quotes for strikes within +/- band -> our CSV schema."""
    ex, tok = ANGEL_BANKNIFTY_SPOT
    spot_q = _angel_quotes(conn, ex, [tok]).get(tok)
    spot = _num((spot_q or {}).get("ltp"))
    if np.isnan(spot) or spot <= 0:
        raise RuntimeError("Could not read Bank Nifty spot from Angel One.")
    e = opts[opts["expiry"] == expiry]
    strikes = np.sort(e["strike"].unique())
    if len(strikes) < 2:
        raise RuntimeError("No Bank Nifty option contracts found for this expiry.")
    interval = float(np.median(np.diff(strikes)))
    atm = round(spot / interval) * interval
    e = e[(e["strike"] >= atm - band) & (e["strike"] <= atm + band)]
    quotes = _angel_quotes(conn, "NFO", list(e["token"]))
    rows = {}
    for _, c in e.iterrows():
        q = quotes.get(c["token"])
        if not q:
            continue
        d = rows.setdefault(c["strike"], {"strike": c["strike"], "expiry_date": expiry})
        p = c["type"].lower()
        d[f"{p}_bid"] = _best(q.get("depth"), "buy")
        d[f"{p}_ask"] = _best(q.get("depth"), "sell")
        d[f"{p}_oi"] = _num(q.get("opnInterest"))
        d[f"{p}_volume"] = _num(q.get("tradeVolume"))
        d[f"{p}_ltp"] = _num(q.get("ltp"))
    df = pd.DataFrame(list(rows.values()))
    for col in REQUIRED_COLS + OPTIONAL_COLS:
        if col not in df.columns:
            df[col] = np.nan
    if df.empty:
        raise RuntimeError("Angel One returned no option quotes (market closed or token problem).")
    return df[REQUIRED_COLS + OPTIONAL_COLS], spot


def apply_ltp_fallback(chain: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    After hours the order book is usually empty (bid/ask = 0). Where bid or ask is missing,
    use the last traded price for both. INDICATIVE ONLY: last prices are not simultaneous
    and not executable. Returns (new_chain, number_of_quotes_filled).
    """
    ch = chain.copy()
    filled = 0
    for p in ("ce", "pe"):
        if f"{p}_ltp" not in ch.columns:
            continue
        for col in (f"{p}_bid", f"{p}_ask", f"{p}_ltp"):
            ch[col] = pd.to_numeric(ch[col], errors="coerce").astype(float)
        bad = (ch[f"{p}_bid"].isna() | ch[f"{p}_ask"].isna() | (ch[f"{p}_bid"] <= 0) | (ch[f"{p}_ask"] <= 0))
        ok = bad & (ch[f"{p}_ltp"] > 0)
        ch.loc[ok, f"{p}_bid"] = ch.loc[ok, f"{p}_ltp"]
        ch.loc[ok, f"{p}_ask"] = ch.loc[ok, f"{p}_ltp"]
        filled += int(ok.sum())
    return ch, filled
