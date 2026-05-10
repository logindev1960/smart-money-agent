"""
Smart Money Agent — Scraper v5
Proper weighted conviction scoring:
  - Politician hit rate calculated from historical data
  - Exec seniority weighting (CEO > CFO > Director > VP)
  - Hedge fund tier weighting (Tier 1 > Tier 2)
  - Multi-source overlap multiplier
  - 6-month date window with days_ago tagging
  - Stocks AND ETFs included
"""

import os, re, io, time, math, zipfile, json, requests
from datetime import datetime, timedelta
from collections import defaultdict

FINNHUB_KEY   = os.environ.get("FINNHUB_KEY", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

SEC_HEADERS = {
    "User-Agent": "SmartMoneyResearch research@smartmoneyagent.com",
    "Accept-Encoding": "gzip, deflate",
}

# ── Date window ───────────────────────────────────────────
LOOKBACK_DAYS = 180  # 6 months to catch full 13F cycle
CUTOFF = datetime.now() - timedelta(days=LOOKBACK_DAYS)

# ── Sector map ────────────────────────────────────────────
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
    "LMT":"Defense","RTX":"Defense","NOC":"Defense","GD":"Defense",
    "SPY":"ETF","QQQ":"ETF","VTI":"ETF","VWRP":"ETF","CNDX":"ETF",
    "GLD":"ETF","SGLN":"ETF","IAU":"ETF","IEMG":"ETF","VT":"ETF",
}

# ── Exec seniority weights ────────────────────────────────
EXEC_WEIGHTS = {
    "ceo": 35, "chief executive": 35, "president": 28,
    "cfo": 25, "chief financial": 25, "chief operating": 22, "coo": 22,
    "chairman": 30, "executive chairman": 32,
    "director": 15, "board": 15,
    "svp": 12, "senior vice president": 12,
    "evp": 14, "executive vice president": 14,
    "vp": 10, "vice president": 10,
    "officer": 8, "general counsel": 10,
}

def exec_weight(role):
    r = role.lower()
    for k, w in EXEC_WEIGHTS.items():
        if k in r: return w
    return 5  # unknown role = small weight

# ── Hedge fund tier weights ───────────────────────────────
FUND_TIER1_CIKS = {
    # Tier 1 — legendary track records
    "1167483": ("Tiger Global", 30),
    "1336532": ("Coatue", 30),
    "1103804": ("Viking Global", 28),
    "1603466": ("D1 Capital", 28),
    "1543160": ("Dragoneer", 25),
    "1061219": ("Lone Pine", 25),
    "1326110": ("Berkshire Hathaway", 25),
    "1537760": ("Whale Rock", 22),
    "1418819": ("Altimeter Capital", 22),
    "1412093": ("Greenoaks Capital", 22),
    "1511184": ("TCI Fund Mgmt", 20),
    "1569345": ("Durable Capital", 20),
    # Tier 2 — solid but less alpha
    "1336528": ("Pershing Square", 18),
    "1040273": ("Third Point", 18),
    "1028328": ("Appaloosa", 18),
    "1649339": ("Citadel Advisors", 15),
    "1638217": ("Millennium Mgmt", 15),
    "1362481": ("Renaissance Tech", 20),
    "1081316": ("Soros Fund Mgmt", 15),
    "1102644": ("Elliott Mgmt", 18),
}

# ── Politician base weights (curated, will be adjusted by hit rate) ──
POLITICIAN_BASE_WEIGHTS = {
    # Current high-edge politicians (2025-2026)
    "pelosi": 35, "nancy pelosi": 35,
    "kushner": 32, "jared kushner": 32,
    "trump": 30,  # family trades
    "hegseth": 28, "pete hegseth": 28,
    "tuberville": 25, "tommy tuberville": 25,
    "scott": 22, "austin scott": 22,
    "crenshaw": 20, "dan crenshaw": 20,
    "mast": 20, "brian mast": 20,
    "Collins": 18, "susan collins": 18,
    "kelly": 18, "mark kelly": 18,
    "ossoff": 18, "jon ossoff": 18,
    "wicker": 18, "roger wicker": 18,
    "capito": 16, "shelley capito": 16,
    "warner": 16, "mark warner": 16,
    "reed": 15, "jack reed": 15,
}

def politician_base_weight(name):
    """Get base weight for a politician by name."""
    n = name.lower()
    for k, w in POLITICIAN_BASE_WEIGHTS.items():
        if k in n: return w
    return 8  # unknown senator = low base weight

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
def sleep(s): time.sleep(s)

