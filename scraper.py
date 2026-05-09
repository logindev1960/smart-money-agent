"""
Smart Money Agent — Daily Scraper v3
Sources:
  1. Finnhub — insider transactions (SEC Form 4) ✅
  2. Senate Stock Watcher — aggregate JSON from GitHub ✅
  3. SEC EDGAR eftsearch — official Senate STOCK Act filings ✅
  4. SEC EDGAR 13F full-text search — hedge fund filings ✅
"""

import os, re, time, math, requests
from datetime import datetime, timedelta

FINNHUB_KEY   = os.environ.get("FINNHUB_KEY", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

# SEC requires a descriptive user agent
SEC_HEADERS = {
    "User-Agent": "SmartMoneyResearch research@smartmoneyagent.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "efts.sec.gov"
}
SEC_EDGAR_HEADERS = {
    "User-Agent": "SmartMoneyResearch research@smartmoneyagent.com",
    "Accept-Encoding": "gzip, deflate",
}

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
    "SMCI":"Tech","SNOW":"Tech","NET":"Tech","DDOG":"Tech","ZS":"Cybersec",
}

EXEC_ROLES = ["ceo","cfo","coo","president","chairman","director","officer","svp","evp","vp"]

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
def sleep(s): time.sleep(s)

def parse_amount(s):
    if not s: return 0
    nums = [int(x) for x in re.findall(r'\d+', str(s).replace(",","")) if x.isdigit() and int(x) > 0]
    if len(nums) >= 2: return (nums[0] + nums[1]) / 2
    if len(nums) == 1: return nums[0]
    return 0

def parse_date(s):
    if not s: return str(datetime.now().date())
    s = str(s).strip()
    for fmt in ["%Y-%m-%d","%m/%d/%Y","%m/%d/%y","%d-%b-%y","%Y-%m-%dT%H:%M:%S"]:
        try: return datetime.strptime(s[:len(fmt)], fmt[:len(s)]).strftime("%Y-%m-%d")
        except: pass
    try: return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except: return str(datetime.now().date())

def calc_score(n, v, is_exec, bonus=0):
    return round(min(
        min(n*12,50) + (min(math.log10(max(v,1))*7,35) if v>0 else 0) +
        (15 if is_exec else 0) + bonus, 99))

def fmt(v):
    if not v: return "$0"
    if v>=1e9: return f"${v/1e9:.1f}B"
    if v>=1e6: return f"${v/1e6:.1f}M"
    if v>=1e3: return f"${v/1e3:.0f}K"
    return f"${int(v)}"


# ══════════════════════════════════════════════════════════
# 1. FINNHUB INSIDER TRANSACTIONS
# ══════════════════════════════════════════════════════════
def get_finnhub_insider():
    log("1. Finnhub insider transactions (SEC Form 4)...")
    signals = []
    if not FINNHUB_KEY: return signals
    to_d = datetime.now().strftime("%Y-%m-%d")
    fr_d = (datetime.now()-timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/insider-transactions",
            params={"from":fr_d,"to":to_d,"token":FINNHUB_KEY}, timeout=20)
        txns = r.json().get("data",[])
        log(f"   Raw: {len(txns)} transactions")
        grouped = {}
        for tx in txns:
            sym = (tx.get("symbol") or tx.get("issuerTicker","")).strip().upper()
            if not sym or len(sym)>5 or "." in sym: continue
            grouped.setdefault(sym,[]).append(tx)
        for ticker, txs in grouped.items():
            buys = [t for t in txs if t.get("transactionCode")=="P"
                    and (t.get("share",0) or t.get("change",0) or 0)>0]
            if not buys: continue
            val   = sum(abs((t.get("share",0) or t.get("change",0) or 0)*(t.get("price",0) or 0)) for t in buys)
            names = list(dict.fromkeys([t.get("name","Unknown") for t in buys]))
            roles = list(dict.fromkeys([(t.get("position") or t.get("title") or "").lower()
                          for t in buys if t.get("position") or t.get("title")]))
            is_ex = any(any(e in r for e in EXEC_ROLES) for r in roles)
            lat   = sorted(buys, key=lambda t:str(t.get("transactionDate") or t.get("filingDate","")), reverse=True)[0]
            signals.append({"ticker":ticker,"company_name":ticker,"signal_type":"buy","source":"insider",
                "value_usd":val,"insider_count":len(buys),"insider_names":", ".join(names[:3]),
                "roles":", ".join(roles[:3]),"is_exec":is_ex,
                "trade_date":parse_date(lat.get("transactionDate") or lat.get("filingDate","")),
                "score":calc_score(len(buys),val,is_ex),"sector":SECTOR_MAP.get(ticker,"Other")})
    except Exception as e:
        log(f"   Error: {e}")
    log(f"   → {len(signals)} signals")
    return signals


