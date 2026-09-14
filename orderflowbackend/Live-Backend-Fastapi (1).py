"""
LIVE BACKEND for NSE OrderFlow Alpha
FastAPI server that bypasses NSE CORS/cookie blocks and serves live data to your React dashboard.

Deploy FREE in 2 mins on Render.com / Railway / Fly.io

Endpoints:
- GET /api/live-orders -> List of large order wins with F&O confluence
- GET /api/fno/{symbol} -> F&O data for symbol
- GET / -> Health check

How it works:
1. Gets NSE cookies by hitting nseindia.com homepage
2. Fetches corporate announcements API with cookies
3. Filters for order win keywords
4. For each F&O stock, fetches F&O quote API for OI buildup
5. Returns JSON that your dashboard consumes

CORS enabled for your frontend.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import re
from datetime import datetime
import time

app = FastAPI(title="OrderFlow Alpha LIVE API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

FNO_FALLBACK = ["LT","BEL","HAL","BHEL","INFY","TCS","KEC","COFORGE","LTIM","SIEMENS","ABB","POWERGRID","NTPC","RELIANCE","ONGC","HINDALCO","JSWSTEEL","TATASTEEL","ADANIENT","ADANIPORTS"]

def get_nse_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        # NSE blocks without this first hit to get cookies
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.5)
    except:
        pass
    return s

def extract_order_value(text):
    """Extract Cr value from announcement text"""
    if not text:
        return None
    patterns = [
        r'₹\s*([\d,]+\.?\d*)\s*(?:crore|cr)',
        r'Rs\.?\s*([\d,]+\.?\d*)\s*(?:crore|cr)',
        r'INR\s*([\d,]+\.?\d*)\s*Cr',
        r'([\d,]+\.?\d*)\s*Crore',
    ]
    text_lower = text.lower()
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = m.group(1).replace(',', '')
                return float(val)
            except:
                continue
    return None

def get_fno_list(session):
    try:
        url = "https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"
        r = session.get(url, timeout=10)
        data = r.json()
        return set([d['symbol'] for d in data['data']])
    except Exception as e:
        print(f"FNO list failed: {e}")
        return set(FNO_FALLBACK)

def get_live_order_wins():
    session = get_nse_session()
    fno_set = get_fno_list(session)
    
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    try:
        r = session.get(url, timeout=10)
        ann = r.json()
    except Exception as e:
        print(f"Announcements fetch failed: {e}")
        return {"error": str(e), "data": []}

    order_wins = []
    keywords_order = ["order", "contract", "loa", "letter of award", "award"]
    keywords_action = ["receives", "bags", "wins", "secures", "awarded", "bagged", "obtains"]

    for item in ann[:100]:  # Check latest 100
        subject = (item.get('subject') or '').lower()
        if not any(k in subject for k in keywords_order):
            continue
        if not any(k in subject for k in keywords_action):
            # Sometimes subject is just "Award of Order" without verb, still include if order value present
            if "award" not in subject and "order" not in subject:
                continue

        symbol = item.get('symbol')
        if symbol not in fno_set:
            continue  # Only F&O as per requirement

        desc = item.get('desc') or item.get('subject') or ""
        order_val = extract_order_value(desc) or extract_order_value(subject)
        
        # Only track larger orders >100Cr
        if order_val and order_val < 100:
            continue

        # Fetch F&O confluence for this symbol
        fno_signal = None
        oi_change = 0
        price_change = 0
        try:
            q_url = f"https://www.nseindia.com/api/quote-derivative?symbol={symbol}"
            qr = session.get(q_url, timeout=8)
            qd = qr.json()
            stocks = qd.get('stocks', [])
            # Find futures
            fut = [x for x in stocks if 'FUT' in x.get('metadata',{}).get('instrumentType','') or 'Futures' in str(x.get('metadata',{}))]
            if fut:
                latest = fut[0]
                price_change = latest.get('pChange', 0) or latest.get('change', 0)
                # OI change calc - NSE provides changeInOpenInterest
                oi = latest.get('openInterest', 0)
                prev_oi = latest.get('pclose', 0)  # approximation, better to use historical
                # Use changeInOpenInterest percent if available
                oi_change = latest.get('changeInOpenInterest', 0)
                if isinstance(oi_change, (int,float)) and oi and oi !=0:
                    # If absolute, convert to %
                    if abs(oi_change) > 100: # absolute number
                        oi_change = (oi_change / (oi - oi_change) * 100) if (oi - oi_change)!=0 else 0
                
                # Signal logic
                if price_change > 0.8 and oi_change > 2:
                    fno_signal = "LONG_BUILDUP"
                elif price_change > 0.8 and oi_change < -2:
                    fno_signal = "SHORT_COVERING"
                elif price_change < -0.8 and oi_change > 2:
                    fno_signal = "SHORT_BUILDUP"
                elif price_change < -0.8 and oi_change < -2:
                    fno_signal = "LONG_UNWINDING"
                else:
                    fno_signal = "NEUTRAL"
        except Exception as e:
            # print(f"F&O fetch failed for {symbol}: {e}")
            fno_signal = "DATA_NA"

        # Impact score (need revenue, placeholder 10% if order >500Cr)
        impact = 5.0
        if order_val:
            if order_val > 2000: impact = 9.5
            elif order_val > 1000: impact = 8.8
            elif order_val > 500: impact = 8.0
            elif order_val > 250: impact = 7.0
            else: impact = 6.0

        verdict = "WATCH"
        if impact >= 8.5 and fno_signal == "LONG_BUILDUP":
            verdict = "ULTRA_BULLISH"
        elif impact >= 8.5 and fno_signal == "SHORT_COVERING":
            verdict = "BULLISH_SQUEEZE"

        order_wins.append({
            "symbol": symbol,
            "company": symbol,
            "date": item.get('an_dt') or item.get('date') or datetime.now().isoformat(),
            "subject": item.get('subject'),
            "order_value_cr": order_val or 0,
            "client": "See announcement",  # Parse from text in advanced version
            "sector": "To be mapped",
            "source": "NSE",
            "pdf_link": item.get('attchmntText') or item.get('pdfLink') or "",
            "price_change_pct": round(price_change, 2),
            "oi_change_pct": round(oi_change, 2) if isinstance(oi_change, (int,float)) else 0,
            "fno_signal": fno_signal,
            "impact_score": impact,
            "combo_verdict": verdict,
            "timestamp": datetime.now().isoformat()
        })
        time.sleep(0.3)  # Be nice to NSE

    return order_wins

@app.get("/")
def health():
    return {"status": "OrderFlow Alpha LIVE", "time": datetime.now().isoformat(), "message": "Use /api/live-orders"}

@app.get("/api/live-orders")
def live_orders():
    data = get_live_order_wins()
    if isinstance(data, dict) and "error" in data:
        return {"status": "error", "count": 0, "data": [], "error": data["error"], "timestamp": datetime.now().isoformat()}
    return {
        "status": "live" if data else "empty",
        "count": len(data),
        "data": data,
        "timestamp": datetime.now().isoformat(),
        "source": "NSE Corporate Announcements + NSE F&O API"
    }

@app.get("/api/fno/{symbol}")
def fno_for_symbol(symbol: str):
    session = get_nse_session()
    try:
        url = f"https://www.nseindia.com/api/quote-derivative?symbol={symbol.upper()}"
        r = session.get(url, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# For local run: uvicorn live_backend_fastapi:app --reload --port 8000
# Deploy to Render: 
# 1. Create new Web Service, connect this file
# 2. Build command: pip install fastapi uvicorn requests
# 3. Start command: uvicorn live_backend_fastapi:app --host 0.0.0.0 --port $PORT
