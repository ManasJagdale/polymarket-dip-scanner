#!/usr/bin/env python3
"""
POLYMARKET DIP SCANNER + NEWS CORRELATOR
=========================================
Theory: a big price dip WITH news is repricing (skip it); a big dip with
NO findable news is a potential overreaction / liquidity event (investigate).

Pipeline:
  1. Pull high-volume active markets from the Gamma API.
  2. For each, fetch recent price history from the CLOB API.
  3. Flag "dips": drop >= DIP_THRESHOLD points within LOOKBACK_HOURS.
  4. For each flagged market, query Google News RSS for recent headlines.
  5. Classify: NEWS-DRIVEN (headlines found in window) vs UNEXPLAINED.
  6. Print a report + save CSV.

Run:  python dip_scanner.py
Deps: requests, pandas  (no API keys needed)

HONEST CAVEATS
--------------
- "No headlines found" != "no information". News can be on X/Telegram/
  Discord before Google News indexes it, or be insider flow. UNEXPLAINED
  means "you should look manually", never "buy now".
- Keyword extraction from market questions is crude; check the headlines
  actually relate to the market before trusting the classification.
- This finds candidates. It does not size bets, and dips can keep dipping.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

# ---------------------------------------------------------------- config ---

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

MARKETS_TO_SCAN = 150        # top markets by volume
DIP_THRESHOLD = 0.10         # 10 probability points
LOOKBACK_HOURS = 24          # dip must occur within this window
HISTORY_FIDELITY = 60        # minutes per candle from CLOB API
MIN_PRICE_BEFORE_DIP = 0.10  # ignore noise on near-zero longshots
MIN_VOLUME = 100_000         # $ — skip illiquid markets (fake moves)
NEWS_WINDOW_HOURS = 36       # headlines within this window count as "news"
MAX_HEADLINES = 5
REQUEST_DELAY = 0.4          # be polite to the APIs

OUTPUT_FOLDER = Path("./polymarket_analysis")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

STOPWORDS = {
    "will", "the", "a", "an", "be", "by", "in", "on", "at", "of", "to",
    "win", "wins", "winner", "before", "after", "than", "more", "next",
    "who", "what", "which", "when", "how", "many", "much", "end", "2025",
    "2026", "2027", "2028", "or", "and", "vs", "for", "is", "does",
}


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------- market fetching ---

def fetch_top_markets(n: int = MARKETS_TO_SCAN) -> List[dict]:
    """Top active markets by volume, with their CLOB token IDs."""
    markets: List[dict] = []
    offset = 0
    while len(markets) < n:
        params = {
            "limit": min(100, n - len(markets)),
            "offset": offset,
            "active": "true",
            "closed": "false",
            "order": "volumeNum",
            "ascending": "false",
        }
        r = requests.get(GAMMA_MARKETS_URL, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        markets.extend(page)
        if len(page) < params["limit"]:
            break
        offset += len(page)
    return markets[:n]


def yes_token_id(market: dict) -> Optional[str]:
    """Extract the YES outcome's CLOB token id."""
    tokens = market.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            return None
    if isinstance(tokens, list) and tokens:
        return str(tokens[0])  # convention: first token = Yes
    return None


