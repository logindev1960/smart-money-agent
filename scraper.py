"""
Smart Money Agent — Daily Scraper v4
Sources:
  1. Finnhub — insider transactions (SEC Form 4) ✅
  2. Senate Stock Watcher — aggregate JSON ✅
  3. SEC EDGAR 13F quarterly flat files — hedge fund holdings ✅ (NEW)

Key fix: uses SEC's pre-parsed quarterly TSV data sets instead of XML parsing.
Latest dataset: 2025 Dec / 2026 Jan-Feb (filed by institutions in Feb 2026)
"""

import os, re, io, time, math, zipfile, requests
from datetime import datetime, timedelta

FINNHUB_KEY   = os.environ.get("FINNHUB_KEY", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

SEC_HEADERS = {
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
    "MRVL":"Semis","QCOM":"Semis","MU":"Semis","INTC":"Semis",
}

EXEC_ROLES = ["ceo","cfo","coo","president","chairman","director","officer","svp","evp","vp"]

# Top hedge funds by CIK — used to identify their filings in the 13F dataset
TOP_FUND_CIKS = {
    "1336528": "Pershing Square",
    "1028328": "Appaloosa",
    "1040273": "Third Point",
    "1167483": "Tiger Global",
    "1336532": "Coatue",
    "1103804": "Viking Global",
    "1061219": "Lone Pine",
    "1543160": "Dragoneer",
    "1537760": "Whale Rock",
    "1326110": "Berkshire Hathaway",
    "1649339": "Citadel Advisors",
    "1638217": "Millennium Mgmt",
    "1412093": "Greenoaks Capital",
    "1418819": "Altimeter Capital",
    "1511184": "TCI Fund Mgmt",
    "1603466": "D1 Capital",
    "1362481": "Renaissance Tech",
    "1081316": "Soros Fund Mgmt",
    "1102644": "Elliott Mgmt",
    "1569345": "Durable Capital",
}

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
def sleep(s): time.sleep(s)

def parse_amount(s):
    if not s: return 0
    nums = [int(x) for x in re.findall(r'\d+', str(s).replace(",","")) if x.isdigit() and int(x)>0]
    if len(nums)>=2: return (nums[0]+nums[1])/2
    if len(nums)==1: return nums[0]
    return 0

def parse_date(s):
    if not s: return str(datetime.now().date())
    s = str(s).strip()
    for fmt in ["%Y-%m-%d","%m/%d/%Y","%m/%d/%y","%d-%b-%y"]:
        try: return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except: pass
    return str(datetime.now().date())

def calc_score(n, v, is_exec, bonus=0):
    return round(min(min(n*12,50)+(min(math.log10(max(v,1))*7,35) if v>0 else 0)+(15 if is_exec else 0)+bonus, 99))

def fmt(v):
    if not v: return "$0"
    if v>=1e9: return f"${v/1e9:.1f}B"
    if v>=1e6: return f"${v/1e6:.1f}M"
    if v>=1e3: return f"${v/1e3:.0f}K"
    return f"${int(v)}"


# ══════════════════════════════════════════════════════════
# 1. FINNHUB INSIDER
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
# 2. SENATE STOCK WATCHER
# ══════════════════════════════════════════════════════════
def get_senate_trades():
    log("2. Senate trades (Senate Stock Watcher aggregate)...")
    signals = []
    cutoff = datetime.now() - timedelta(days=90)
    url = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"
    try:
        r = requests.get(url, timeout=30,
            headers={"User-Agent":"SmartMoneyAgent/1.0 research@example.com"})
        if r.status_code != 200:
            log(f"   HTTP {r.status_code}")
            return signals
        data = r.json()
        log(f"   Got {len(data)} total records")
        for tx in data:
            try:
                if tx.get("type","").lower() not in ["purchase","buy"]: continue
                ticker = tx.get("ticker","").strip()
                if not ticker or ticker=="--" or len(ticker)>5: continue
                date_str = str(tx.get("transaction_date",""))[:10]
                try:
                    if date_str and datetime.strptime(date_str,"%Y-%m-%d") < cutoff: continue
                except: pass
                amt  = parse_amount(tx.get("amount",""))
                name = (tx.get("senator","") or
                        f"{tx.get('first_name','')} {tx.get('last_name','')}").strip()
                signals.append({"ticker":ticker.upper(),"company_name":tx.get("asset_description",ticker),
                    "signal_type":"buy","source":"congress","value_usd":amt,"insider_count":1,
                    "insider_names":name,"roles":"Senator","is_exec":False,
                    "trade_date":parse_date(date_str),"score":calc_score(1,amt,False,bonus=10),
                    "sector":SECTOR_MAP.get(ticker.upper(),"Other")})
            except: continue
    except Exception as e:
        log(f"   Error: {e}")
    log(f"   → {len(signals)} senate signals")
    return signals


# ══════════════════════════════════════════════════════════
# 3. SEC 13F QUARTERLY FLAT FILE DATA SET
# ══════════════════════════════════════════════════════════
def get_sec_13f_dataset():
    """
    Download SEC's pre-parsed 13F quarterly data sets (TSV format).
    Much more reliable than XML parsing.
    Latest: 2025 Dec / 2026 Jan-Feb quarter
    URL pattern: https://www.sec.gov/files/form13f/YYYY-QQ-13F-infotable.tsv
    """
    log("3. SEC EDGAR 13F quarterly dataset...")
    signals = []

    # SEC correct URL format: /files/structureddata/data/form-13f-data-sets/
    # Latest: Dec 2025 - Feb 2026 quarter (filed by institutions in Feb 2026)
    dataset_urls = []  # no direct TSV — only zip

    zip_urls = [
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01dec2025-28feb2026_form13f.zip",
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01sep2025-30nov2025_form13f.zip",
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01jun2025-31aug2025_form13f.zip",
    ]

    infotable_data = None

    # Try TSV direct first
    for url in dataset_urls:
        try:
            log(f"   Trying: {url.split('/')[-1]}")
            r = requests.get(url, headers=SEC_HEADERS, timeout=60, stream=True)
            if r.status_code == 200:
                log(f"   ✓ Got dataset ({len(r.content)//1024}KB)")
                infotable_data = r.text
                break
            else:
                log(f"   HTTP {r.status_code}")
        except Exception as e:
            log(f"   Error: {e}")

    # Try ZIP format
    if not infotable_data:
        for url in zip_urls:
            try:
                log(f"   Trying: {url.split('/')[-1]}")
                r = requests.get(url, headers=SEC_HEADERS, timeout=120)
                if r.status_code == 200:
                    log(f"   ✓ Downloaded ({len(r.content)//1024}KB) — extracting...")
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    log(f"   Files in zip: {z.namelist()}")
                    for name in z.namelist():
                        nl = name.lower()
                        if "infotable" in nl and nl.endswith(".tsv"):
                            infotable_data = z.read(name).decode("utf-8", errors="ignore")
                            log(f"   ✓ Extracted {name} ({len(infotable_data)//1024}KB)")
                            break
                    if not infotable_data:
                        # Try any TSV file
                        for name in z.namelist():
                            if name.lower().endswith(".tsv"):
                                infotable_data = z.read(name).decode("utf-8", errors="ignore")
                                log(f"   ✓ Using {name} ({len(infotable_data)//1024}KB)")
                                break
                    if infotable_data: break
                else:
                    log(f"   HTTP {r.status_code}")
            except Exception as e:
                log(f"   ZIP error: {e}")

    if not infotable_data:
        log("   Could not get 13F dataset — trying EDGAR EFTS search fallback")
        return get_13f_via_efts()

    # Parse the TSV
    # Columns: ACCESSION_NUMBER, INFOTABLE_SK, NAMEOFISSUER, TITLEOFCLASS,
    #          CUSIP, FIGI, VALUE, SSHPRNAMT, SSHPRNAMTTYPE, PUTCALL,
    #          INVESTMENTDISCRETION, OTHERMANAGER, VOTING_AUTH_SOLE,
    #          VOTING_AUTH_SHARED, VOTING_AUTH_NONE
    try:
        lines = infotable_data.strip().split("\n")
        log(f"   Parsing {len(lines)} rows...")

        # Get submission data to map accession numbers to fund names
        # We'll do this separately — for now just use known CIK patterns
        fund_holdings = {}  # accession_number -> list of holdings

        for line in lines[1:]:  # skip header
            try:
                parts = line.split("\t")
                if len(parts) < 7: continue
                # INFOTABLE accession format: 0001172661-26-001091 (with dashes)
                accnum    = parts[0].strip()
                # Normalise — store both dashed and undashed versions
                accnum_nodash = accnum.replace("-","")
                name      = parts[2].strip()
                value_raw = parts[6].strip()
                # Post Jan 2023: values in dollars. Pre Jan 2023: in thousands.
                # The zip filename tells us the period — assume dollars for 2025+
                value = int(value_raw) if value_raw.isdigit() else 0
                # If value looks too small (< 1000), it's probably still in thousands
                if value > 0 and value < 1000:
                    value = value * 1000
                disc      = parts[9].strip() if len(parts)>9 else "SOLE"

                if value < 1_000_000: continue  # skip small positions
                if disc not in ["SOLE","DEFINED"]: continue

                ticker = name_to_ticker_map(name)
                if not ticker: continue

                # Store under both formats
                fund_holdings.setdefault(accnum, []).append({
                    "ticker": ticker, "name": name, "value": value
                })
                fund_holdings.setdefault(accnum_nodash, []).append({
                    "ticker": ticker, "name": name, "value": value
                })
            except: continue

        log(f"   Parsed holdings in {len(fund_holdings)} unique accessions")
        # Debug: show first few accession numbers from INFOTABLE
        sample_accs = list(fund_holdings.keys())[:5]
        log(f"   Sample INFOTABLE accessions: {sample_accs}")
        log(f"   Fund accessions looking for: {list(fund_accessions.keys())[:5]}")

        # Now get submission metadata to identify top fund names
        # Use EDGAR submissions API for our known funds
        fund_accessions = {}  # cik -> accession_number
        for cik, fund_name in TOP_FUND_CIKS.items():
            try:
                r = requests.get(
                    f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                    headers=SEC_HEADERS, timeout=10)
                if r.status_code != 200: continue
                d = r.json()
                filings = d.get("filings",{}).get("recent",{})
                forms   = filings.get("form",[])
                accnums = filings.get("accessionNumber",[])
                dates   = filings.get("filingDate",[])
                for i, form in enumerate(forms):
                    if form in ["13F-HR","13F-HR/A"]:
                        acc = accnums[i].replace("-","") if i<len(accnums) else ""
                        dt  = dates[i] if i<len(dates) else ""
                        try:
                            if (datetime.now()-datetime.strptime(dt,"%Y-%m-%d")).days > 200: break
                        except: break
                        if acc:
                            # Store both formats to match INFOTABLE
                            acc_nodash = acc.replace("-","")
                            acc_dashed = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}" if len(acc)==18 else acc
                            fund_accessions[acc_nodash] = fund_name
                            fund_accessions[acc_dashed]  = fund_name
                            log(f"   {fund_name}: {acc_dashed} ({dt})")
                        break
                sleep(0.3)
            except: continue

        log(f"   Matched {len(fund_accessions)} top fund filings")

        # Match holdings to fund names
        for accnum, holdings in fund_holdings.items():
            fund_name = fund_accessions.get(accnum, None)
            if not fund_name: continue  # only process top funds

            # Get top holdings by value
            top_holdings = sorted(holdings, key=lambda h: h["value"], reverse=True)[:20]
            for h in top_holdings:
                signals.append({
                    "ticker":        h["ticker"],
                    "company_name":  h["name"],
                    "signal_type":   "buy",
                    "source":        "hedge_fund",
                    "value_usd":     float(h["value"]),
                    "insider_count": 1,
                    "insider_names": fund_name,
                    "roles":         "Hedge Fund 13F",
                    "is_exec":       False,
                    "trade_date":    str(datetime.now().date()),
                    "score":         calc_score(1, h["value"], False, bonus=5),
                    "sector":        SECTOR_MAP.get(h["ticker"],"Other"),
                })

    except Exception as e:
        log(f"   Parse error: {e}")

    log(f"   → {len(signals)} hedge fund signals")
    return signals


