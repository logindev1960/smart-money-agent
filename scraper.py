"""
Smart Money Agent — Daily Scraper
Pulls from: Open Insider, Capitol Trades (Senate), WhaleWisdom, Finnhub
Writes to: Supabase

Run daily via GitHub Actions.
"""

import os
import re
import json
import time
import math
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# ── Config from environment variables ─────────────────────
FINNHUB_KEY   = os.environ.get("FINNHUB_KEY", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SECTOR_MAP = {
    "NVDA":"Semis","META":"Tech","MSFT":"Tech","GOOGL":"Tech","AMZN":"Tech",
    "AAPL":"Tech","TSLA":"Auto","GEV":"Energy","VST":"Energy","XOM":"Energy",
    "CVX":"Energy","NVO":"Health","LLY":"Health","ABBV":"Health","UNH":"Health",
    "JNJ":"Health","PLTR":"Defense","AXON":"Defense","CRWD":"Cybersec",
    "PANW":"Cybersec","MSTR":"Crypto","COIN":"Crypto","JPM":"Finance",
    "GS":"Finance","BAC":"Finance","ARM":"Semis","AVGO":"Semis","AMD":"Semis",
    "WMT":"Retail","COST":"Retail","NFLX":"Media","UBER":"Tech","V":"Finance","MA":"Finance"
}

EXEC_ROLES = ["ceo","cfo","coo","president","chairman","director","officer","svp","evp","vp"]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def sleep(s):
    time.sleep(s)

# ── 1. Open Insider — cluster buys ────────────────────────
def scrape_open_insider():
    """Scrape cluster buys from Open Insider screener."""
    log("Scraping Open Insider cluster buys...")
    signals = []
    try:
        url = "https://openinsider.com/screener?s=&o=&pl=&ph=&ls=&lsh=&cli=&clh=&niprc=&niprh=&fd=1&fdr=&td=1&tdr=&fdlyl=&fdlyh=&daysago=30&xs=1&vl=100&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=100&page=1"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "tinytable"})
        if not table:
            log("Open Insider: table not found")
            return signals

        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 12:
                continue
            try:
                filing_date = cols[1].get_text(strip=True)
                trade_date  = cols[2].get_text(strip=True)
                ticker      = cols[3].get_text(strip=True)
                company     = cols[4].get_text(strip=True)
                insider     = cols[5].get_text(strip=True)
                title       = cols[6].get_text(strip=True)
                trade_type  = cols[7].get_text(strip=True)
                price_str   = cols[8].get_text(strip=True).replace("$","").replace(",","")
                qty_str     = cols[9].get_text(strip=True).replace(",","")
                value_str   = cols[11].get_text(strip=True).replace("$","").replace(",","").replace("+","")

                if "P" not in trade_type:  # only purchases
                    continue

                price = float(price_str) if price_str else 0
                qty   = int(qty_str) if qty_str.isdigit() else 0
                value = float(value_str) if value_str else price * qty

                is_exec = any(e in title.lower() for e in EXEC_ROLES)
                score   = calc_score(1, value, is_exec)

                signals.append({
                    "ticker":        ticker.upper(),
                    "company_name":  company,
                    "signal_type":   "buy",
                    "source":        "open_insider",
                    "value_usd":     value,
                    "insider_count": 1,
                    "insider_names": insider,
                    "roles":         title,
                    "is_exec":       is_exec,
                    "trade_date":    parse_date(trade_date),
                    "score":         score,
                    "sector":        SECTOR_MAP.get(ticker.upper(), "Other"),
                })
            except Exception as e:
                continue

        log(f"Open Insider: {len(signals)} signals scraped")
        sleep(2)
    except Exception as e:
        log(f"Open Insider error: {e}")
    return signals