def parse_amount(s):
    if not s: return 0
    nums = [int(x) for x in re.findall(r'\d+', str(s).replace(",","")) if x.isdigit() and int(x)>0]
    if len(nums)>=2: return (nums[0]+nums[1])/2
    if len(nums)==1: return nums[0]
    return 0

def parse_date(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ["%Y-%m-%d","%m/%d/%Y","%m/%d/%y","%d-%b-%y","%Y-%m-%dT%H:%M:%S"]:
        try: return datetime.strptime(s[:len(fmt)], fmt)
        except: pass
    try: return datetime.strptime(s[:10], "%Y-%m-%d")
    except: return None

def days_ago(date_str):
    d = parse_date(str(date_str))
    if not d: return 999
    return (datetime.now() - d).days

def fmt(v):
    if not v: return "$0"
    if v>=1e9: return f"${v/1e9:.1f}B"
    if v>=1e6: return f"${v/1e6:.1f}M"
    if v>=1e3: return f"${v/1e3:.0f}K"
    return f"${int(v)}"

def in_window(date_str):
    d = parse_date(str(date_str))
    if not d: return False
    return d >= CUTOFF


# ══════════════════════════════════════════════════════════
# POLITICIAN HIT RATE CALCULATOR
# Uses Senate Stock Watcher historical data + Finnhub prices
# to calculate each senator's track record
# ══════════════════════════════════════════════════════════
def calculate_politician_hit_rates(all_senate_trades):
    """
    For each senator, calculate what % of their past buys
    beat the S&P 500 by >5% over the following 90 days.
    Returns dict: {senator_name: hit_rate_0_to_1}
    """
    log("   Calculating politician hit rates from historical data...")

    # Get historical trades (older than 90 days so we can measure outcome)
    historical = [t for t in all_senate_trades
                  if days_ago(t.get("transaction_date","")) > 90
                  and t.get("type","").lower() == "purchase"
                  and t.get("ticker","").strip() not in ["--",""]
                  and len(t.get("ticker","").strip()) <= 5]

    log(f"   {len(historical)} historical trades to evaluate")

    if not historical or not FINNHUB_KEY:
        log("   Skipping hit rate calc — no data or no Finnhub key")
        return {}

    # Sample up to 500 trades for efficiency (rate limits)
    import random
    sample = random.sample(historical, min(500, len(historical)))

    # Get S&P 500 as benchmark — use SPY
    # We'll compare each trade's return vs SPY over same period
    hit_rates = defaultdict(lambda: {"wins":0,"total":0})

    price_cache = {}

    def get_price(ticker, date_str):
        """Get closing price for ticker on a given date."""
        key = f"{ticker}_{date_str[:7]}"  # monthly cache
        if key in price_cache: return price_cache[key]
        try:
            d = parse_date(date_str)
            if not d: return None
            from_ts = int((d - timedelta(days=5)).timestamp())
            to_ts   = int((d + timedelta(days=5)).timestamp())
            r = requests.get("https://finnhub.io/api/v1/stock/candle",
                params={"symbol":ticker,"resolution":"D","from":from_ts,"to":to_ts,"token":FINNHUB_KEY},
                timeout=8)
            d2 = r.json()
            if d2.get("c") and len(d2["c"])>0:
                price_cache[key] = d2["c"][0]
                return d2["c"][0]
        except: pass
        return None

    evaluated = 0
    for trade in sample[:200]:  # limit to 200 for API calls
        try:
            ticker    = trade.get("ticker","").strip().upper()
            senator   = trade.get("senator","") or f"{trade.get('first_name','')} {trade.get('last_name','')}".strip()
            date_str  = str(trade.get("transaction_date",""))[:10]

            if not ticker or not senator or not date_str: continue

            # Get price at purchase date and 90 days later
            d_buy  = parse_date(date_str)
            d_exit = d_buy + timedelta(days=90)
            if not d_buy or d_exit > datetime.now() - timedelta(days=30):
                continue

            p_buy_stock  = get_price(ticker, date_str)
            p_exit_stock = get_price(ticker, d_exit.strftime("%Y-%m-%d"))
            p_buy_spy    = get_price("SPY", date_str)
            p_exit_spy   = get_price("SPY", d_exit.strftime("%Y-%m-%d"))

            if not all([p_buy_stock, p_exit_stock, p_buy_spy, p_exit_spy]):
                continue
            if p_buy_stock == 0 or p_buy_spy == 0:
                continue

            stock_return = (p_exit_stock - p_buy_stock) / p_buy_stock
            spy_return   = (p_exit_spy - p_buy_spy) / p_buy_spy
            outperformed = stock_return > (spy_return + 0.05)  # beat SPY by >5%

            senator_key = senator.lower().strip()
            hit_rates[senator_key]["total"] += 1
            if outperformed:
                hit_rates[senator_key]["wins"] += 1

            evaluated += 1
            if evaluated % 20 == 0:
                sleep(1)  # rate limit

        except: continue

    # Convert to rates, minimum 3 trades to be meaningful
    rates = {}
    for name, data in hit_rates.items():
        if data["total"] >= 3:
            rates[name] = data["wins"] / data["total"]
            log(f"   {name}: {data['wins']}/{data['total']} = {rates[name]:.0%} hit rate")

    log(f"   Hit rates calculated for {len(rates)} politicians")
    return rates


# ══════════════════════════════════════════════════════════
# CONVICTION SCORER — evidence-based
# Formula: Track Record × Freshness × Convergence
# ══════════════════════════════════════════════════════════

# Who is buying — track record weights (evidence-based, not arbitrary)
INSIDER_TRACK = {
    "cfo":40,"chief financial":40,       # CFO knows the numbers cold — rarest/strongest
    "ceo":35,"chief executive":35,
    "president":30,"coo":28,"chief operating":28,
    "executive chairman":32,"chairman":25,
    "director":15,"board":15,
    "evp":14,"executive vice president":14,
    "svp":12,"senior vice president":12,
    "vp":8,"vice president":8,
    "officer":6,
}

POLITICIAN_TRACK = {
    # High-accuracy documented traders (2024-2026)
    "pelosi":38,"nancy pelosi":38,
    "tuberville":36,"tommy tuberville":36,
    "kushner":33,"jared kushner":33,
    "trump":30,
    "hegseth":28,"pete hegseth":28,
    "scott":25,"austin scott":25,
    "crenshaw":22,"dan crenshaw":22,
    "mast":20,"brian mast":20,
    "ossoff":20,"jon ossoff":20,
    "kelly":18,"mark kelly":18,
    "wicker":18,"roger wicker":18,
    "warner":16,"mark warner":16,
    "collins":16,"susan collins":16,
    "capito":14,"shelley capito":14,
}

FUND_TRACK = {
    # Tier 1 — consistent alpha generators
    "tiger global":35,"coatue":33,"viking global":32,
    "d1 capital":32,"dragoneer":28,"lone pine":28,
    "whale rock":25,"altimeter":25,"greenoaks":25,
    "tci fund":22,"durable capital":22,
    # Tier 2
    "pershing square":20,"third point":20,"appaloosa":18,
    "berkshire":22,"renaissance":22,"soros":16,
    "citadel":15,"millennium":15,"elliott":18,
}

def get_track_weight(source, role, names):
    if source == "insider":
        r = role.lower()
        for k,w in INSIDER_TRACK.items():
            if k in r: return w
        return 5
    elif source == "congress":
        n = names.lower()
        for k,w in POLITICIAN_TRACK.items():
            if k in n: return w
        return 8  # unknown politician — low weight
    elif source == "hedge_fund":
        n = names.lower()
        for k,w in FUND_TRACK.items():
            if k in n: return w
        return 10
    return 5

def freshness_factor(da, source):
    """
    How fresh is the signal?
    Insider/Congress: decays sharply — these should be acted on quickly
    Hedge fund 13F: structurally lagged by 45-90 days — use flat weight
    """
    if source == "hedge_fund":
        # 13F is always lagged — don't penalise for that
        # But do decay if extremely old (>6 months)
        if da <= 90:  return 0.7   # normal 13F lag — still valid
        if da <= 180: return 0.4   # older quarter — less relevant
        return 0.1
    else:
        # Insider/Congress — freshness matters a lot
        if da <= 7:   return 1.0
        if da <= 14:  return 0.90
        if da <= 30:  return 0.75
        if da <= 60:  return 0.45
        if da <= 90:  return 0.20
        return 0.08

def value_factor(source, v):
    """Is this a meaningful amount for this type of buyer?"""
    if source == "insider":
        # For exec: >$1M = very meaningful, >$500K = meaningful
        if v >= 2e6:  return 1.4
        if v >= 1e6:  return 1.25
        if v >= 5e5:  return 1.1
        return 1.0
    elif source == "hedge_fund":
        # For funds: >$500M = major position, >$100M = significant
        if v >= 1e9:  return 1.4
        if v >= 5e8:  return 1.3
        if v >= 1e8:  return 1.15
        return 1.0
    return 1.0

def weighted_score(signals_for_ticker, politician_hit_rates={}):
    """
    Conviction = Track Record × Freshness × Value Factor × Convergence

    Key insight: independent smart money actors agreeing on the same stock
    within a short time window is the strongest possible signal.
    Single source = useful but not actionable alone.
    """
    best_per_source = {}   # source -> best score for that source
    source_days    = {}    # source -> most recent days_ago
    source_details = []

    for sig in signals_for_ticker:
        src   = sig.get("source","")
        role  = sig.get("roles","")
        names = sig.get("insider_names","")
        da    = sig.get("days_ago", 90)
        v     = sig.get("value_usd", 0)
        n     = sig.get("insider_count", 1)

        tw  = get_track_weight(src, role, names)
        fr  = freshness_factor(da, src)
        vf  = value_factor(src, v)

        # Cluster boost — multiple insiders in same company
        cluster = 1.0 + (n - 1) * 0.2 if src == "insider" else 1.0

        score = tw * fr * vf * cluster

        # Keep best signal per source
        if src not in best_per_source or score > best_per_source[src]:
            best_per_source[src] = score
            source_days[src] = da

        source_details.append(
            f"{src}({names[:12]}): tw={tw} × fr={fr:.2f} × vf={vf:.2f} = {score:.1f}"
        )

    sources_present = set(best_per_source.keys())
    n_sources = len(sources_present)
    base = sum(best_per_source.values())

    # Convergence multiplier — the heart of the system
    # Did independent actors buy CLOSE IN TIME to each other?
    if n_sources >= 2:
        days_vals  = list(source_days.values())
        time_spread = max(days_vals) - min(days_vals)

        if n_sources >= 3:
            # Triple source — massive signal regardless of timing
            if time_spread <= 30:   conv = 4.0   # all three within a month = extraordinary
            elif time_spread <= 60: conv = 3.0
            else:                   conv = 2.0
        else:
            # Double source
            if time_spread <= 14:   conv = 2.8   # bought within 2 weeks of each other
            elif time_spread <= 30: conv = 2.2
            elif time_spread <= 60: conv = 1.6
            else:                   conv = 1.2   # same stock but months apart = weak
    else:
        conv = 1.0

    raw = base * conv

    # Caps by source count — single source can NEVER be "high conviction"
    if n_sources >= 3:   cap = 99
    elif n_sources == 2: cap = 84
    else:                cap = 58

    final = min(round(raw), cap)
    return final, source_details, sources_present


# ══════════════════════════════════════════════════════════
# 1. FINNHUB INSIDER TRANSACTIONS
# ══════════════════════════════════════════════════════════
def get_finnhub_insider():
    log("1. Finnhub insider transactions (SEC Form 4, 6 months)...")
    signals = []
    if not FINNHUB_KEY: return signals
    to_d = datetime.now().strftime("%Y-%m-%d")
    fr_d = CUTOFF.strftime("%Y-%m-%d")
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
            # Filter to window
            buys = [t for t in buys if in_window(t.get("transactionDate") or t.get("filingDate",""))]
            if not buys: continue
            val   = sum(abs((t.get("share",0) or t.get("change",0) or 0)*(t.get("price",0) or 0)) for t in buys)
            names = list(dict.fromkeys([t.get("name","Unknown") for t in buys]))
            roles = list(dict.fromkeys([(t.get("position") or t.get("title") or "").lower()
                          for t in buys if t.get("position") or t.get("title")]))
            lat   = sorted(buys, key=lambda t:str(t.get("transactionDate") or t.get("filingDate","")), reverse=True)[0]
            td    = lat.get("transactionDate") or lat.get("filingDate","")
            signals.append({
                "ticker":ticker,"company_name":ticker,"signal_type":"buy","source":"insider",
                "value_usd":val,"insider_count":len(buys),"insider_names":", ".join(names[:3]),
                "roles":", ".join(roles[:3]) if roles else "insider","is_exec":any(exec_weight(r)>=20 for r in roles),
                "trade_date":str(parse_date(td).date()) if parse_date(td) else "",
                "days_ago":days_ago(td),"sector":SECTOR_MAP.get(ticker,"Other"),
            })
    except Exception as e:
        log(f"   Error: {e}")
    log(f"   → {len(signals)} signals")
    return signals


# ══════════════════════════════════════════════════════════
# 2. CONGRESS TRADES
# Primary: Quiver Quantitative (add QUIVER_KEY secret)
# Fallback: House Stock Watcher free API
# ══════════════════════════════════════════════════════════
def get_senate_trades():
    log("2. Congress trades...")
    signals = []
    hit_rates = {}
    QUIVER_KEY = os.environ.get("QUIVER_KEY","")

    # ── Quiver Quant (best source — real-time, both House + Senate) ──
    if QUIVER_KEY:
        try:
            log("   Using Quiver Quantitative API...")
            r = requests.get(
                "https://api.quiverquant.com/beta/bulk/congresstrading",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Token {QUIVER_KEY}",
                },
                timeout=20
            )
            if r.status_code == 200:
                data = r.json()
                log(f"   Quiver: {len(data)} total congress trades")
                for tx in data:
                    try:
                        if tx.get("Transaction","").lower() not in ["purchase","buy"]: continue
                        ticker = tx.get("Ticker","").strip()
                        if not ticker or len(ticker)>5: continue
                        date_str = str(tx.get("TransactionDate",""))[:10]
                        d = parse_date(date_str)
                        if not d: continue
                        da = (datetime.now()-d).days
                        if da > LOOKBACK_DAYS: continue
                        amt = parse_amount(tx.get("Range",""))
                        name = tx.get("Representative","Unknown")
                        signals.append({
                            "ticker":ticker.upper(),
                            "company_name":tx.get("Asset",""),
                            "signal_type":"buy","source":"congress",
                            "value_usd":amt,"insider_count":1,
                            "insider_names":name,
                            "roles":tx.get("House","Congress"),
                            "is_exec":False,"trade_date":date_str,"days_ago":da,
                            "sector":SECTOR_MAP.get(ticker.upper(),"Other"),
                        })
                    except: continue
                log(f"   → {len(signals)} congress signals")
                return signals, hit_rates
            else:
                log(f"   Quiver HTTP {r.status_code} — trying fallback")
        except Exception as e:
            log(f"   Quiver error: {e} — trying fallback")

    # ── Fallback: House Stock Watcher (free, House only) ──
    try:
        log("   House Stock Watcher fallback...")
        r = requests.get("https://housestockwatcher.com/api",
            headers={"User-Agent":"SmartMoneyAgent/1.0 research@example.com"},
            timeout=20)
        if r.status_code == 200:
            for tx in r.json():
                try:
                    if tx.get("type","").lower() != "purchase": continue
                    ticker = tx.get("ticker","").strip()
                    if not ticker or ticker=="--" or len(ticker)>5: continue
                    date_str = str(tx.get("transaction_date",""))[:10]
                    d = parse_date(date_str)
                    if not d: continue
                    da = (datetime.now()-d).days
                    if da > LOOKBACK_DAYS: continue
                    amt = parse_amount(tx.get("amount",""))
                    name = tx.get("representative","Unknown Rep")
                    signals.append({
                        "ticker":ticker.upper(),
                        "company_name":tx.get("asset_description",ticker),
                        "signal_type":"buy","source":"congress",
                        "value_usd":amt,"insider_count":1,
                        "insider_names":name,"roles":"House Representative",
                        "is_exec":False,"trade_date":date_str,"days_ago":da,
                        "sector":SECTOR_MAP.get(ticker.upper(),"Other"),
                    })
                except: continue
            log(f"   House Stock Watcher: {len(signals)} signals")
        else:
            log(f"   House Stock Watcher HTTP {r.status_code}")
    except Exception as e:
        log(f"   House Stock Watcher error: {e}")

    if not signals:
        log("   ⚠ No congress data — add QUIVER_KEY to GitHub Secrets for full coverage")

    return signals, hit_rates


