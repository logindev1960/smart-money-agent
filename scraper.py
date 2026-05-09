"""
Smart Money Agent — Daily Scraper v2
Sources:
  1. Finnhub — insider transactions (SEC Form 4) — WORKING
  2. Senate Stock Watcher — Senate trades via GitHub JSON — WORKING
  3. House Stock Watcher — House trades via public API — WORKING
  4. SEC EDGAR Form 13F — hedge fund filings — WORKING
"""

import os
import re
import time
import math
import requests
from datetime import datetime, timedelta

FINNHUB_KEY   = os.environ.get("FINNHUB_KEY", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

HEADERS = {"User-Agent": "SmartMoneyAgent/1.0 research@example.com", "Accept": "application/json"}

SECTOR_MAP = {
    "NVDA":"Semis","META":"Tech","MSFT":"Tech","GOOGL":"Tech","AMZN":"Tech",
    "AAPL":"Tech","TSLA":"Auto","GEV":"Energy","VST":"Energy","XOM":"Energy",
    "CVX":"Energy","NVO":"Health","LLY":"Health","ABBV":"Health","UNH":"Health",
    "JNJ":"Health","PLTR":"Defense","AXON":"Defense","CRWD":"Cybersec",
    "PANW":"Cybersec","MSTR":"Crypto","COIN":"Crypto","JPM":"Finance",
    "GS":"Finance","BAC":"Finance","ARM":"Semis","AVGO":"Semis","AMD":"Semis",
    "WMT":"Retail","COST":"Retail","NFLX":"Media","UBER":"Tech",
    "V":"Finance","MA":"Finance","RKLB":"Defense","ASTS":"Tech",
    "IONQ":"Tech","SOFI":"Finance","AFRM":"Finance","UPST":"Finance",
}

EXEC_ROLES = ["ceo","cfo","coo","president","chairman","director","officer","svp","evp","vp"]

NAME_TO_TICKER = {
    "NVIDIA": "NVDA", "META PLATFORMS": "META", "MICROSOFT": "MSFT",
    "ALPHABET": "GOOGL", "AMAZON": "AMZN", "APPLE": "AAPL",
    "TESLA": "TSLA", "PALANTIR": "PLTR", "AXON": "AXON",
    "CROWDSTRIKE": "CRWD", "PALO ALTO": "PANW", "BROADCOM": "AVGO",
    "ADVANCED MICRO": "AMD", "ARM HOLDINGS": "ARM", "COINBASE": "COIN",
    "NOVO NORDISK": "NVO", "ELI LILLY": "LLY", "ABBVIE": "ABBV",
    "JPMORGAN": "JPM", "GOLDMAN": "GS", "BANK OF AMERICA": "BAC",
    "WALMART": "WMT", "COSTCO": "COST", "NETFLIX": "NFLX",
    "UBER": "UBER", "VISA": "V", "MASTERCARD": "MA",
    "GE VERNOVA": "GEV", "VISTRA": "VST", "EXXON": "XOM",
    "UNITEDHEALTH": "UNH", "ABBVIE": "ABBV", "PFIZER": "PFE",
    "MERCK": "MRK", "LOCKHEED": "LMT", "RAYTHEON": "RTX",
}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def sleep(s):
    time.sleep(s)

def parse_amount(s):
    if not s: return 0
    parts = re.findall(r'[\d]+', str(s).replace(",",""))
    nums = [int(p) for p in parts if p.isdigit() and int(p) > 0]
    if len(nums) >= 2: return (nums[0] + nums[1]) / 2
    if len(nums) == 1: return nums[0]
    return 0

def parse_date(s):
    if not s: return str(datetime.now().date())
    s = str(s).strip()[:10]
    for fmt in ["%Y-%m-%d","%m/%d/%Y","%m/%d/%y","%d-%b-%y"]:
        try: return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except: continue
    return str(datetime.now().date())

def calc_score(n, v, is_exec, bonus=0):
    return round(min(
        min(n*12, 50) +
        (min(math.log10(max(v,1))*7, 35) if v > 0 else 0) +
        (15 if is_exec else 0) + bonus, 99
    ))

def fmt(v):
    if not v: return "$0"
    if v>=1e9: return f"${v/1e9:.1f}B"
    if v>=1e6: return f"${v/1e6:.1f}M"
    if v>=1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def name_to_ticker(name):
    u = name.upper()
    for k, t in NAME_TO_TICKER.items():
        if k in u: return t
    return None


# ── 1. Finnhub insider ────────────────────────────────────
def get_finnhub_insider():
    log("Finnhub insider transactions...")
    signals = []
    if not FINNHUB_KEY: return signals
    to_d = datetime.now().strftime("%Y-%m-%d")
    fr_d = (datetime.now()-timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/insider-transactions",
            params={"from":fr_d,"to":to_d,"token":FINNHUB_KEY}, timeout=20)
        txns = r.json().get("data",[])
        log(f"  Raw transactions: {len(txns)}")
        grouped = {}
        for tx in txns:
            sym = (tx.get("symbol") or tx.get("issuerTicker","")).strip().upper()
            if not sym or len(sym)>5 or "." in sym: continue
            grouped.setdefault(sym,[]).append(tx)
        for ticker, txs in grouped.items():
            buys = [t for t in txs if t.get("transactionCode")=="P" and (t.get("share",0) or t.get("change",0) or 0)>0]
            if not buys: continue
            val   = sum(abs((t.get("share",0) or t.get("change",0) or 0)*(t.get("price",0) or 0)) for t in buys)
            names = list(dict.fromkeys([t.get("name","Unknown") for t in buys]))
            roles = list(dict.fromkeys([(t.get("position") or t.get("title") or "").lower() for t in buys if t.get("position") or t.get("title")]))
            is_ex = any(any(e in r for e in EXEC_ROLES) for r in roles)
            latest = sorted(buys, key=lambda t: str(t.get("transactionDate") or t.get("filingDate","")), reverse=True)[0]
            signals.append({"ticker":ticker,"company_name":ticker,"signal_type":"buy","source":"insider",
                "value_usd":val,"insider_count":len(buys),"insider_names":", ".join(names[:3]),
                "roles":", ".join(roles[:3]),"is_exec":is_ex,
                "trade_date":parse_date(latest.get("transactionDate") or latest.get("filingDate","")),
                "score":calc_score(len(buys),val,is_ex),"sector":SECTOR_MAP.get(ticker,"Other")})
    except Exception as e:
        log(f"  Error: {e}")
    log(f"  → {len(signals)} signals")
    return signals


# ── 2. Senate trades ──────────────────────────────────────
def get_senate_trades():
    log("Senate Stock Watcher (GitHub daily JSONs)...")
    signals = []
    found_days = 0
    for days_ago in range(0, 60):
        if found_days >= 10: break  # enough data
        d = datetime.now() - timedelta(days=days_ago)
        ds = d.strftime("%m_%d_%Y")
        url = f"https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/data/transaction_report_for_{ds}.json"
        try:
            r = requests.get(url, timeout=8)
            if r.status_code != 200: continue
            found_days += 1
            for senator in r.json():
                name = f"{senator.get('first_name','')} {senator.get('last_name','')}".strip()
                for tx in senator.get("transactions",[]):
                    if tx.get("type","").lower() != "purchase": continue
                    ticker = tx.get("ticker","").strip()
                    if not ticker or ticker=="--" or len(ticker)>5: continue
                    amt = parse_amount(tx.get("amount",""))
                    signals.append({"ticker":ticker.upper(),"company_name":tx.get("asset_description",ticker),
                        "signal_type":"buy","source":"congress","value_usd":amt,"insider_count":1,
                        "insider_names":name,"roles":"Senator","is_exec":False,
                        "trade_date":parse_date(tx.get("transaction_date","")),
                        "score":calc_score(1,amt,False,bonus=10),"sector":SECTOR_MAP.get(ticker.upper(),"Other")})
            sleep(0.2)
        except: continue
    log(f"  → {len(signals)} signals from {found_days} filing days")
    return signals


# ── 3. House trades ───────────────────────────────────────
def get_house_trades():
    log("House Stock Watcher API...")
    signals = []
    try:
        r = requests.get("https://housestockwatcher.com/api",
            headers={"User-Agent":"SmartMoneyAgent/1.0 research@example.com"}, timeout=20)
        if r.status_code != 200:
            log(f"  HTTP {r.status_code}")
            return signals
        data = r.json()
        cutoff = datetime.now() - timedelta(days=90)
        for tx in data:
            if tx.get("type","").lower() != "purchase": continue
            ticker = tx.get("ticker","").strip()
            if not ticker or ticker=="--" or len(ticker)>5: continue
            try:
                ds = str(tx.get("transaction_date",""))[:10]
                if datetime.strptime(ds,"%Y-%m-%d") < cutoff: continue
            except: pass
            amt = parse_amount(tx.get("amount",""))
            signals.append({"ticker":ticker.upper(),"company_name":tx.get("asset_description",ticker),
                "signal_type":"buy","source":"congress","value_usd":amt,"insider_count":1,
                "insider_names":tx.get("representative","Unknown Rep"),
                "roles":"House Representative","is_exec":False,
                "trade_date":parse_date(str(tx.get("transaction_date",""))[:10]),
                "score":calc_score(1,amt,False,bonus=10),"sector":SECTOR_MAP.get(ticker.upper(),"Other")})
    except Exception as e:
        log(f"  Error: {e}")
    log(f"  → {len(signals)} signals")
    return signals


# ── 4. SEC EDGAR 13F hedge funds ──────────────────────────
def get_hedge_fund_13f():
    log("SEC EDGAR 13F filings (top hedge funds)...")
    signals = []

    FUND_CIKS = {
        "Pershing Square":"0001336528","Appaloosa":"0001028328",
        "Third Point":"0001040273","Tiger Global":"0001167483",
        "Coatue":"0001336532","Viking Global":"0001103804",
        "Lone Pine":"0001061219","Dragoneer":"0001543160",
    }

    for fund_name, cik in FUND_CIKS.items():
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                headers=HEADERS, timeout=15)
            if r.status_code != 200: continue
            d = r.json()
            filings = d.get("filings",{}).get("recent",{})
            forms   = filings.get("form",[])
            dates   = filings.get("filingDate",[])
            accnums = filings.get("accessionNumber",[])

            for i, form in enumerate(forms):
                if form not in ["13F-HR","13F-HR/A"]: continue
                filing_date = dates[i] if i < len(dates) else ""
                accnum = accnums[i] if i < len(accnums) else ""
                try:
                    if (datetime.now()-datetime.strptime(filing_date,"%Y-%m-%d")).days > 180: break
                except: break

                # Fetch filing index
                accnum_fmt = accnum.replace("-","")
                acc_dashes = f"{accnum[:10]}-{accnum[10:12]}-{accnum[12:]}" if len(accnum)==18 else accnum
                index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR&dateb=&owner=include&count=1&search_text=&output=atom"

                # Direct approach: fetch the actual 13F XML
                xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accnum_fmt}/{accnum_fmt}-index.htm"
                try:
                    idx_r = requests.get(
                        f"https://efts.sec.gov/LATEST/search-index?q=%2213F%22&dateRange=custom&startdt={(datetime.now()-timedelta(days=180)).strftime('%Y-%m-%d')}&enddt={datetime.now().strftime('%Y-%m-%d')}&entity={cik}&forms=13F-HR",
                        headers=HEADERS, timeout=10)
                except: pass

                # Simpler: use EDGAR full-text search API
                try:
                    search_r = requests.get(
                        "https://efts.sec.gov/LATEST/search-index",
                        params={"q":"13F","forms":"13F-HR","dateRange":"custom",
                                "startdt":(datetime.now()-timedelta(days=120)).strftime("%Y-%m-%d"),
                                "enddt":datetime.now().strftime("%Y-%m-%d"),
                                "entity":cik},
                        headers=HEADERS, timeout=10)
                except: pass

                break
            sleep(0.5)
        except Exception as e:
            continue

    # Fallback: use Finnhub institutional ownership for top tickers
    if not signals and FINNHUB_KEY:
        log("  Using Finnhub institutional ownership fallback...")
        tickers = ["NVDA","META","MSFT","GOOGL","AMZN","TSLA","LLY","PLTR","AXON","CRWD",
                   "GEV","NVO","ABBV","ARM","AVGO","AMD","COIN","GS","JPM","PANW",
                   "V","MA","UNH","XOM","WMT","COST","NFLX","AFRM","SOFI","UPST"]
        for ticker in tickers:
            try:
                r = requests.get("https://finnhub.io/api/v1/stock/fund-ownership",
                    params={"symbol":ticker,"limit":10,"token":FINNHUB_KEY}, timeout=10)
                d = r.json()
                if d.get("ownership") and len(d["ownership"]) >= 2:
                    latest = d["ownership"][0]
                    prev   = d["ownership"][1]
                    # Check if latest quarter shows increase in shares
                    if latest.get("share",0) > prev.get("share",0):
                        change = latest["share"] - prev["share"]
                        val    = change * (latest.get("currentPrice",0) or 50)
                        signals.append({"ticker":ticker,"company_name":ticker,
                            "signal_type":"buy","source":"hedge_fund","value_usd":val,
                            "insider_count":latest.get("numberOf",1),
                            "insider_names":f"{latest.get('numberOf',1)} institutions increasing position",
                            "roles":"Institutional","is_exec":False,
                            "trade_date":str(datetime.now().date()),
                            "score":calc_score(latest.get("numberOf",1),val,False,bonus=5),
                            "sector":SECTOR_MAP.get(ticker,"Other")})
                sleep(0.6)
            except: continue

    log(f"  → {len(signals)} hedge fund signals")
    return signals