# ── 2. Capitol Trades — Congress buys ─────────────────────
def scrape_capitol_trades():
    """Scrape recent Congress stock purchases from Capitol Trades."""
    log("Scraping Capitol Trades (Congress buys)...")
    signals = []
    try:
        # Try the trades page with buy filter
        url = "https://capitoltrades.com/trades?txType=buy&pageSize=96"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Capitol Trades renders server-side — look for trade rows
        rows = soup.find_all("tr", {"class": re.compile("q-tr")})
        if not rows:
            # Try alternative selector
            rows = soup.find_all("tr")[1:]

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 6:
                continue
            try:
                text = [c.get_text(strip=True) for c in cols]
                # Capitol Trades columns: politician, party, ticker, company, type, date, amount
                ticker  = ""
                company = ""
                amount  = 0
                date    = ""
                politician = text[0] if len(text) > 0 else ""

                # Find ticker — usually a short all-caps string
                for t in text:
                    if re.match(r'^[A-Z]{1,5}$', t) and len(t) >= 1:
                        ticker = t
                        break

                # Find amount range — usually "$X,XXX - $XX,XXX" format
                for t in text:
                    if "$" in t and ("-" in t or "–" in t):
                        # Take midpoint of range
                        parts = re.findall(r'[\d,]+', t)
                        if len(parts) >= 2:
                            lo = int(parts[0].replace(",",""))
                            hi = int(parts[1].replace(",",""))
                            amount = (lo + hi) / 2

                # Find date
                for t in text:
                    if re.match(r'\d{4}-\d{2}-\d{2}', t) or re.match(r'\d{2}/\d{2}/\d{4}', t):
                        date = t
                        break

                if not ticker or not amount:
                    continue

                signals.append({
                    "ticker":        ticker.upper(),
                    "company_name":  company or ticker,
                    "signal_type":   "buy",
                    "source":        "congress",
                    "value_usd":     amount,
                    "insider_count": 1,
                    "insider_names": politician,
                    "roles":         "Congress Member",
                    "is_exec":       False,
                    "trade_date":    parse_date(date) if date else str(datetime.now().date()),
                    "score":         calc_score(1, amount, False) + 10,  # congress boost
                    "sector":        SECTOR_MAP.get(ticker.upper(), "Other"),
                })
            except Exception:
                continue

        log(f"Capitol Trades: {len(signals)} signals scraped")
        sleep(2)
    except Exception as e:
        log(f"Capitol Trades error: {e}")

    # Fallback: try Senate Stock Watcher GitHub data
    if len(signals) == 0:
        signals = scrape_senate_stock_watcher()

    return signals


def scrape_senate_stock_watcher():
    """Fallback: pull Senate trades from the GitHub-hosted JSON."""
    log("Trying Senate Stock Watcher fallback...")
    signals = []
    try:
        url = "https://raw.githubusercontent.com/r/senate-stock-watcher-data/main/aggregate/all_transactions.json"
        r = requests.get(url, timeout=15)
        data = r.json()
        cutoff = datetime.now() - timedelta(days=90)

        for tx in data[:500]:  # latest 500
            try:
                date_str = tx.get("transaction_date","")
                tx_date  = datetime.strptime(date_str, "%Y-%m-%d") if date_str else None
                if tx_date and tx_date < cutoff:
                    continue
                if tx.get("type","").lower() != "purchase":
                    continue

                ticker = tx.get("ticker","").strip()
                if not ticker or ticker == "N/A":
                    continue

                # Amount is a range like "$1,001 - $15,000"
                amount_str = tx.get("amount","")
                parts = re.findall(r'[\d,]+', amount_str)
                amount = 0
                if len(parts) >= 2:
                    amount = (int(parts[0].replace(",","")) + int(parts[1].replace(",",""))) / 2

                signals.append({
                    "ticker":        ticker.upper(),
                    "company_name":  tx.get("asset_description", ticker),
                    "signal_type":   "buy",
                    "source":        "congress",
                    "value_usd":     amount,
                    "insider_count": 1,
                    "insider_names": tx.get("senator","Unknown Senator"),
                    "roles":         "Senator",
                    "is_exec":       False,
                    "trade_date":    date_str or str(datetime.now().date()),
                    "score":         calc_score(1, amount, False) + 10,
                    "sector":        SECTOR_MAP.get(ticker.upper(), "Other"),
                })
            except Exception:
                continue

        log(f"Senate Stock Watcher: {len(signals)} signals")
    except Exception as e:
        log(f"Senate Stock Watcher error: {e}")
    return signals