def get_13f_via_efts():
    """Fallback: use EDGAR full-text search to find and parse 13F filings."""
    log("   EFTS fallback — searching for recent 13F filings...")
    signals = []

    for cik, fund_name in list(TOP_FUND_CIKS.items())[:8]:
        try:
            # Get latest 13F filing details
            r = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                headers=SEC_HEADERS, timeout=10)
            if r.status_code != 200: continue

            d = r.json()
            filings = d.get("filings",{}).get("recent",{})
            forms   = filings.get("form",[])
            dates   = filings.get("filingDate",[])
            accnums = filings.get("accessionNumber",[])
            docs    = filings.get("primaryDocument",[])

            for i, form in enumerate(forms):
                if form not in ["13F-HR","13F-HR/A"]: continue
                dt    = dates[i] if i<len(dates) else ""
                acc   = accnums[i] if i<len(accnums) else ""
                try:
                    if (datetime.now()-datetime.strptime(dt,"%Y-%m-%d")).days > 200: break
                except: break

                # Fetch filing index to find the infotable document
                acc_clean = acc.replace("-","")
                idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{acc_clean}-index.json"
                try:
                    idx_r = requests.get(idx_url, headers=SEC_HEADERS, timeout=10)
                    if idx_r.status_code != 200:
                        # Try without dashes
                        idx_url2 = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR&dateb=&owner=include&count=1&output=atom"
                        continue

                    items = idx_r.json().get("directory",{}).get("item",[])
                    infotable_doc = None
                    for item in items:
                        nm = item.get("name","").lower()
                        if "infotable" in nm or (nm.endswith(".xml") and "primary" not in nm):
                            infotable_doc = item.get("name","")
                            break

                    if not infotable_doc:
                        # The primary doc is sometimes the infotable
                        for item in items:
                            if item.get("name","").endswith(".xml"):
                                infotable_doc = item.get("name","")
                                break

                    if infotable_doc:
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{infotable_doc}"
                        doc_r = requests.get(doc_url, headers=SEC_HEADERS, timeout=20)
                        if doc_r.status_code == 200:
                            entries = re.findall(r'<infoTable>(.*?)</infoTable>', doc_r.text, re.DOTALL|re.IGNORECASE)
                            log(f"   {fund_name}: {len(entries)} holdings ({dt})")
                            for entry in entries[:30]:
                                try:
                                    nm = re.search(r'<nameOfIssuer>(.*?)</nameOfIssuer>', entry, re.I)
                                    vl = re.search(r'<value>(.*?)</value>', entry, re.I)
                                    if not nm or not vl: continue
                                    name  = nm.group(1).strip()
                                    value = int(vl.group(1).strip()) * 1000
                                    if value < 500_000: continue
                                    ticker = name_to_ticker_map(name)
                                    if not ticker: continue
                                    signals.append({"ticker":ticker,"company_name":name,
                                        "signal_type":"buy","source":"hedge_fund","value_usd":float(value),
                                        "insider_count":1,"insider_names":fund_name,
                                        "roles":"Hedge Fund 13F","is_exec":False,
                                        "trade_date":dt,"score":calc_score(1,value,False,bonus=5),
                                        "sector":SECTOR_MAP.get(ticker,"Other")})
                                except: continue
                except Exception as e:
                    log(f"   {fund_name} index error: {e}")
                break
            sleep(0.5)
        except Exception as e:
            log(f"   {fund_name} error: {e}")

    log(f"   → {len(signals)} signals via EFTS fallback")
    return signals


