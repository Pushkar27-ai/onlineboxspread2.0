# Bank Nifty Box Spread Opportunity Finder

Scans a BANKNIFTY monthly option chain for debit-box and credit-box arbitrage using
bid/ask pricing, an Indian F&O cost model and liquidity filters.
**Education/research only. Not investment advice.**

## Setup & run
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```
Open the URL Streamlit prints (usually http://localhost:8501). Choose **Use sample file**,
**Upload CSV** or **Manual entry**; all filters and cost constants are in the sidebar.
Keep `sample_option_chain.csv` in the same folder as `app.py`.

## CSV format
`strike, ce_bid, ce_ask, ce_oi, ce_volume, pe_bid, pe_ask, pe_oi, pe_volume, expiry_date`
(expiry as YYYY-MM-DD). Duplicates are removed, NaN prices are skipped, and only
monthly expiries (last Tuesday) are offered unless you tick the testing override.
The sample is synthetic (spot 54,600, expiry 24-Nov-2026) with one planted mispricing
at the 54,300 call so you can see a hit. Set the valuation date to 2026-11-03 for the
same days-to-expiry (21) used in testing.

## Math (short note)
Debit box: buy CE@L, sell CE@H, buy PE@H, sell PE@L. Payoff at expiry = H - L always.
- Net debit = ask(CE_L) + ask(PE_H) - bid(CE_H) - bid(PE_L)
- Gross profit = (Width - Net debit) x lot

Credit (reverse) box: sell CE@L, buy CE@H, sell PE@H, buy PE@L; you owe H - L at expiry.
- Net credit = bid(CE_L) + bid(PE_H) - ask(CE_H) - ask(PE_L)
- Gross profit = (Net credit - Width) x lot

NET profit = Gross - slippage (4 legs x tick x lot) - fees.
ROI = NET / (debit paid + fees) for debit boxes; NET / (Width x lot) for credit boxes
(a margin proxy: real SPAN/exposure margin is broker-specific and not modelled).
Annualized % = ROI x 365 / days to expiry (simple, not compounded).
A mid-price version (no slippage) is shown for comparison only.

## Assumptions and things to check
1. **Credit-box legs.** Your spec lists the same four legs for both box types. A reverse
   box needs the opposite legs, so the code uses sell CE@L / buy CE@H / sell PE@H / buy PE@L.
2. **"Within 10% of strike width" filter.** Read literally it rejects almost every real
   arbitrage (real edges are a fraction of a percent of width). It is replaced by
   `min NET edge % of width` (default 0.1%), adjustable in the sidebar.
3. **STT.** 0.0125% on sell premium is the figure you supplied, and it is what the default uses.
   Please verify current NSE/government STT rates for index options (my recollection is that the
   option-premium STT has been raised well above this in recent years, and I could not
   confirm today's rate). The same applies to exchange charges. All are editable.
4. **Exercise STT.** Long ITM legs settle at expiry and STT applies on intrinsic value
   (default 0.125%, an assumed rate, editable). On a held-to-expiry box this is often a major
   cost; set it to 0 to ignore.
5. Lot size 30 and last-Tuesday monthly expiry are taken from your spec, not independently verified.
6. Position is held to expiry (no closing orders). European/cash-settled, so no early-assignment risk.
7. Margin, funding cost, order-execution risk across 4 legs and stale quotes are not modelled.
   Prices in a CSV snapshot may no longer be tradable.

---
## v2: Live data, Telegram alerts, ATM band
Files: `app.py` (UI), `core.py` (logic), `sample_option_chain.csv`, `requirements.txt`.

**Live data (Upstox).** Choose "Live (Upstox)". The app calls Upstox's Put/Call Option Chain
endpoint for `NSE_INDEX|Nifty Bank` and uses its spot, bid, ask, OI and volume.
1. Open an Upstox account and create an API app at the Upstox developer console.
2. Generate an access token (they expire daily; re-generate each trading day, or check
   whether your app offers a longer-lived read-only token) and paste it in the sidebar,
   or set the environment variable `UPSTOX_ACCESS_TOKEN`.

**ATM band.** ATM = spot rounded to the strike step (read from the data); only strikes within
+/- the band (default 1000) are scanned. In CSV mode, enter a spot in the sidebar to apply it.

**Telegram alerts.** In Telegram, talk to @BotFather -> `/newbot` -> copy the bot token. Send any
message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
`chat.id`. Paste both in the sidebar (or set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) and use
"Send test message". The same box is not re-alerted within the cooldown. Alerts are paused outside
09:15-15:30 IST on weekdays (exchange holidays are not checked).

**Limits.** Alerts only go out while the app is running with the browser tab open. Live quotes
can be stale or have wide/zero bids; that is why the liquidity and spread filters stay on.
The Upstox integration was written from its public documentation and tested only with mocked
responses here, so run it once during market hours and report any error text.

---
## v3: Angel One (SmartAPI) as an alternative live source
Pick **Angel One** under Live data. Needs: SmartAPI API key (smartapi.angelone.in, create an app),
client code, trading PIN, and TOTP (the secret shown when you enable TOTP, for auto-login, or the
current 6-digit code). Prices come from the Market Data API in FULL mode (best bid/ask = top-of-book
depth, plus OI and volume); the contract list comes from Angel's instrument master.
- The login session is cached while the app runs; it re-logs in if it expires.
- Instrument master download is large; it happens once a day per run.
- Not tested against the live API here (mocked responses only). If a call fails, the error text is shown.
- **Security:** PIN and TOTP secret are far more sensitive than a read-only token. Run this on your own
  laptop and use environment variables (ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET).
  Do not put them into a public Streamlit Cloud app.