# ── 3. WhaleWisdom — hedge fund 13F ───────────────────────
def scrape_whalewisdom():
    """Scrape top hedge fund new positions from WhaleWisdom."""
    log("Scraping WhaleWisdom (hedge fund 13F)...")
    signals = []
    try:
        # WhaleWisdom top buys — new positions page
        url = "https://whalewisdom.com/stock/top-buys"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Look for stock tables
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")[1:]
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue
                try:
                    text = [c.get_text(strip=True) for c in cols]
                    ticker = ""
                    for t in text:
                        if re.match(r'^[A-Z]{1,5}$', t):
                            ticker = t
                            break
                    if not ticker:
                        continue

                    # Try to find fund count and value
                    fund_count = 1
                    value = 0
                    for t in text:
                        if re.match(r'^\d+$', t):
                            fund_count = int(t)
                        if "$" in t:
                            parts = re.findall(r'[\d.]+', t.replace(",",""))
                            if parts:
                                v = float(parts[0])
                                if "B" in t: v *= 1e9
                                elif "M" in t: v *= 1e6
                                elif "K" in t: v *= 1e3
                                value = v

                    signals.append({
                        "ticker":        ticker.upper(),
                        "company_name":  text[0] if text else ticker,
                        "signal_type":   "buy",
                        "source":        "hedge_fund",
                        "value_usd":     value,
                        "insider_count": fund_count,
                        "insider_names": f"{fund_count} hedge funds",
                        "roles":         "Hedge Fund",
                        "is_exec":       False,
                        "trade_date":    str(datetime.now().date()),
                        "score":         calc_score(fund_count, value, False) + 5,
                        "sector":        SECTOR_MAP.get(ticker.upper(), "Other"),
                    })
                except Exception:
                    continue

        log(f"WhaleWisdom: {len(signals)} signals scraped")
        sleep(2)
    except Exception as e:
        log(f"WhaleWisdom error: {e}")

    # Fallback: use Finnhub institutional ownership for key tickers
    if len(signals) == 0:
        signals = get_finnhub_institutional()

    return signals


def get_finnhub_institutional():
    """Fallback: get institutional ownership changes via Finnhub."""
    log("Trying Finnhub institutional ownership fallback...")
    signals = []
    if not FINNHUB_KEY:
        return signals

    tickers = ["NVDA","META","MSFT","GOOGL","AMZN","TSLA","LLY","PLTR","AXON","CRWD",
               "GEV","NVO","ABBV","ARM","AVGO","AMD","COIN","GS","JPM","PANW"]

    for ticker in tickers:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/stock/institutional-ownership",
                params={"symbol":ticker,"token":FINNHUB_KEY},
                timeout=10
            )
            d = r.json()
            if d.get("ownership") and len(d["ownership"]) >= 2:
                latest   = d["ownership"][0]
                previous = d["ownership"][1]
                change   = latest.get("share",0) - previous.get("share",0)
                if change > 0:
                    value = change * latest.get("currentPrice",0)
                    signals.append({
                        "ticker":        ticker,
                        "company_name":  ticker,
                        "signal_type":   "buy",
                        "source":        "hedge_fund",
                        "value_usd":     value,
                        "insider_count": latest.get("numberOf",1),
                        "insider_names": f"{latest.get('numberOf',1)} institutions",
                        "roles":         "Institutional",
                        "is_exec":       False,
                        "trade_date":    str(datetime.now().date()),
                        "score":         calc_score(latest.get("numberOf",1), value, False),
                        "sector":        SECTOR_MAP.get(ticker,"Other"),
                    })
            sleep(0.8)
        except Exception:
            continue

    log(f"Finnhub institutional: {len(signals)} signals")
    return signals