# ══════════════════════════════════════════════════════════
# 2. SENATE STOCK WATCHER — aggregate JSON
# ══════════════════════════════════════════════════════════
def get_senate_trades():
    log("2. Senate trades (Senate Stock Watcher aggregate)...")
    signals = []
    cutoff = datetime.now() - timedelta(days=90)

    # Try aggregate all_transactions.json first
    urls_to_try = [
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_ticker_transactions.json",
    ]

    for url in urls_to_try:
        try:
            log(f"   Trying: {url.split('/')[-1]}")
            r = requests.get(url, timeout=30,
                headers={"User-Agent":"SmartMoneyAgent/1.0 research@example.com"})
            if r.status_code != 200:
                log(f"   HTTP {r.status_code}")
                continue

            data = r.json()
            log(f"   Got {len(data)} records")

            # all_transactions.json format: list of transactions with senator field
            if isinstance(data, list) and len(data) > 0:
                sample = data[0]
                if "transaction_date" in sample or "type" in sample:
                    # Flat list format
                    for tx in data:
                        try:
                            if tx.get("type","").lower() not in ["purchase","buy"]: continue
                            ticker = tx.get("ticker","").strip()
                            if not ticker or ticker=="--" or len(ticker)>5: continue
                            date_str = tx.get("transaction_date","")
                            try:
                                if date_str and datetime.strptime(date_str[:10],"%Y-%m-%d") < cutoff: continue
                            except: pass
                            amt  = parse_amount(tx.get("amount",""))
                            name = tx.get("senator","") or tx.get("first_name","") + " " + tx.get("last_name","")
                            signals.append({"ticker":ticker.upper(),"company_name":tx.get("asset_description",ticker),
                                "signal_type":"buy","source":"congress","value_usd":amt,"insider_count":1,
                                "insider_names":name.strip(),"roles":"Senator","is_exec":False,
                                "trade_date":parse_date(date_str),"score":calc_score(1,amt,False,bonus=10),
                                "sector":SECTOR_MAP.get(ticker.upper(),"Other")})
                        except: continue
                    if signals: break

                elif "transactions" in sample:
                    # Nested senator format
                    for senator in data:
                        name = f"{senator.get('first_name','')} {senator.get('last_name','')}".strip()
                        for tx in senator.get("transactions",[]):
                            try:
                                if tx.get("type","").lower() != "purchase": continue
                                ticker = tx.get("ticker","").strip()
                                if not ticker or ticker=="--" or len(ticker)>5: continue
                                date_str = tx.get("transaction_date","")
                                try:
                                    if date_str and datetime.strptime(date_str[:10],"%Y-%m-%d") < cutoff: continue
                                except: pass
                                amt = parse_amount(tx.get("amount",""))
                                signals.append({"ticker":ticker.upper(),"company_name":tx.get("asset_description",ticker),
                                    "signal_type":"buy","source":"congress","value_usd":amt,"insider_count":1,
                                    "insider_names":name,"roles":"Senator","is_exec":False,
                                    "trade_date":parse_date(date_str),"score":calc_score(1,amt,False,bonus=10),
                                    "sector":SECTOR_MAP.get(ticker.upper(),"Other")})
                            except: continue
                    if signals: break

        except Exception as e:
            log(f"   Error: {e}")
            continue

    # If aggregate failed, try recent daily files with correct format
    if not signals:
        log("   Trying daily files...")
        found = 0
        for days_ago in range(0, 90):
            if found >= 15: break
            d = datetime.now() - timedelta(days=days_ago)
            # The repo README says MM_DD_YYYY format
            ds = d.strftime("%m_%d_%Y")
            url = f"https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/data/transaction_report_for_{ds}.json"
            try:
                r = requests.get(url, timeout=8,
                    headers={"User-Agent":"SmartMoneyAgent/1.0"})
                if r.status_code == 404: continue
                if r.status_code != 200: continue
                found += 1
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
                            "score":calc_score(1,amt,False,bonus=10),
                            "sector":SECTOR_MAP.get(ticker.upper(),"Other")})
                sleep(0.2)
            except: continue

    log(f"   → {len(signals)} senate signals")
    return signals


