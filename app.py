"""
Bank Nifty Box Spread Opportunity Finder - Streamlit UI
Run:  python -m streamlit run app.py
Education / research only. Not investment advice.
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pandas as pd
import streamlit as st

import core
from core import (CostConfig, FilterConfig, LOT_SIZE, REQUIRED_COLS, angel_banknifty_options,
                  angel_login, angel_monthly_expiries, atm_band, fetch_angel_chain, fetch_live_chain,
                  load_angel_master,
                  find_opportunities, format_alert, get_monthly_expiries, is_monthly_expiry,
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
    if viable.empty:
        st.info("No arbitrage found after costs and filters.")
        if all_df.empty:
            st.write("No strike pair passed the liquidity/spread filters, so there are no candidates to show.")
        else:
            st.subheader("Closest 3 candidates (not profitable)")
            st.dataframe(all_df.head(3)[DISPLAY_COLS], hide_index=True, width="stretch")
        shown = all_df.head(3)
    else:
        st.success(f"{len(viable)} viable opportunity(ies) found.")
        st.subheader("Ranked opportunities (NET profit per lot)")
        st.dataframe(viable[DISPLAY_COLS + ["Mid NET Profit"]], hide_index=True, width="stretch")
        shown = viable
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


def main():
    st.set_page_config(page_title="Bank Nifty Box Spread Finder", layout="wide")
    st.title("Bank Nifty Box Spread Opportunity Finder")
    st.warning(DISCLAIMER)
    sb = st.sidebar

    sb.header("Contract")
    lot = sb.number_input("Lot size", 1, 500, LOT_SIZE)
    band = sb.number_input("ATM band (+/- points)", 100, 5000, 1000, 100,
                           help="Only strikes within this distance of ATM are scanned (live mode, "
                                "or CSV mode when a spot price is entered).")

    sb.header("Liquidity & safety filters")
    f = FilterConfig(
        min_oi=sb.number_input("Min OI (CE & PE)", 0, 10**7, 500, 100),
        min_volume=sb.number_input("Min volume (CE & PE)", 0, 10**7, 100, 50),
        max_spread_pct=sb.slider("Max bid-ask spread % of mid", 0.1, 10.0, 1.5, 0.1),
        min_edge_pct=sb.slider("Min NET edge % of strike width", 0.0, 5.0, 0.1, 0.05),
        min_width=sb.number_input("Min strike width", 0, 5000, 200, 100),
        max_width=sb.number_input("Max strike width", 0, 5000, 1000, 100),
    )
    sb.header("Transaction costs")
    c = CostConfig(
        brokerage_per_order=sb.number_input("Brokerage / order (Rs)", 0.0, 500.0, 20.0, 1.0),
        stt_sell_pct=sb.number_input("STT on sell premium (%)", 0.0, 1.0, 0.0125, 0.0025, format="%.4f"),
        exch_pct=sb.number_input("Exchange charges (% premium)", 0.0, 1.0, 0.03503, 0.0001, format="%.5f"),
        sebi_per_crore=sb.number_input("SEBI fee (Rs / crore)", 0.0, 100.0, 10.0, 1.0),
        gst_pct=sb.number_input("GST (%)", 0.0, 30.0, 18.0, 1.0),
        stamp_buy_pct=sb.number_input("Stamp duty on buy premium (%)", 0.0, 1.0, 0.003, 0.001, format="%.4f"),
        exercise_stt_pct=sb.number_input("STT on ITM exercise at expiry (% intrinsic)", 0.0, 1.0, 0.125,
                                         0.005, format="%.4f"),
        slippage=sb.number_input("Slippage per leg (Rs)", 0.0, 10.0, 0.05, 0.05),
    )

    mode = st.radio("Data source", ["Live (Upstox)", "Upload CSV", "Use sample file", "Manual entry"],
                    horizontal=True)

    # ------------------------------------------------------------------ LIVE
    if mode == "Live (Upstox)":
        sb.header("Live data")
        provider = sb.radio("Broker", ["Upstox", "Angel One"], horizontal=True)
        refresh_s = sb.slider("Auto-refresh every (seconds)", 5 if provider == "Upstox" else 10, 120, 15)
        auto = sb.checkbox("Auto-refresh", True)
        token = conn = opts = None
        if provider == "Upstox":
            token = sb.text_input("Upstox access token", os.getenv("UPSTOX_ACCESS_TOKEN", ""), type="password")
        else:
            a_key = sb.text_input("Angel API key", os.getenv("ANGEL_API_KEY", ""), type="password")
            a_client = sb.text_input("Angel client code", os.getenv("ANGEL_CLIENT_CODE", ""))
            a_pin = sb.text_input("Angel PIN", os.getenv("ANGEL_PIN", ""), type="password")
            a_secret = sb.text_input("TOTP secret (auto login)", os.getenv("ANGEL_TOTP_SECRET", ""), type="password")
            a_code = sb.text_input("...or current 6-digit TOTP", "", help="Used only if no TOTP secret is given.")

        sb.header("Telegram alerts")
        tg_on = sb.checkbox("Send Telegram alerts", False)
        tg_token = sb.text_input("Bot token", os.getenv("TELEGRAM_BOT_TOKEN", ""), type="password")
        tg_chat = sb.text_input("Chat id", os.getenv("TELEGRAM_CHAT_ID", ""))
        cooldown_min = sb.slider("Re-alert same box after (minutes)", 1, 60, 15)
        if sb.button("Send test message"):
            ok, msg = send_telegram(tg_token, tg_chat, "Box spread finder: test message OK.")
            sb.success("Test message sent.") if ok else sb.error(msg)

        try:
            if provider == "Upstox":
                if not token:
                    st.info("Paste your Upstox access token in the sidebar to start live scanning.")
                    return
                expiries = get_monthly_expiries(token)
            else:
                if not (a_key and a_client and a_pin and (a_secret or a_code)):
                    st.info("Fill the Angel One details in the sidebar to start live scanning.")
                    return
                if "angel_conn" not in st.session_state or st.session_state.get("angel_id") != (a_key, a_client):
                    with st.spinner("Logging in to Angel One..."):
                        st.session_state["angel_conn"] = angel_login(a_key, a_client, a_pin, a_secret, a_code)
                        st.session_state["angel_id"] = (a_key, a_client)
                if "angel_opts" not in st.session_state:
                    with st.spinner("Downloading instrument list (large file, once a day)..."):
                        st.session_state["angel_opts"] = angel_banknifty_options(load_angel_master())
                conn, opts = st.session_state["angel_conn"], st.session_state["angel_opts"]
                expiries = angel_monthly_expiries(opts)
        except RuntimeError as e:
            st.session_state.pop("angel_conn", None)
            st.error(str(e))
            return
        if not expiries:
            st.error("No upcoming monthly expiry found.")
            return
        expiry = st.selectbox("Expiry (monthly)", expiries, format_func=lambda d: d.strftime("%d-%b-%Y"))
        st.session_state.setdefault("last_sent", {})

        @st.fragment(run_every=refresh_s if auto else None)
        def live_panel():
            try:
                if provider == "Upstox":
                    raw, spot = fetch_live_chain(token, expiry)
                else:
                    raw, spot = fetch_angel_chain(conn, opts, expiry, band + 200)
                chain, warns = validate(raw)
                chain, atm, interval = atm_band(chain, spot, band)
            except (RuntimeError, ValueError) as e:
                if "session" in str(e).lower():
                    st.session_state.pop("angel_conn", None)
                st.error(str(e))
                return
            open_now = market_open_now()
            st.caption(f"Updated {now_ist():%H:%M:%S} IST | Spot {spot:,.2f} | ATM {atm:,.0f} | "
                       f"strike step {interval:.0f} | scanning {atm - band:,.0f} to {atm + band:,.0f}")
            if not open_now:
                st.warning("Market appears closed (Mon-Fri 09:15-15:30 IST). Quotes may be stale; "
                           "Telegram alerts are paused.")
            all_df, viable = render_scan(chain, expiry, f, c, now_ist().date(), lot, key="live")
            if tg_on and open_now and not viable.empty:
                new = select_new_alerts(viable, st.session_state["last_sent"], cooldown_min * 60)
                for _, r in new.iterrows():
                    ok, msg = send_telegram(tg_token, tg_chat, format_alert(r, spot, atm, int(lot)))
                    st.toast("Telegram alert sent" if ok else f"Telegram failed: {msg}")

        live_panel()
        st.divider()
        st.caption(DISCLAIMER)
        return

    # ------------------------------------------------------------------- CSV
    val_date = sb.date_input("Valuation date", date.today())
    spot_in = sb.number_input("Spot (optional, enables ATM band for CSV)", 0.0, 1e7, 0.0, 1.0)
    allow_non_monthly = sb.checkbox("Allow non-monthly expiries (testing only)", False)
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
    if spot_in > 0:
        try:
            chain, atm, _ = atm_band(chain, spot_in, band)
            st.caption(f"ATM band applied: {atm - band:,.0f} to {atm + band:,.0f}")
        except ValueError as e:
            st.error(str(e))
            return

    expiries = sorted(chain["expiry_date"].unique())
    allowed = expiries if allow_non_monthly else [e for e in expiries if is_monthly_expiry(e)]
    if not allowed:
        st.error("No monthly expiries (last Tuesday of the month) found in the data. "
                 "Tick 'Allow non-monthly expiries' to override.")
        return
    expiry = st.selectbox("Expiry", allowed, format_func=lambda d: d.strftime("%d-%b-%Y"))
    render_scan(chain, expiry, f, c, val_date, lot, key="csv")
    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()