# ── 4. Finnhub insider transactions ───────────────────────
def scrape_finnhub_insider():
    """Pull insider transactions from Finnhub SEC feed."""
    log("Pulling Finnhub insider transactions...")
    signals = []
    if not FINNHUB_KEY:
        log("No Finnhub key — skipping")
        return signals

    to_date   = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        # Try blank symbol — full market feed
        r = requests.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"from":from_date,"to":to_date,"token":FINNHUB_KEY},
            timeout=20
        )
        d = r.json()
        txns = d.get("data",[])

        if txns:
            log(f"Finnhub blank symbol: {len(txns)} transactions")
            grouped = {}
            for tx in txns:
                sym = (tx.get("symbol") or tx.get("issuerTicker","")).strip().upper()
                if not sym or len(sym) > 5:
                    continue
                if sym not in grouped:
                    grouped[sym] = []
                grouped[sym].append(tx)

            for ticker, txs in grouped.items():
                buys = [t for t in txs if t.get("transactionCode")=="P" and (t.get("share",0) or t.get("change",0)) > 0]
                if not buys:
                    continue
                total_val   = sum(abs((t.get("share",0) or t.get("change",0)) * (t.get("price",0) or 0)) for t in buys)
                names       = list(set(t.get("name","Unknown") for t in buys))
                roles       = list(set((t.get("position") or t.get("title","")).lower() for t in buys if t.get("position") or t.get("title")))
                is_exec     = any(any(e in r for e in EXEC_ROLES) for r in roles)
                latest      = sorted(buys, key=lambda t: t.get("transactionDate") or t.get("filingDate",""), reverse=True)[0]
                trade_date  = latest.get("transactionDate") or latest.get("filingDate","")

                tags = ["insider"]
                if len(buys) >= 2: tags.append("cluster")
                if total_val >= 1e6: tags.append("mega")

                signals.append({
                    "ticker":        ticker,
                    "company_name":  ticker,
                    "signal_type":   "buy",
                    "source":        "insider",
                    "value_usd":     total_val,
                    "insider_count": len(buys),
                    "insider_names": ", ".join(names[:3]),
                    "roles":         ", ".join(roles[:3]),
                    "is_exec":       is_exec,
                    "trade_date":    trade_date,
                    "score":         calc_score(len(buys), total_val, is_exec),
                    "sector":        SECTOR_MAP.get(ticker,"Other"),
                })
    except Exception as e:
        log(f"Finnhub insider error: {e}")

    log(f"Finnhub insider: {len(signals)} signals")
    return signals


# ── 5. AI enrichment via Claude ───────────────────────────
def enrich_with_ai(signals):
    """Add AI-generated thesis to top signals using Claude."""
    if not ANTHROPIC_KEY:
        log("No Anthropic key — skipping AI enrichment")
        return signals

    log(f"Enriching top {min(10,len(signals))} signals with Claude AI...")
    top = sorted(signals, key=lambda s: s["score"], reverse=True)[:10]

    for sig in top:
        try:
            prompt = f"""Hedge fund analyst. 2 sentences max on {sig['ticker']}: 
(1) likely thesis behind {sig['source']} buying ({sig['insider_names'][:50]}, ${sig['value_usd']:,.0f}), 
(2) biggest risk. No disclaimers."""

            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 150,
                    "messages": [{"role":"user","content":prompt}]
                },
                timeout=20
            )
            d = r.json()
            sig["ai_thesis"] = d.get("content",[{}])[0].get("text","")
            sleep(1)
        except Exception as e:
            sig["ai_thesis"] = ""
            continue

    return signals