def fetch_price_history(token_id: str, hours: int) -> pd.DataFrame:
    """Hourly price candles for the last `hours` hours."""
    end = int(time.time())
    start = end - hours * 3600
    params = {
        "market": token_id,
        "startTs": start,
        "endTs": end,
        "fidelity": HISTORY_FIDELITY,
    }
    r = requests.get(CLOB_HISTORY_URL, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    hist = r.json().get("history", [])
    if not hist:
        return pd.DataFrame()
    df = pd.DataFrame(hist)  # columns: t (unix), p (price)
    df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df["p"] = df["p"].astype(float)
    return df.sort_values("t").reset_index(drop=True)


# --------------------------------------------------------- dip detection ---

def detect_dip(df: pd.DataFrame) -> Optional[Dict]:
    """
    Largest peak-to-trough drop in the window. Returns dip info if it
    exceeds DIP_THRESHOLD and started from a non-trivial price.
    """
    if df.empty or len(df) < 3:
        return None

    prices = df["p"].values
    times = df["t"].values

    running_max = prices[0]
    running_max_t = times[0]
    best = None

    for i in range(1, len(prices)):
        if prices[i] > running_max:
            running_max = prices[i]
            running_max_t = times[i]
            continue
        drop = running_max - prices[i]
        if drop >= DIP_THRESHOLD and running_max >= MIN_PRICE_BEFORE_DIP:
            if best is None or drop > best["drop"]:
                best = {
                    "peak_price": round(float(running_max), 3),
                    "trough_price": round(float(prices[i]), 3),
                    "drop": round(float(drop), 3),
                    "peak_time": pd.Timestamp(running_max_t),
                    "trough_time": pd.Timestamp(times[i]),
                    "current_price": round(float(prices[-1]), 3),
                }
    if best:
        # how much has it already bounced back? (0 = none, 1 = full recovery)
        rng = best["peak_price"] - best["trough_price"]
        best["recovery_pct"] = round(
            100 * (best["current_price"] - best["trough_price"]) / rng, 1
        ) if rng > 0 else 0.0
    return best


# ----------------------------------------------------------- news lookup ---

def keywords_from_question(question: str, max_words: int = 5) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z'.-]+", question)
    # prefer capitalized words (names/entities), then others
    caps = [w for w in words if w[0].isupper() and w.lower() not in STOPWORDS]
    rest = [w for w in words if w[0].islower() and w.lower() not in STOPWORDS]
    chosen = (caps + rest)[:max_words]
    return " ".join(chosen)


def fetch_news(query: str, window_hours: int = NEWS_WINDOW_HOURS) -> List[Dict]:
    """Recent headlines from Google News RSS (no API key)."""
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = f"{GOOGLE_NEWS_RSS}?{urllib.parse.urlencode(params)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    items = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        pub = item.findtext("pubDate") or ""
        link = item.findtext("link") or ""
        try:
            pub_dt = pd.to_datetime(pub, utc=True)
        except Exception:
            continue
        if pub_dt >= cutoff:
            items.append({"title": title, "published": pub_dt, "link": link})
        if len(items) >= MAX_HEADLINES:
            break
    return items


# ----------------------------------------------------------------- main ---

def scan() -> pd.DataFrame:
    print(f"Fetching top {MARKETS_TO_SCAN} markets by volume...")
    markets = fetch_top_markets(MARKETS_TO_SCAN)
    print(f"  Got {len(markets)}. Checking price histories for dips "
          f">= {DIP_THRESHOLD*100:.0f} pts in last {LOOKBACK_HOURS}h...\n")

    rows = []
    for idx, m in enumerate(markets, 1):
        if _safe_float(m.get("volumeNum") or m.get("volume")) < MIN_VOLUME:
            continue
        token = yes_token_id(m)
        if not token:
            continue

        try:
            hist = fetch_price_history(token, LOOKBACK_HOURS)
        except Exception:
            continue
        time.sleep(REQUEST_DELAY)

        dip = detect_dip(hist)
        if not dip:
            continue

        question = m.get("question", "Unknown")
        query = keywords_from_question(question)
        headlines = fetch_news(query) if query else []
        time.sleep(REQUEST_DELAY)

        classification = "NEWS-DRIVEN" if headlines else "UNEXPLAINED"
        print(f"[{idx:>3}] {classification:<12} -{dip['drop']*100:.0f}pts  "
              f"{dip['peak_price']:.2f}->{dip['trough_price']:.2f} "
              f"(now {dip['current_price']:.2f}, recovered {dip['recovery_pct']:.0f}%)  "
              f"{question[:55]}")
        for h in headlines[:3]:
            print(f"        - {h['title'][:90]}")

        rows.append({
            "Question": question,
            "Slug": m.get("slug", ""),
            "Classification": classification,
            "DropPts": round(dip["drop"] * 100, 1),
            "PeakPrice": dip["peak_price"],
            "TroughPrice": dip["trough_price"],
            "CurrentPrice": dip["current_price"],
            "RecoveryPct": dip["recovery_pct"],
            "PeakTime": dip["peak_time"],
            "TroughTime": dip["trough_time"],
            "Volume": _safe_float(m.get("volumeNum") or m.get("volume")),
            "NewsQuery": query,
            "Headlines": " | ".join(h["title"] for h in headlines),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        # UNEXPLAINED first (the interesting ones), then by drop size
        df["_sort"] = (df["Classification"] == "UNEXPLAINED").astype(int)
        df = df.sort_values(["_sort", "DropPts"], ascending=False).drop(columns="_sort")
        df = df.reset_index(drop=True)
    return df


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Detect Polymarket price dips and correlate with news")
    ap.add_argument("--markets", type=int, default=MARKETS_TO_SCAN,
                    help=f"top N markets by volume to scan (default {MARKETS_TO_SCAN})")
    ap.add_argument("--threshold", type=float, default=DIP_THRESHOLD,
                    help=f"min dip in probability points, 0-1 scale (default {DIP_THRESHOLD})")
    ap.add_argument("--hours", type=int, default=LOOKBACK_HOURS,
                    help=f"lookback window in hours (default {LOOKBACK_HOURS})")
    ap.add_argument("--min-volume", type=float, default=MIN_VOLUME,
                    help=f"skip markets below this volume in $ (default {MIN_VOLUME})")
    args = ap.parse_args()

    MARKETS_TO_SCAN = args.markets
    DIP_THRESHOLD = args.threshold
    LOOKBACK_HOURS = args.hours
    MIN_VOLUME = args.min_volume

    df = scan()
    if df.empty:
        print("\nNo dips found meeting the threshold. Try lowering DIP_THRESHOLD "
              "or extending LOOKBACK_HOURS.")
    else:
        unexplained = (df["Classification"] == "UNEXPLAINED").sum()
        print(f"\n=== {len(df)} dips found, {unexplained} unexplained ===")
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_FOLDER / f"dips_{datetime.now():%Y%m%d_%H%M}.csv"
        df.to_csv(out, index=False)
        print(f"Saved -> {out.resolve()}")