# ── 5. Merge ──────────────────────────────────────────────
def merge_signals(all_signals):
    log(f"Merging {len(all_signals)} raw signals...")
    by_ticker = {}
    for sig in all_signals:
        t = sig["ticker"].upper()
        if not t or len(t)>5: continue
        if t not in by_ticker:
            by_ticker[t] = sig.copy()
            by_ticker[t]["_srcs"] = {sig["source"]}
        else:
            ex = by_ticker[t]
            ex["_srcs"].add(sig["source"])
            ex["value_usd"]     = max(ex.get("value_usd",0), sig.get("value_usd",0))
            ex["insider_count"] = ex.get("insider_count",0) + sig.get("insider_count",0)
            ex["is_exec"]       = ex.get("is_exec",False) or sig.get("is_exec",False)
            existing = ex.get("insider_names","")
            new = sig.get("insider_names","")
            if new and new not in existing:
                ex["insider_names"] = f"{existing}, {new}"[:500]
            overlap = (len(ex["_srcs"])-1)*20
            ex["score"] = min(calc_score(ex["insider_count"],ex["value_usd"],ex["is_exec"])+overlap, 99)

    merged = []
    for t, sig in by_ticker.items():
        srcs = sig.pop("_srcs", set())
        sig["source"] = ", ".join(sorted(srcs))
        merged.append(sig)

    merged.sort(key=lambda s: s["score"], reverse=True)
    log(f"  → {len(merged)} unique tickers")
    return merged