# ── 6. Deduplicate & merge signals ────────────────────────
def merge_signals(all_signals):
    """Merge signals from multiple sources by ticker, boost score for overlap."""
    log("Merging and deduplicating signals...")
    by_ticker = {}

    for sig in all_signals:
        ticker = sig["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = sig.copy()
            by_ticker[ticker]["sources"] = [sig["source"]]
        else:
            existing = by_ticker[ticker]
            existing["sources"].append(sig["source"])
            existing["value_usd"] = max(existing["value_usd"], sig["value_usd"])
            existing["insider_count"] += sig["insider_count"]
            existing["is_exec"] = existing["is_exec"] or sig["is_exec"]
            # Recalculate score with source overlap bonus
            overlap_bonus = (len(set(existing["sources"])) - 1) * 20
            existing["score"] = min(calc_score(
                existing["insider_count"],
                existing["value_usd"],
                existing["is_exec"]
            ) + overlap_bonus, 99)

    merged = list(by_ticker.values())
    for sig in merged:
        sig["source"] = ", ".join(set(sig.get("sources",[])))

    merged.sort(key=lambda s: s["score"], reverse=True)
    log(f"Merged to {len(merged)} unique tickers")
    return merged


# ── 7. Write to Supabase ──────────────────────────────────
def write_to_supabase(signals):
    """Upsert signals into Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("No Supabase config — skipping write")
        return

    log(f"Writing {len(signals)} signals to Supabase...")

    # Clear today's data first
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/signals",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            },
            params={"created_at": f"gte.{today}"}
        )
    except Exception as e:
        log(f"Clear error (non-fatal): {e}")

    # Insert in batches of 50
    batch_size = 50
    for i in range(0, len(signals), batch_size):
        batch = signals[i:i+batch_size]
        # Clean for JSON serialisation
        clean = []
        for s in batch:
            clean.append({
                "ticker":        str(s.get("ticker",""))[:10],
                "company_name":  str(s.get("company_name",""))[:200],
                "signal_type":   str(s.get("signal_type","buy"))[:20],
                "source":        str(s.get("source",""))[:100],
                "value_usd":     float(s.get("value_usd",0) or 0),
                "insider_count": int(s.get("insider_count",1) or 1),
                "insider_names": str(s.get("insider_names",""))[:500],
                "roles":         str(s.get("roles",""))[:200],
                "is_exec":       bool(s.get("is_exec",False)),
                "trade_date":    str(s.get("trade_date", datetime.now().strftime("%Y-%m-%d")))[:10],
                "score":         int(s.get("score",0) or 0),
                "sector":        str(s.get("sector","Other"))[:50],
            })

        try:
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/signals",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal"
                },
                json=clean,
                timeout=30
            )
            if r.status_code in [200,201]:
                log(f"Batch {i//batch_size+1}: {len(clean)} rows inserted ✓")
            else:
                log(f"Batch {i//batch_size+1} error: {r.status_code} {r.text[:200]}")
        except Exception as e:
            log(f"Supabase write error: {e}")

    log("Supabase write complete ✓")


# ── Utils ─────────────────────────────────────────────────
def calc_score(n, v, is_exec):
    count_score = min(n * 12, 50)
    val_score   = min(math.log10(max(v, 1)) * 7, 35) if v > 0 else 0
    exec_bonus  = 15 if is_exec else 0
    return round(min(count_score + val_score + exec_bonus, 99))

def parse_date(date_str):
    """Try to parse various date formats."""
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%y", "%b %d, %Y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return datetime.now().strftime("%Y-%m-%d")


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 50)
    log("Smart Money Agent — Daily Scraper Starting")
    log("=" * 50)

    all_signals = []

    # 1. Finnhub insider (most reliable)
    all_signals += scrape_finnhub_insider()
    sleep(2)

    # 2. Open Insider cluster buys
    all_signals += scrape_open_insider()
    sleep(2)

    # 3. Congress trades
    all_signals += scrape_capitol_trades()
    sleep(2)

    # 4. Hedge fund moves
    all_signals += scrape_whalewisdom()
    sleep(2)

    log(f"Total raw signals collected: {len(all_signals)}")

    # 5. Merge by ticker (boosts score for multi-source overlap)
    merged = merge_signals(all_signals)

    # 6. AI enrichment (optional)
    if ANTHROPIC_KEY:
        merged = enrich_with_ai(merged)

    # 7. Write to Supabase
    write_to_supabase(merged)

    log("=" * 50)
    log(f"Done. {len(merged)} signals written to Supabase.")
    log("=" * 50)
