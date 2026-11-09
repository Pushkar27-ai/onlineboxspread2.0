"""
Bank Nifty / Sensex Box Spread Opportunity Finder - Streamlit UI
All settings live in the right-hand panel (and can be saved/loaded as a file), so no code edits are needed.
Run:  python -m streamlit run app.py
Education / research only. Not investment advice.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import streamlit as st

import core
from core import (CostConfig, FilterConfig, INSTRUMENTS, REQUIRED_COLS, get_angel_options,
                  angel_login, angel_expiries, atm_band, fetch_angel_chain, fetch_live_chain,
                  apply_ltp_fallback, find_opportunities, format_alert, get_expiries, monthly_from_list,
                  load_data, market_open_now, now_ist, select_new_alerts, send_telegram, validate)

DISCLAIMER = (
    "This tool is for education/research only. Box spread arbitrage opportunities are rare "
    "and fleeting; real execution may differ due to liquidity, slippage, and margin. "
    "Not investment advice. Consult a SEBI-registered advisor."
)
DISPLAY_COLS = ["Type", "Expiry", "L", "H", "Strike Width", "Net Debit/Credit", "Gross Profit",
                "Total Costs", "NET Profit", "ROI %", "Annualized %", "Liquidity Score"]


def render_breakdown(row: pd.Series):
    st.markdown(f"**{row['Type']} box  {int(row['L'])} / {int(row['H'])}**  (width {int(row['Strike Width'])})")
    legs = pd.DataFrame(row["_legs"])
    legs.columns = ["Side", "Type", "Strike", "Bid", "Ask", "Mid", "Exec price (incl. slippage)"]
    st.dataframe(legs, hide_index=True, width="stretch")
    cost = {k.replace("_", " ").title(): round(v, 2) for k, v in row["_costs"].items()}
    st.dataframe(pd.DataFrame([cost]), hide_index=True, width="stretch")
    st.caption(f"Mid-price view: gross {row['Mid Gross Profit']:.2f}, net after fees "
               f"{row['Mid NET Profit']:.2f} (no slippage, unrealistic).")


def render_scan(chain, expiry, f, c, val_date, lot, key=""):
    """Run the scan and draw results. Returns (all_df, viable)."""
    all_df, viable, stats = find_opportunities(chain, expiry, f, c, val_date, int(lot))
    st.caption(f"{stats['strikes_liquid']}/{stats['strikes_total']} strikes passed liquidity filters; "
               f"{stats['pairs_checked']} strike pairs checked; {stats['days_to_expiry']} days to expiry.")
    st.caption(f"Rs amounts are for a quantity of {int(lot)} units (lots x lot size). "
               "Width, net debit/credit and 'Short by' are index points per unit.")
    if viable.empty:
        st.info("No arbitrage found after costs and filters.")
        if all_df.empty:
            st.write("No strike pair passed the liquidity/spread filters, so there are no candidates to show.")
            shown = all_df
        else:
            st.subheader("Closest candidates (not profitable)")
            st.caption("'Short by' = how many index points cheaper/richer the box would need to be to break even after all costs.")
            parts = []
            for kind, label in (("DEBIT", "Debit boxes (buy the box)"), ("CREDIT", "Credit boxes (sell the box)")):
                sub = all_df[all_df["Type"] == kind].sort_values("NET Profit", ascending=False).head(3).copy()
                if sub.empty:
                    continue
                sub["Short by (pts)"] = (-sub["NET Profit"] / lot).clip(lower=0).round(2)
                st.markdown(f"**{label}**")
                st.dataframe(sub[DISPLAY_COLS + ["Short by (pts)"]], hide_index=True, width="stretch")
                parts.append(sub)
            shown = pd.concat(parts) if parts else all_df.head(3)
    else:
        st.success(f"{len(viable)} viable opportunity(ies) found.")
        st.subheader(f"Ranked opportunities (by {f.sort_by})")
        shown = viable.head(int(f.max_results))
        st.dataframe(shown[DISPLAY_COLS + ["Mid NET Profit"]], hide_index=True, width="stretch")
    if not all_df.empty:
        st.subheader("Leg-by-leg breakdown")
        for _, r in shown.iterrows():
            with st.expander(f"{r['Type']} {int(r['L'])}/{int(r['H'])}  |  NET Rs {r['NET Profit']:.2f}"):
                render_breakdown(r)
        export = shown.drop(columns=["_legs", "_costs", "_edge_pct"])
        st.download_button("Export results to CSV", export.to_csv(index=False).encode(),
                           file_name=f"box_spreads_{datetime.now():%Y%m%d_%H%M%S}.csv",
                           mime="text/csv", key=f"dl_{key}")
    return all_df, viable


WEEKDAYS = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}
MODES = ["Live (broker)", "Upload CSV", "Use sample file", "Manual entry"]


# ---- small widget helpers: the session_state default is set first, so saved settings can be loaded ----
def ni(label, key, default, **kw):
    st.session_state.setdefault(key, default)
    return st.number_input(label, key=key, **kw)


def sl(label, key, default, **kw):
    st.session_state.setdefault(key, default)
    return st.slider(label, key=key, **kw)


def cb(label, key, default, **kw):
    st.session_state.setdefault(key, default)
    return st.checkbox(label, key=key, **kw)


def tx(label, key, default, **kw):
    st.session_state.setdefault(key, default)
    return st.text_input(label, key=key, **kw)


def sx(label, options, key, default, **kw):
    st.session_state.setdefault(key, default)
    return st.selectbox(label, options, key=key, **kw)


def _settings_json() -> str:
    """Only non-secret settings (keys starting with cfg_). Tokens, PIN and TOTP are never included."""
    d = {k: v for k, v in st.session_state.items()
         if k.startswith("cfg_") and isinstance(v, (int, float, str, bool))}
    return json.dumps(d, indent=2)


def _apply_imported_settings():
    up = st.session_state.get("imp_file")
    if up is None:
        st.session_state["imp_msg"] = "Choose a settings file first."
        return
    try:
        data = json.load(up)
        n = 0
        for k, v in data.items():
            if k.startswith("cfg_") and isinstance(v, (int, float, str, bool)):
                st.session_state[k] = v
                n += 1
        st.session_state["imp_msg"] = f"Loaded {n} setting(s)."
    except Exception as e:  # noqa: BLE001
        st.session_state["imp_msg"] = f"Could not read the settings file: {e}"


def settings_panel(right, mode) -> SimpleNamespace:
    """Draw every setting in the right-hand column and return the chosen values."""
    S = SimpleNamespace()
    with right:
        st.subheader("Settings")

        with st.expander("Contract", expanded=True):
            S.underlying = sx("Underlying", list(INSTRUMENTS), "cfg_underlying", "BANKNIFTY",
                              format_func=lambda k: INSTRUMENTS[k]["label"])
            u = S.underlying
            base = INSTRUMENTS[u]
            S.lot = ni("Lot size", f"cfg_lot_{u}", base["lot"], min_value=1, max_value=500, step=1)
            S.lots = ni("Number of lots", "cfg_lots", 1, min_value=1, max_value=1000, step=1,
                        help="Profit, costs and ROI are calculated for lots x lot size units. Brokerage is per "
                             "order, so it does not grow with the number of lots.")
            S.qty = S.lot * S.lots
            S.band = ni("ATM band (+/- points)", "cfg_band", 1000, min_value=100, max_value=5000, step=100,
                        help="Only strikes within this distance of ATM are scanned (live mode, or CSV mode "
                             "when a spot price is entered).")
            if u == "SENSEX":
                st.caption("Sensex trades on BSE. Its exchange charge differs from NSE: set it under "
                           "Transaction costs.")

        with st.expander("Instrument settings (advanced)"):
            st.caption("Change these if the exchange revises a contract or a broker key/token is rejected. "
                       "No code edit needed.")
            cfg = dict(base)
            cfg["lot"] = S.lot
            default_wd = [k for k, v in WEEKDAYS.items() if v == base["weekday"]][0]
            cfg["weekday"] = WEEKDAYS[sx("Monthly expiry weekday", list(WEEKDAYS), f"cfg_{u}_weekday", default_wd)]
            cfg["has_weekly"] = cb("Has weekly expiries", f"cfg_{u}_hasweekly", base["has_weekly"])
            cfg["upstox_key"] = tx("Upstox instrument key", f"cfg_{u}_ukey", base["upstox_key"]).strip()
            cfg["angel_seg"] = tx("Angel segment (NFO / BFO)", f"cfg_{u}_aseg", base["angel_seg"]).strip()
            cfg["angel_name"] = tx("Angel instrument name", f"cfg_{u}_aname", base["angel_name"]).strip()
            sp_ex = tx("Angel spot exchange", f"cfg_{u}_spex", base["angel_spot"][0]).strip()
            sp_tok = tx("Angel spot index token", f"cfg_{u}_sptok", base["angel_spot"][1]).strip()
            cfg["angel_spot"] = (sp_ex, sp_tok)
        S.icfg = cfg

        with st.expander("Box rules (type, width, range, profit)", expanded=True):
            allow_debit = cb("Debit boxes (buy the box)", "cfg_debit", True)
            allow_credit = cb("Credit boxes (sell the box)", "cfg_credit", True)
            min_width = ni("Min strike width (points)", "cfg_min_width", 200, min_value=0, max_value=5000, step=100)
            max_width = ni("Max strike width (points)", "cfg_max_width", 1000, min_value=0, max_value=5000, step=100)
            strike_low = ni("Lowest strike to use (0 = off)", "cfg_strike_low", 0, min_value=0, max_value=10**6, step=100)
            strike_high = ni("Highest strike to use (0 = off)", "cfg_strike_high", 0, min_value=0, max_value=10**6, step=100)
            min_net_profit = ni("Min NET profit (Rs, after costs)", "cfg_min_net", 0.0,
                                min_value=0.0, max_value=1e7, step=50.0)
            min_edge = sl("Min NET edge % of strike width", "cfg_min_edge", 0.1, min_value=0.0, max_value=5.0, step=0.05)
            min_roi = ni("Min ROI %", "cfg_min_roi", 0.0, min_value=0.0, max_value=100.0, step=0.1)
            min_ann = ni("Min annualized %", "cfg_min_ann", 0.0, min_value=0.0, max_value=1000.0, step=1.0)
            sort_by = sx("Sort results by", ["NET Profit", "ROI %", "Annualized %"], "cfg_sort", "NET Profit")
            max_results = ni("Max rows to show", "cfg_max_rows", 20, min_value=1, max_value=200, step=5)

        with st.expander("Liquidity & safety filters"):
            S.f = FilterConfig(
                min_oi=ni("Min OI (CE & PE)", "cfg_min_oi", 500, min_value=0, max_value=10**7, step=100),
                min_volume=ni("Min volume (CE & PE)", "cfg_min_vol", 100, min_value=0, max_value=10**7, step=50),
                max_spread_pct=sl("Max bid-ask spread % of mid", "cfg_max_spread", 1.5,
                                  min_value=0.1, max_value=10.0, step=0.1),
                min_edge_pct=min_edge, min_width=min_width, max_width=max_width,
                allow_debit=allow_debit, allow_credit=allow_credit, min_net_profit=min_net_profit,
                min_roi_pct=min_roi, min_annualized_pct=min_ann, strike_low=strike_low,
                strike_high=strike_high, sort_by=sort_by, max_results=max_results,
            )

        with st.expander("Transaction costs"):
            S.c = CostConfig(
                brokerage_per_order=ni("Brokerage / order (Rs)", "cfg_brokerage", 20.0,
                                       min_value=0.0, max_value=500.0, step=1.0),
                stt_sell_pct=ni("STT on sell premium (%)", "cfg_stt", 0.0125,
                                min_value=0.0, max_value=1.0, step=0.0025, format="%.4f"),
                exch_pct=ni("Exchange charges (% premium)", f"cfg_exch_{u}", base["exch_pct"],
                            min_value=0.0, max_value=1.0, step=0.0001, format="%.5f"),
                sebi_per_crore=ni("SEBI fee (Rs / crore)", "cfg_sebi", 10.0, min_value=0.0, max_value=100.0, step=1.0),
                gst_pct=ni("GST (%)", "cfg_gst", 18.0, min_value=0.0, max_value=30.0, step=1.0),
                stamp_buy_pct=ni("Stamp duty on buy premium (%)", "cfg_stamp", 0.003,
                                 min_value=0.0, max_value=1.0, step=0.001, format="%.4f"),
                exercise_stt_pct=ni("STT on ITM exercise at expiry (% intrinsic)", "cfg_exstt", 0.125,
                                    min_value=0.0, max_value=1.0, step=0.005, format="%.4f"),
                slippage=ni("Slippage per leg (Rs)", "cfg_slip", 0.05, min_value=0.0, max_value=10.0, step=0.05),
            )

        if mode == MODES[0]:
            with st.expander("Live data", expanded=True):
                st.session_state.setdefault("cfg_provider", "Upstox")
                S.provider = st.radio("Broker", ["Upstox", "Angel One"], key="cfg_provider", horizontal=True)
                S.refresh_s = sl("Auto-refresh every (seconds)", "cfg_refresh", 15, min_value=5, max_value=120, step=1)
                S.auto = cb("Auto-refresh", "cfg_auto", True)
                S.weekly = cb("Include weekly expiries", "cfg_weekly", True) if cfg["has_weekly"] else False
                S.use_ltp = cb("Market closed: use last traded prices (indicative)", "cfg_useltp", True,
                               help="After 3:30 PM bid/ask is usually empty. This fills gaps with the last traded "
                                    "price so you can see whether a box existed at the close.")
                S.token = S.a_key = S.a_client = S.a_pin = S.a_secret = S.a_code = ""
                if S.provider == "Upstox":
                    S.token = st.text_input("Upstox access token", os.getenv("UPSTOX_ACCESS_TOKEN", ""), type="password")
                else:
                    S.a_key = st.text_input("Angel API key", os.getenv("ANGEL_API_KEY", ""), type="password")
                    S.a_client = st.text_input("Angel client code", os.getenv("ANGEL_CLIENT_CODE", ""))
                    S.a_pin = st.text_input("Angel PIN", os.getenv("ANGEL_PIN", ""), type="password")
                    S.a_secret = st.text_input("TOTP secret (auto login)", os.getenv("ANGEL_TOTP_SECRET", ""),
                                               type="password")
                    S.a_code = st.text_input("...or current 6-digit TOTP", "", help="Used only if no TOTP secret.")
            with st.expander("Telegram alerts"):
                S.tg_on = cb("Send Telegram alerts", "cfg_tgon", False)
                S.tg_token = st.text_input("Bot token", os.getenv("TELEGRAM_BOT_TOKEN", ""), type="password")
                S.tg_chat = st.text_input("Chat id", os.getenv("TELEGRAM_CHAT_ID", ""))
                S.cooldown_min = sl("Re-alert same box after (minutes)", "cfg_cooldown", 15,
                                    min_value=1, max_value=60, step=1)
                if st.button("Send test message"):
                    ok, msg = send_telegram(S.tg_token, S.tg_chat, "Box spread finder: test message OK.")
                    if ok:
                        st.success("Test message sent.")
                    else:
                        st.error(msg)
        else:
            with st.expander("CSV options", expanded=True):
                S.val_date = st.date_input("Valuation date", date.today())
                S.spot_in = st.number_input("Spot (optional, enables ATM band)", 0.0, 1e7, 0.0, 1.0)
                S.allow_non_monthly = cb("Allow non-monthly expiries (testing only)", "cfg_allow_nm", False)

        with st.expander("Save / load settings"):
            st.caption("Saves all settings above (never tokens, PINs or secrets). Load the file next time "
                       "instead of re-entering everything.")
            st.download_button("Download settings file", _settings_json(),
                               file_name="boxspread_settings.json", mime="application/json")
            st.file_uploader("Load settings file", type="json", key="imp_file")
            st.button("Apply loaded settings", on_click=_apply_imported_settings)
            if st.session_state.get("imp_msg"):
                st.caption(st.session_state["imp_msg"])
    return S


def run_live(S):
    icfg, u = S.icfg, S.underlying
    try:
        if S.provider == "Upstox":
            if not S.token:
                st.info("Paste your Upstox access token in the Settings panel (Live data) to start.")
                return
            expiries = get_expiries(S.token, u, S.weekly, cfg=icfg)
            conn = opts = None
        else:
            if not (S.a_key and S.a_client and S.a_pin and (S.a_secret or S.a_code)):
                st.info("Fill the Angel One details in the Settings panel (Live data) to start.")
                return
            if "angel_conn" not in st.session_state or st.session_state.get("angel_id") != (S.a_key, S.a_client):
                with st.spinner("Logging in to Angel One..."):
                    st.session_state["angel_conn"] = angel_login(S.a_key, S.a_client, S.a_pin, S.a_secret, S.a_code)
                    st.session_state["angel_id"] = (S.a_key, S.a_client)
            okey = f"angel_opts_{icfg['angel_seg']}_{icfg['angel_name']}"
            if okey not in st.session_state:
                with st.spinner("Downloading instrument list (large file, once a day)..."):
                    st.session_state[okey] = get_angel_options(u, cfg=icfg)
            conn, opts = st.session_state["angel_conn"], st.session_state[okey]
            expiries = angel_expiries(opts, u, S.weekly, cfg=icfg)
    except RuntimeError as e:
        st.session_state.pop("angel_conn", None)
        st.error(str(e))
        return
    if not expiries:
        st.error("No upcoming expiry found for this underlying. Check Settings > Instrument settings.")
        return
    expiry = st.selectbox("Expiry (weekly + monthly)" if S.weekly else "Expiry (monthly)", expiries,
                          format_func=lambda d: d.strftime("%d-%b-%Y"))
    st.session_state.setdefault("last_sent", {})
    refresh = S.refresh_s if S.provider == "Upstox" else max(S.refresh_s, 10)

    @st.fragment(run_every=refresh if S.auto else None)
    def live_panel():
        try:
            if S.provider == "Upstox":
                raw, spot = fetch_live_chain(S.token, expiry, u, cfg=icfg)
            else:
                raw, spot = fetch_angel_chain(conn, opts, expiry, S.band + 200, u, cfg=icfg)
            chain, _ = validate(raw)
            chain, atm, interval = atm_band(chain, spot, S.band)
        except (RuntimeError, ValueError) as e:
            if "session" in str(e).lower():
                st.session_state.pop("angel_conn", None)
            st.error(str(e))
            return
        open_now = market_open_now()
        st.caption(f"Updated {now_ist():%H:%M:%S} IST | Spot {spot:,.2f} | ATM {atm:,.0f} | "
                   f"strike step {interval:.0f} | scanning {atm - S.band:,.0f} to {atm + S.band:,.0f}")
        if open_now:
            st.session_state["snapshot"] = {"chain": chain.copy(), "time": now_ist(), "spot": spot}
        else:
            st.warning("Market is closed (Mon-Fri 09:15-15:30 IST). Showing the last data the broker "
                       "returns. Telegram alerts are paused.")
            if S.use_ltp:
                chain, n_filled = apply_ltp_fallback(chain)
                if n_filled:
                    st.info(f"INDICATIVE RESULT: {n_filled} missing bid/ask quote(s) were replaced by the last "
                            "traded price. Last prices are not simultaneous and not executable, so treat any "
                            "'opportunity' below as a hint to check at the next open, not a real trade.")
        snap = st.session_state.get("snapshot")
        if snap is not None:
            st.download_button(f"Download last in-session chain ({snap['time']:%d-%b %H:%M} IST)",
                               snap["chain"].to_csv(index=False).encode(),
                               file_name=f"{u.lower()}_chain_{snap['time']:%Y%m%d_%H%M}.csv",
                               mime="text/csv", key="dl_snap")
        _, viable = render_scan(chain, expiry, S.f, S.c, now_ist().date(), S.qty, key="live")
        if S.tg_on and open_now and not viable.empty:
            new = select_new_alerts(viable, st.session_state["last_sent"], S.cooldown_min * 60)
            for _, r in new.iterrows():
                ok, msg = send_telegram(S.tg_token, S.tg_chat, format_alert(r, spot, atm, int(S.qty), icfg["short"]))
                st.toast("Telegram alert sent" if ok else f"Telegram failed: {msg}")

    live_panel()


def run_csv(S, mode):
    try:
        if mode == "Upload CSV":
            up = st.file_uploader("Option chain CSV", type="csv")
            raw = load_data(up) if up is not None else None
        elif mode == "Use sample file":
            raw = load_data("sample_option_chain.csv")
        else:
            template = pd.DataFrame([{k: None for k in REQUIRED_COLS}] * 6)
            template["expiry_date"] = ""
            edited = st.data_editor(template, num_rows="dynamic", width="stretch")
            raw = load_data(edited.dropna(how="all"))
        if raw is None:
            st.info("Provide an option chain to begin.")
            return
        chain, warns = validate(raw)
    except FileNotFoundError:
        st.error("sample_option_chain.csv not found next to app.py.")
        return
    except pd.errors.EmptyDataError:
        st.error("The uploaded CSV is empty.")
        return
    except ValueError as e:
        st.error(str(e))
        return
    for w in warns:
        st.caption(f"Note: {w}")
    if S.spot_in > 0:
        try:
            chain, atm, _ = atm_band(chain, S.spot_in, S.band)
            st.caption(f"ATM band applied: {atm - S.band:,.0f} to {atm + S.band:,.0f}")
        except ValueError as e:
            st.error(str(e))
            return
    expiries = sorted(chain["expiry_date"].unique())
    allowed = expiries if S.allow_non_monthly else monthly_from_list(expiries)
    if not allowed:
        st.error("No monthly expiry found in the data. Tick 'Allow non-monthly expiries' to override.")
        return
    expiry = st.selectbox("Expiry", allowed, format_func=lambda d: d.strftime("%d-%b-%Y"))
    render_scan(chain, expiry, S.f, S.c, S.val_date, S.qty, key="csv")


def main():
    st.set_page_config(page_title="Box Spread Finder", layout="wide")
    main_col, right = st.columns([3, 1.25], gap="large")
    with main_col:
        st.title("Bank Nifty / Sensex Box Spread Opportunity Finder")
        st.warning(DISCLAIMER)
        st.session_state.setdefault("cfg_mode", MODES[0])
        mode = st.radio("Data source", MODES, key="cfg_mode", horizontal=True)
    S = settings_panel(right, mode)          # right-hand settings panel
    with main_col:
        if mode == MODES[0]:
            run_live(S)
        else:
            run_csv(S, mode)
        st.divider()
        st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()