def name_to_ticker_map(name):
    """Map company name to ticker symbol."""
    LOOKUP = {
        "NVIDIA":"NVDA","NVIDIA CORP":"NVDA","META PLATFORMS":"META",
        "MICROSOFT":"MSFT","MICROSOFT CORP":"MSFT",
        "ALPHABET":"GOOGL","AMAZON":"AMZN","AMAZON COM":"AMZN",
        "APPLE":"AAPL","APPLE INC":"AAPL",
        "TESLA":"TSLA","TESLA INC":"TSLA",
        "PALANTIR":"PLTR","AXON ENTERPRISE":"AXON","AXON":"AXON",
        "CROWDSTRIKE":"CRWD","PALO ALTO NETWORKS":"PANW","PALO ALTO":"PANW",
        "BROADCOM":"AVGO","BROADCOM INC":"AVGO",
        "ADVANCED MICRO DEVICES":"AMD","AMD":"AMD",
        "ARM HOLDINGS":"ARM","COINBASE":"COIN","COINBASE GLOBAL":"COIN",
        "NOVO NORDISK":"NVO","ELI LILLY":"LLY","LILLY":"LLY",
        "ABBVIE":"ABBV","UNITEDHEALTH":"UNH","UNITEDHEALTH GROUP":"UNH",
        "JPMORGAN":"JPM","JPMORGAN CHASE":"JPM",
        "GOLDMAN SACHS":"GS","BANK OF AMERICA":"BAC",
        "WALMART":"WMT","COSTCO":"COST","COSTCO WHOLESALE":"COST",
        "NETFLIX":"NFLX","NETFLIX INC":"NFLX",
        "UBER":"UBER","UBER TECHNOLOGIES":"UBER",
        "VISA":"V","VISA INC":"V","MASTERCARD":"MA",
        "GE VERNOVA":"GEV","VISTRA":"VST","VISTRA CORP":"VST",
        "EXXON":"XOM","EXXONMOBIL":"XOM",
        "PFIZER":"PFE","MERCK":"MRK","MERCK CO":"MRK",
        "LOCKHEED MARTIN":"LMT","RAYTHEON":"RTX",
        "MICROSTRATEGY":"MSTR","STRATEGY":"MSTR",
        "SNOWFLAKE":"SNOW","CLOUDFLARE":"NET","DATADOG":"DDOG",
        "SUPER MICRO":"SMCI","SOFI TECHNOLOGIES":"SOFI","SOFI":"SOFI",
        "AFFIRM":"AFRM","UPSTART":"UPST",
        "MARVELL":"MRVL","MARVELL TECHNOLOGY":"MRVL",
        "QUALCOMM":"QCOM","MICRON":"MU","MICRON TECHNOLOGY":"MU",
        "INTEL":"INTC","INTEL CORP":"INTC",
        "BOOKING HOLDINGS":"BKNG","BOOKING":"BKNG",
        "AIRBNB":"ABNB","SPOTIFY":"SPOT",
        "SHOPIFY":"SHOP","SQUARE":"SQ","BLOCK INC":"SQ","BLOCK":"SQ",
        "PAYPAL":"PYPL","PAYPAL HOLDINGS":"PYPL",
        "SALESFORCE":"CRM","SERVICENOW":"NOW",
        "CHIPOTLE":"CMG","STARBUCKS":"SBUX",
        "HOME DEPOT":"HD","NIKE":"NKE","NIKE INC":"NKE",
        "ABBOTT":"ABT","ABBOTT LABORATORIES":"ABT",
        "THERMO FISHER":"TMO","DANAHER":"DHR",
        "UNION PACIFIC":"UNP","NORFOLK SOUTHERN":"NSC",
        "AMERICAN EXPRESS":"AXP","CHARLES SCHWAB":"SCHW",
        "BLACKROCK":"BLK","BLACKROCK INC":"BLK",
        "S&P GLOBAL":"SPGI","MOODY":"MCO",
        "INTUIT":"INTU","ADOBE":"ADBE","ORACLE":"ORCL",
        "APPLIED MATERIALS":"AMAT","LAM RESEARCH":"LRCX",
        "TEXAS INSTRUMENTS":"TXN","ANALOG DEVICES":"ADI",
    }
    n = name.upper().strip()
    # Exact match first
    if n in LOOKUP: return LOOKUP[n]
    # Partial match
    for k, t in LOOKUP.items():
        if k in n and len(k) >= 5: return t
    return None