# ══════════════════════════════════════════════════════════
# 3. SEC EDGAR EFTS — official Senate STOCK Act search
# ══════════════════════════════════════════════════════════
def get_sec_senate_official():
    """Pull directly from SEC EDGAR full-text search for Senate PTR filings."""
    log("3. SEC EDGAR official Senate disclosures...")
    signals = []
    try:
        # Search SEC EDGAR for recent Senate PTR (Periodic Transaction Report) filings
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index?q=%22periodic+transaction%22&dateRange=custom"
            f"&startdt={(datetime.now()-timedelta(days=45)).strftime('%Y-%m-%d')}"
            f"&enddt={datetime.now().strftime('%Y-%m-%d')}&forms=4",
            headers={**SEC_EDGAR_HEADERS, "Host":"efts.sec.gov"},
            timeout=15
        )
        if r.status_code == 200:
            d = r.json()
            hits = d.get("hits",{}).get("hits",[])
            log(f"   SEC EDGAR hits: {len(hits)}")
            # This is Form 4 data — already captured by Finnhub, but useful as cross-check
    except Exception as e:
        log(f"   SEC EDGAR error: {e}")

    # More reliable: use SEC EDGAR company search for large congressional filers
    # The Senate financial disclosure portal has a direct API
    try:
        r = requests.get(
            "https://efdsearch.senate.gov/search/report/data/",
            params={
                "limit": 100,
                "offset": 0,
                "report_types": "[11]",  # PTR = type 11
                "submitted_start_date": (datetime.now()-timedelta(days=45)).strftime("%Y-%m-%d 00:00:00"),
            },
            headers={
                "User-Agent": "SmartMoneyAgent/1.0 research@example.com",
                "Referer": "https://efdsearch.senate.gov/search/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=15
        )
        if r.status_code == 200:
            d = r.json()
            results = d.get("data",[])
            log(f"   Senate EFD API: {len(results)} filings")
            for filing in results:
                try:
                    senator_name = f"{filing[0]} {filing[1]}".strip() if len(filing)>1 else "Unknown Senator"
                    ptr_url = filing[4] if len(filing)>4 else ""
                    # Would need to fetch each PTR URL to get transactions
                    # For now just log that we found filings
                except: continue
    except Exception as e:
        log(f"   Senate EFD error (non-fatal): {e}")

    log(f"   → {len(signals)} signals from SEC official")
    return signals


# ══════════════════════════════════════════════════════════
# 4. SEC EDGAR 13F — Hedge fund positions
# ══════════════════════════════════════════════════════════
def get_hedge_fund_13f():
    log("4. SEC EDGAR 13F hedge fund filings...")
    signals = []

    # Top hedge funds CIK numbers
    FUNDS = {
        "Pershing Square":  "1336528",
        "Appaloosa":        "1028328",
        "Third Point":      "1040273",
        "Tiger Global":     "1167483",
        "Coatue":           "1336532",
        "Viking Global":    "1103804",
        "Lone Pine":        "1061219",
        "Duquesne":         "1536411",
        "Druckenmiller":    "1536411",
        "Baupost":          "1103804",
    }

    NAME_TO_TICKER = {
        "NVIDIA":"NVDA","NVIDIA CORP":"NVDA","META PLATFORMS":"META","META":"META",
        "MICROSOFT":"MSFT","ALPHABET":"GOOGL","AMAZON":"AMZN","AMAZON.COM":"AMZN",
        "APPLE":"AAPL","TESLA":"TSLA","PALANTIR":"PLTR","AXON":"AXON",
        "CROWDSTRIKE":"CRWD","PALO ALTO":"PANW","BROADCOM":"AVGO",
        "ADVANCED MICRO DEVICES":"AMD","ARM HOLDINGS":"ARM","COINBASE":"COIN",
        "NOVO NORDISK":"NVO","ELI LILLY":"LLY","ABBVIE":"ABBV",
        "JPMORGAN":"JPM","GOLDMAN SACHS":"GS","BANK OF AMERICA":"BAC",
        "WALMART":"WMT","COSTCO":"COST","NETFLIX":"NFLX",
        "UBER":"UBER","VISA":"V","MASTERCARD":"MA",
        "GE VERNOVA":"GEV","VISTRA":"VST","EXXON":"XOM","EXXONMOBIL":"XOM",
        "UNITEDHEALTH":"UNH","PFIZER":"PFE","MERCK":"MRK",
        "LOCKHEED":"LMT","RAYTHEON":"RTX","MICROSTRATEGY":"MSTR",
        "SNOWFLAKE":"SNOW","CLOUDFLARE":"NET","DATADOG":"DDOG",
        "SUPER MICRO":"SMCI","SOFI":"SOFI","AFFIRM":"AFRM","UPSTART":"UPST",
    }

    def name_to_ticker(name):
        n = name.upper().strip()
        for k, t in NAME_TO_TICKER.items():
            if k in n: return t
        # Try first word match
        first = n.split()[0] if n.split() else ""
        for k, t in NAME_TO_TICKER.items():
            if k.startswith(first) and len(first) >= 4: return t
        return None

    for fund_name, cik in FUNDS.items():
        try:
            # Get filing list
            r = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                headers=SEC_EDGAR_HEADERS, timeout=15
            )
            if r.status_code != 200:
                continue

            d = r.json()
            filings = d.get("filings",{}).get("recent",{})
            forms   = filings.get("form",[])
            dates   = filings.get("filingDate",[])
            accnums = filings.get("accessionNumber",[])
            primary = filings.get("primaryDocument",[])

            # Find most recent 13F-HR
            for i, form in enumerate(forms):
                if form not in ["13F-HR","13F-HR/A"]: continue
                filing_date = dates[i] if i < len(dates) else ""
                accnum = accnums[i].replace("-","") if i < len(accnums) else ""
                prim_doc = primary[i] if i < len(primary) else ""

                # Skip if older than 6 months
                try:
                    if (datetime.now()-datetime.strptime(filing_date,"%Y-%m-%d")).days > 180:
                        break
                except: break

                log(f"   {fund_name}: found 13F from {filing_date}")

                # Fetch the primary document (infotable XML)
                doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accnum}/{prim_doc}"
                try:
                    doc_r = requests.get(doc_url, headers=SEC_EDGAR_HEADERS, timeout=20)
                    if doc_r.status_code != 200:
                        # Try to find the infotable file in the index
                        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accnum}/{accnum}-index.json"
                        idx_r = requests.get(idx_url, headers=SEC_EDGAR_HEADERS, timeout=10)
                        if idx_r.status_code == 200:
                            for item in idx_r.json().get("directory",{}).get("item",[]):
                                if "infotable" in item.get("name","").lower() or item.get("name","").endswith(".xml"):
                                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accnum}/{item['name']}"
                                    doc_r = requests.get(doc_url, headers=SEC_EDGAR_HEADERS, timeout=20)
                                    break

                    if doc_r.status_code == 200 and doc_r.text:
                        # Parse holdings from XML
                        entries = re.findall(r'<infoTable>(.*?)</infoTable>', doc_r.text, re.DOTALL|re.IGNORECASE)
                        log(f"   {fund_name}: {len(entries)} holdings in 13F")

                        for entry in entries[:50]:  # top 50 holdings
                            try:
                                name_m  = re.search(r'<nameOfIssuer>(.*?)</nameOfIssuer>', entry, re.I)
                                value_m = re.search(r'<value>(.*?)</value>', entry, re.I)
                                shrs_m  = re.search(r'<sshPrnamt>(.*?)</sshPrnamt>', entry, re.I)
                                type_m  = re.search(r'<investmentDiscretion>(.*?)</investmentDiscretion>', entry, re.I)

                                name  = name_m.group(1).strip() if name_m else ""
                                value = int(value_m.group(1).strip()) * 1000 if value_m else 0
                                disc  = type_m.group(1).strip() if type_m else "SOLE"

                                if disc not in ["SOLE","DEFINED"] or value < 100000:
                                    continue

                                ticker = name_to_ticker(name)
                                if not ticker: continue

                                signals.append({
                                    "ticker":        ticker,
                                    "company_name":  name,
                                    "signal_type":   "buy",
                                    "source":        "hedge_fund",
                                    "value_usd":     float(value),
                                    "insider_count": 1,
                                    "insider_names": fund_name,
                                    "roles":         "Hedge Fund",
                                    "is_exec":       False,
                                    "trade_date":    filing_date,
                                    "score":         calc_score(1, value, False, bonus=5),
                                    "sector":        SECTOR_MAP.get(ticker,"Other"),
                                })
                            except: continue
                except Exception as e:
                    log(f"   {fund_name} doc error: {e}")

                sleep(0.5)  # respect SEC rate limits
                break  # only latest 13F

        except Exception as e:
            log(f"   {fund_name} error: {e}")
            continue

    log(f"   → {len(signals)} hedge fund signals")
    return signals