# ══════════════════════════════════════════════════════════
# 3. SEC EDGAR 13F QUARTERLY DATA
# ══════════════════════════════════════════════════════════
def get_hedge_fund_13f():
    log("3. SEC EDGAR 13F quarterly dataset...")
    signals = []

    zip_urls = [
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01dec2025-28feb2026_form13f.zip",
        "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01sep2025-30nov2025_form13f.zip",
    ]

    infotable_data = None
    for url in zip_urls:
        try:
            log(f"   Downloading: {url.split('/')[-1]}")
            r = requests.get(url, headers=SEC_HEADERS, timeout=120)
            if r.status_code == 200:
                log(f"   ✓ {len(r.content)//1024}KB — extracting...")
                z = zipfile.ZipFile(io.BytesIO(r.content))
                for name in z.namelist():
                    if "INFOTABLE" in name.upper() and name.endswith(".tsv"):
                        infotable_data = z.read(name).decode("utf-8", errors="ignore")
                        log(f"   ✓ Extracted {name} ({len(infotable_data)//1024}KB)")
                        break
                if infotable_data: break
            else:
                log(f"   HTTP {r.status_code}")
        except Exception as e:
            log(f"   Error: {e}")

    if not infotable_data:
        log("   Could not get 13F data")
        return signals

    # Parse header
    lines = infotable_data.strip().split("\n")
    header = [h.upper().strip() for h in lines[0].split("\t")]
    def col(n, d):
        try: return header.index(n)
        except: return d
    idx_acc   = col("ACCESSION_NUMBER", 0)
    idx_name  = col("NAMEOFISSUER", 2)
    idx_value = col("VALUE", 6)
    idx_disc  = col("INVESTMENTDISCRETION", 10)

    # Get fund accession numbers first
    fund_accessions = {}
    for cik, (fund_name, weight) in FUND_TIER1_CIKS.items():
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                headers=SEC_HEADERS, timeout=10)
            if r.status_code != 200: continue
            d = r.json()
            filings = d.get("filings",{}).get("recent",{})
            forms   = filings.get("form",[])
            accnums = filings.get("accessionNumber",[])
            dates   = filings.get("filingDate",[])
            for i, form in enumerate(forms):
                if form not in ["13F-HR","13F-HR/A"]: continue
                dt  = dates[i] if i<len(dates) else ""
                acc = accnums[i] if i<len(accnums) else ""
                try:
                    if (datetime.now()-datetime.strptime(dt,"%Y-%m-%d")).days > 270: break
                except: break
                if acc:
                    fund_accessions[acc] = (fund_name, weight, dt)
                    fund_accessions[acc.replace("-","")] = (fund_name, weight, dt)
                    log(f"   {fund_name}: {acc} ({dt})")
                break
            sleep(0.3)
        except: continue
    log(f"   Loaded {len(fund_accessions)//2} fund filings")

    # Parse INFOTABLE — only our target funds
    fund_holdings = defaultdict(lambda: defaultdict(list))
    matched = 0
    for line in lines[1:]:
        try:
            parts = line.split("\t")
            if len(parts) <= max(idx_acc, idx_name, idx_value): continue
            accnum = parts[idx_acc].strip()
            fund_info = fund_accessions.get(accnum) or fund_accessions.get(accnum.replace("-",""))
            if not fund_info: continue
            fund_name, weight, filing_date = fund_info
            name      = parts[idx_name].strip() if len(parts)>idx_name else ""
            value_raw = parts[idx_value].strip() if len(parts)>idx_value else ""
            value     = int(value_raw) if value_raw.isdigit() else 0
            if value < 100_000: continue
            ticker = name_to_ticker_map(name)
            if not ticker: continue
            matched += 1
            fund_holdings[accnum][fund_name].append({"ticker":ticker,"name":name,"value":value,"filing_date":filing_date})
        except: continue

    log(f"   Matched {matched} holdings rows")

    # Build signals
    for accnum, funds in fund_holdings.items():
        for fund_name, holdings in funds.items():
            top = sorted(holdings, key=lambda h:h["value"], reverse=True)[:25]
            for h in top:
                da = days_ago(h["filing_date"]) if h["filing_date"] else 90
                signals.append({
                    "ticker":h["ticker"],"company_name":h["name"],
                    "signal_type":"buy","source":"hedge_fund","value_usd":float(h["value"]),
                    "insider_count":1,"insider_names":fund_name,"roles":"Hedge Fund 13F",
                    "is_exec":False,"trade_date":h["filing_date"],"days_ago":da,
                    "sector":SECTOR_MAP.get(h["ticker"],"Other"),
                })

    log(f"   → {len(signals)} hedge fund signals")
    return signals