# ══════════════════════════════════════════════════════════
# 4. MERGE SIGNALS
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
            ex["score"] = min(calc_score(ex["insider_count"],ex["value_usd"],ex["is_exec"])+overlap, 99)

    merged = []
    for t, sig in by_ticker.items():
        srcs = sig.pop("_srcs", set())
        sig["source"] = ", ".join(sorted(srcs))
        merged.append(sig)

    merged.sort(key=lambda s: s["score"], reverse=True)
    log(f"   → {len(merged)} unique tickers")
    return merged


# ══════════════════════════════════════════════════════════
# 5. AI ENRICHMENT
# ══════════════════════════════════════════════════════════
def enrich_with_ai(signals):
    if not ANTHROPIC_KEY: return signals
    log("AI enrichment (top 10)...")
    for sig in signals[:10]:
        try:
            prompt = (f"2 sentences: (1) likely thesis behind {sig['source']} activity on "
                      f"{sig['ticker']} ({sig['insider_names'][:60]}, {fmt(sig['value_usd'])}), "
                      f"(2) biggest risk. No disclaimers.")
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":150,
                      "messages":[{"role":"user","content":prompt}]}, timeout=20)
            text = r.json().get("content",[{}])[0].get("text","")
            if text: sig["company_name"] = (sig.get("company_name","")+" | "+text)[:200]
            sleep(1)
        except: continue
    return signals