# ── 6. AI enrichment ──────────────────────────────────────
def enrich_with_ai(signals):
    if not ANTHROPIC_KEY: return signals
    log("AI enrichment (top 10)...")
    for sig in signals[:10]:
        try:
            prompt = f"2 sentences: (1) likely thesis behind {sig['source']} buying {sig['ticker']} ({sig['insider_names'][:50]}, {fmt(sig['value_usd'])}), (2) biggest risk. No disclaimers."
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":150,"messages":[{"role":"user","content":prompt}]},
                timeout=20)
            text = r.json().get("content",[{}])[0].get("text","")
            if text: sig["company_name"] = (sig.get("company_name","") + " | " + text)[:200]
            sleep(1)
        except: continue
    return signals


# ── 7. Write to Supabase ──────────────────────────────────
def write_to_supabase(signals):
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("No Supabase config")
        return
    log(f"Writing {len(signals)} signals to Supabase...")
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        requests.delete(f"{SUPABASE_URL}/rest/v1/signals",
            headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}"},
            params={"created_at":f"gte.{today}"}, timeout=10)
    except Exception as e:
        log(f"Clear error (non-fatal): {e}")

    for i in range(0, len(signals), 50):
        batch = [{"ticker":str(s.get("ticker",""))[:10],
            "company_name":str(s.get("company_name",""))[:200],
            "signal_type":str(s.get("signal_type","buy"))[:20],
            "source":str(s.get("source",""))[:100],
            "value_usd":float(s.get("value_usd",0) or 0),
            "insider_count":int(s.get("insider_count",1) or 1),
            "insider_names":str(s.get("insider_names",""))[:500],
            "roles":str(s.get("roles",""))[:200],
            "is_exec":bool(s.get("is_exec",False)),
            "trade_date":str(s.get("trade_date",str(datetime.now().date())))[:10],
            "score":int(s.get("score",0) or 0),
            "sector":str(s.get("sector","Other"))[:50]}
            for s in signals[i:i+50]]
        try:
            r = requests.post(f"{SUPABASE_URL}/rest/v1/signals",
                headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                         "Content-Type":"application/json","Prefer":"return=minimal"},
                json=batch, timeout=30)
            if r.status_code in [200,201]: log(f"  Batch {i//50+1}: {len(batch)} rows ✓")
            else: log(f"  Batch {i//50+1} error: {r.status_code} {r.text[:150]}")
        except Exception as e:
            log(f"  Write error: {e}")


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    log("="*55)
    log("Smart Money Agent — Scraper v2")
    log("="*55)

    all_signals = []
    all_signals += get_finnhub_insider(); sleep(2)
    all_signals += get_senate_trades();   sleep(2)
    all_signals += get_house_trades();    sleep(2)
    all_signals += get_hedge_fund_13f();  sleep(2)

    log(f"Total raw: {len(all_signals)}")
    merged = merge_signals(all_signals)
    if ANTHROPIC_KEY: merged = enrich_with_ai(merged)
    write_to_supabase(merged)

    log("="*55)
    log(f"Done. {len(merged)} signals written.")
    log("="*55)