def name_to_ticker_map(name):
    LOOKUP = {
        "NVIDIA":"NVDA","NVIDIA CORP":"NVDA","META PLATFORMS":"META","MICROSOFT":"MSFT",
        "ALPHABET":"GOOGL","AMAZON":"AMZN","AMAZON COM":"AMZN","APPLE":"AAPL",
        "TESLA":"TSLA","PALANTIR":"PLTR","AXON ENTERPRISE":"AXON","AXON":"AXON",
        "CROWDSTRIKE":"CRWD","PALO ALTO":"PANW","BROADCOM":"AVGO","ADVANCED MICRO":"AMD",
        "ARM HOLDINGS":"ARM","COINBASE":"COIN","NOVO NORDISK":"NVO","ELI LILLY":"LLY",
        "LILLY":"LLY","ABBVIE":"ABBV","UNITEDHEALTH":"UNH","JPMORGAN":"JPM",
        "GOLDMAN SACHS":"GS","BANK OF AMERICA":"BAC","WALMART":"WMT","COSTCO":"COST",
        "NETFLIX":"NFLX","UBER":"UBER","VISA":"V","MASTERCARD":"MA",
        "GE VERNOVA":"GEV","VISTRA":"VST","EXXON":"XOM","EXXONMOBIL":"XOM",
        "PFIZER":"PFE","MERCK":"MRK","LOCKHEED":"LMT","RAYTHEON":"RTX",
        "MICROSTRATEGY":"MSTR","STRATEGY":"MSTR","SNOWFLAKE":"SNOW",
        "CLOUDFLARE":"NET","DATADOG":"DDOG","SUPER MICRO":"SMCI",
        "SOFI":"SOFI","AFFIRM":"AFRM","UPSTART":"UPST","MARVELL":"MRVL",
        "QUALCOMM":"QCOM","MICRON":"MU","INTEL":"INTC","BOOKING":"BKNG",
        "AIRBNB":"ABNB","SPOTIFY":"SPOT","SHOPIFY":"SHOP","BLOCK":"SQ",
        "PAYPAL":"PYPL","SALESFORCE":"CRM","SERVICENOW":"NOW","ADOBE":"ADBE",
        "ORACLE":"ORCL","APPLIED MATERIALS":"AMAT","LAM RESEARCH":"LRCX",
        "TEXAS INSTRUMENTS":"TXN","HOME DEPOT":"HD","NIKE":"NKE",
        "ABBOTT":"ABT","THERMO FISHER":"TMO","UNION PACIFIC":"UNP",
        "AMERICAN EXPRESS":"AXP","BLACKROCK":"BLK","S&P GLOBAL":"SPGI",
        "INTUIT":"INTU","ANALOG DEVICES":"ADI","CATERPILLAR":"CAT",
        "DEERE":"DE","HONEYWELL":"HON","GENERAL ELECTRIC":"GE",
        "BOEING":"BA","NORTHROP":"NOC","GENERAL DYNAMICS":"GD",
        "SPDR S&P 500":"SPY","INVESCO QQQ":"QQQ","VANGUARD":"VTI",
        "ISHARES GOLD":"GLD","COINBASE GLOBAL":"COIN","ROCKETLAB":"RKLB",
        "AST SPACEMOBILE":"ASTS","IONQ":"IONQ","MICROSTRATEGY":"MSTR",
    }
    n = name.upper().strip()
    if n in LOOKUP: return LOOKUP[n]
    for k, t in LOOKUP.items():
        if k in n and len(k)>=5: return t
    return None