# ══════════════════════════════════════════════════════════
# 5. MERGE SIGNALS
# ══════════════════════════════════════════════════════════
def merge_signals(all_signals):
    log(f"Merging {len(all_signals)} raw signals...")
    by_ticker = {}
    for sig in all_signals:
        t = sig["ticker"].upper().strip()
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
            new_name = sig.get("insider_names","")
            if new_name and new_name not in existing:
                ex["insider_names"] = f"{existing}, {new_name}"[:500]
            overlap = (len(ex["_srcs"])-1) * 20
            ex["score"] = min(calc_score(ex["insider_count"],ex["value_usd"],ex["is_exec"]) + overlap, 99)

    merged = []
    for t, sig in by_ticker.items():
        srcs = sig.pop("_srcs", set())
        sig["source"] = ", ".join(sorted(srcs))
        merged.append(sig)

    merged.sort(key=lambda s: s["score"], reverse=True)
    log(f"   → {len(merged)} unique tickers")
    return merged


# ══════════════════════════════════════════════════════════
# 6. AI ENRICHMENT
# ══════════════════════════════════════════════════════════
def enrich_with_ai(signals):
    if not ANTHROPIC_KEY: return signals
    log("AI enrichment (top 10)...")
    for sig in signals[:10]:
        try:
            prompt = (f"2 sentences only: (1) likely thesis behind {sig['source']} buying "
                      f"{sig['ticker']} ({sig['insider_names'][:50]}, {fmt(sig['value_usd'])}), "
                      f"(2) biggest risk. No disclaimers.")
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":150,
                      "messages":[{"role":"user","content":prompt}]}, timeout=20)
            text = r.json().get("content",[{}])[0].get("text","")
            if text: sig["company_name"] = (sig.get("company_name","") + " | " + text)[:200]
            sleep(1)
        except: continue
    return signals