# ══════════════════════════════════════════════════════════
# 6. WRITE TO SUPABASE
# ══════════════════════════════════════════════════════════
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
    except: pass

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
            if r.status_code in [200,201]: log(f"   Batch {i//50+1}: {len(batch)} rows ✓")
            else: log(f"   Batch {i//50+1} error: {r.status_code} {r.text[:150]}")
        except Exception as e:
            log(f"   Write error: {e}")


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    log("="*55)
    log("Smart Money Agent — Scraper v4")
    log("="*55)

    all_signals = []
    all_signals += get_finnhub_insider();    sleep(2)
    all_signals += get_senate_trades();      sleep(2)
    all_signals += get_sec_13f_dataset();    sleep(2)

    log(f"\nTotal raw signals: {len(all_signals)}")
    log(f"  Insider:    {sum(1 for s in all_signals if s['source']=='insider')}")
    log(f"  Congress:   {sum(1 for s in all_signals if s['source']=='congress')}")
    log(f"  Hedge fund: {sum(1 for s in all_signals if s['source']=='hedge_fund')}")

    merged = merge_signals(all_signals)
    if ANTHROPIC_KEY: merged = enrich_with_ai(merged)
    write_to_supabase(merged)

    log("="*55)
    log(f"Done. {len(merged)} signals written.")
    log("="*55)