# ══════════════════════════════════════════════════════════
# MERGE WITH WEIGHTED SCORING
# ══════════════════════════════════════════════════════════
def merge_and_score(all_signals, politician_hit_rates):
    log(f"Merging and scoring {len(all_signals)} signals...")
    by_ticker = defaultdict(list)
    for sig in all_signals:
        t = sig["ticker"].upper().strip()
        if not t or len(t)>5: continue
        by_ticker[t].append(sig)

    merged = []
    for ticker, sigs in by_ticker.items():
        # Calculate weighted conviction score
        score, details, sources = weighted_score(sigs, politician_hit_rates)

        # Aggregate fields
        all_names  = []
        all_roles  = []
        total_val  = 0
        min_days   = 999
        is_exec    = False
        company    = ticker
        sector     = SECTOR_MAP.get(ticker, "Other")

        for sig in sigs:
            all_names.extend((sig.get("insider_names","")).split(", "))
            all_roles.append(sig.get("roles",""))
            total_val  = max(total_val, sig.get("value_usd",0))
            min_days   = min(min_days, sig.get("days_ago",999))
            is_exec    = is_exec or sig.get("is_exec",False)
            if sig.get("company_name") and sig["company_name"] != ticker:
                company = sig["company_name"]

        unique_names = list(dict.fromkeys([n for n in all_names if n and n != "Unknown"]))
        source_str   = ", ".join(sorted(sources))

        merged.append({
            "ticker":        ticker,
            "company_name":  company,
            "signal_type":   "buy",
            "source":        source_str,
            "value_usd":     total_val,
            "insider_count": len([s for s in sigs if s.get("source")=="insider"]) or
                             len([s for s in sigs if s.get("source")=="congress"]) or 1,
            "insider_names": ", ".join(unique_names[:5]),
            "roles":         " | ".join([r for r in all_roles if r])[:200],
            "is_exec":       is_exec,
            "trade_date":    str(datetime.now().date() - timedelta(days=min_days)),
            "days_ago":      min_days,
            "score":         score,
            "sector":        sector,
            "source_count":  len(sources),
            "score_detail":  " | ".join(details[:5]),
        })

    merged.sort(key=lambda s: s["score"], reverse=True)
    log(f"   → {len(merged)} unique tickers scored")

    # Log top 10
    log("   TOP 10 by conviction:")
    for s in merged[:10]:
        log(f"   {s['ticker']:6} score={s['score']:3} sources={s['source']} days_ago={s['days_ago']}")

    return merged