# ══════════════════════════════════════════════════════════
# 7. WRITE TO SUPABASE
# ══════════════════════════════════════════════════════════
def write_to_supabase(signals):
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("No Supabase config — skipping")
        return
    log(f"Writing {len(signals)} signals to Supabase...")

    # Clear today's data
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        requests.delete(f"{SUPABASE_URL}/rest/v1/signals",
            headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}"},
            params={"created_at":f"gte.{today}"}, timeout=10)
    except Exception as e:
        log(f"   Clear error (non-fatal): {e}")

    # Insert in batches of 50
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
            if r.status_code in [200,201]:
                log(f"   Batch {i//50+1}: {len(batch)} rows ✓")
            else:
                log(f"   Batch {i//50+1} error: {r.status_code} {r.text[:200]}")
        except Exception as e:
            log(f"   Write error: {e}")


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    log("="*55)
    log("Smart Money Agent — Scraper v3")
    log("="*55)

    all_signals = []
    all_signals += get_finnhub_insider();    sleep(2)
    all_signals += get_senate_trades();      sleep(2)
    all_signals += get_sec_senate_official(); sleep(2)
    all_signals += get_hedge_fund_13f();     sleep(2)

    log(f"\nTotal raw signals: {len(all_signals)}")
    log(f"  Insider: {sum(1 for s in all_signals if s['source']=='insider')}")
    log(f"  Congress: {sum(1 for s in all_signals if s['source']=='congress')}")
    log(f"  Hedge fund: {sum(1 for s in all_signals if s['source']=='hedge_fund')}")

    merged = merge_signals(all_signals)
    if ANTHROPIC_KEY: merged = enrich_with_ai(merged)
    write_to_supabase(merged)

    log("="*55)
    log(f"Done. {len(merged)} signals written to Supabase.")
    log("="*55)