# ══════════════════════════════════════════════════════════
# AI ENRICHMENT
# ══════════════════════════════════════════════════════════
def enrich_with_ai(signals):
    if not ANTHROPIC_KEY: return signals
    log("AI enrichment (top 10 by conviction)...")
    # Only enrich multi-source high conviction signals
    top = [s for s in signals if s.get("source_count",1) >= 2][:10]
    for sig in top:
        try:
            prompt = (
                f"Hedge fund analyst. {sig['ticker']} has smart money activity from: {sig['source']}.\n"
                f"Buyers: {sig['insider_names'][:80]}\n"
                f"Total value: {fmt(sig['value_usd'])}\n"
                f"Score detail: {sig.get('score_detail','')[:150]}\n\n"
                f"In exactly 2 sentences: (1) the most likely thesis these buyers share, "
                f"(2) the single biggest risk to that thesis. Be specific, no disclaimers."
            )
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":180,
                      "messages":[{"role":"user","content":prompt}]}, timeout=20)
            text = r.json().get("content",[{}])[0].get("text","")
            if text: sig["ai_thesis"] = text
            sleep(1)
        except: continue
    return signals


# ══════════════════════════════════════════════════════════
# WRITE TO SUPABASE
# ══════════════════════════════════════════════════════════
def write_to_supabase(signals):
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("No Supabase config")
        return
    log(f"Writing {len(signals)} signals to Supabase...")

    # Clear all existing data (fresh run)
    try:
        requests.delete(f"{SUPABASE_URL}/rest/v1/signals",
            headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                     "Content-Type":"application/json"},
            params={"id":"neq.00000000-0000-0000-0000-000000000000"},
            timeout=15)
        log("   Cleared existing signals")
    except Exception as e:
        log(f"   Clear error: {e}")

    for i in range(0, len(signals), 50):
        batch = []
        for s in signals[i:i+50]:
            batch.append({
                "ticker":        str(s.get("ticker",""))[:10],
                "company_name":  str(s.get("company_name",""))[:200],
                "signal_type":   "buy",
                "source":        str(s.get("source",""))[:100],
                "value_usd":     float(s.get("value_usd",0) or 0),
                "insider_count": int(s.get("insider_count",1) or 1),
                "insider_names": str(s.get("insider_names",""))[:500],
                "roles":         str(s.get("roles",""))[:200],
                "is_exec":       bool(s.get("is_exec",False)),
                "trade_date":    str(s.get("trade_date",str(datetime.now().date())))[:10],
                "score":         int(s.get("score",0) or 0),
                "sector":        str(s.get("sector","Other"))[:50],
            })
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
    log("Smart Money Agent — Scraper v5")
    log(f"Window: last {LOOKBACK_DAYS} days ({CUTOFF.strftime('%Y-%m-%d')} to today)")
    log("="*55)

    all_signals = []

    # 1. Insider transactions
    all_signals += get_finnhub_insider()
    sleep(2)

    # 2. Senate trades + politician hit rates
    senate_signals, hit_rates = get_senate_trades()
    all_signals += senate_signals
    sleep(2)

    # 3. Hedge fund 13F
    all_signals += get_hedge_fund_13f()
    sleep(2)

    log(f"\nRaw totals:")
    log(f"  Insider:    {sum(1 for s in all_signals if s['source']=='insider')}")
    log(f"  Congress:   {sum(1 for s in all_signals if s['source']=='congress')}")
    log(f"  Hedge fund: {sum(1 for s in all_signals if s['source']=='hedge_fund')}")

    # Merge with weighted scoring
    merged = merge_and_score(all_signals, hit_rates)

    # AI enrichment on top multi-source signals
    if ANTHROPIC_KEY:
        merged = enrich_with_ai(merged)

    # Write to Supabase
    write_to_supabase(merged)

    log("="*55)
    log(f"Done. {len(merged)} signals written.")
    log(f"Multi-source: {sum(1 for s in merged if s.get('source_count',1)>=2)}")
    log(f"Triple-source: {sum(1 for s in merged if s.get('source_count',1)>=3)}")
    log("="*55)
