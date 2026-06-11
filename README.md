# Polymarket Dip Scanner

Detects unusually large price dips in high-volume [Polymarket](https://polymarket.com)
markets, then cross-checks Google News to answer one question:

> **Does the news explain this dip — or not?**

A big drop *with* headlines is just repricing (a candidate dropped out, a team
lost, a rate decision landed). A big drop with **no findable news** is more
interesting: a possible overreaction, cascade, or liquidity event — something
worth a human look. This tool finds those moments automatically.

No API keys required.

## Example output

A real run during the 2026 NBA Finals:

```
[ 12] NEWS-DRIVEN   -47pts  0.64->0.17 (now 0.20, recovered 6%)   Will the San Antonio Spurs win the 2026 NBA Finals?
        - Knicks stage historic Game 4 comeback against Spurs, 1 win away from title
        - Data shows just how close these NBA Finals games have been
[ 27] NEWS-DRIVEN   -27pts  0.63->0.36 (now 0.80, recovered 165%)  Will the New York Knicks win the 2026 NBA Finals?
        - New York Knicks win Game 4, pulling off greatest comeback in NBA Finals history
```

Both dips correctly classified as news-driven (it was Game 4 of the Finals).
On most days you'll get a handful of rows; the rare **UNEXPLAINED** ones are
the point.

## How it works

1. Pulls the top N active markets by volume from Polymarket's public
   [Gamma API](https://gamma-api.polymarket.com).
2. Fetches hourly price history for each from the
   [CLOB API](https://clob.polymarket.com).
3. Flags any peak-to-trough drop ≥ the threshold (default: 10 probability
   points within 24h), skipping near-zero longshots and illiquid markets
   where a single trade can fake a crash.
4. Extracts keywords from each flagged market's question (names first) and
   queries Google News RSS for recent headlines.
5. Classifies each dip **NEWS-DRIVEN** (recent headlines found) or
   **UNEXPLAINED** (none found), prints a report, and saves a timestamped CSV.

Each row includes how far the price has already bounced back
(`RecoveryPct`) — a dip that's 90% recovered has been arbitraged; one
sitting at the bottom is still an open question.

## Install

```bash
git clone https://github.com/<ManasJagdale>/polymarket-dip-scanner
cd polymarket-dip-scanner
pip install -r requirements.txt
```

Python 3.9+. Dependencies: `requests`, `pandas`.

## Usage

```bash
# default scan: top 150 markets, 10-point dips, last 24h  (~3-5 min)
python dip_scanner.py

# faster scan of just the biggest markets
python dip_scanner.py --markets 50

# stricter: 15-point dips within the last 12 hours
python dip_scanner.py --threshold 0.15 --hours 12
```

| Flag | Default | Meaning |
|---|---|---|
| `--markets` | 150 | top N markets by volume to scan |
| `--threshold` | 0.10 | minimum dip size (probability points, 0–1 scale) |
| `--hours` | 24 | lookback window |
| `--min-volume` | 100000 | skip markets below this volume ($) |

Results are saved to `./polymarket_analysis/dips_<timestamp>.csv` with full
details: peak/trough prices and times, current price, recovery %, volume,
the news query used, and the headlines found.

## Honest caveats (read before trading anything)

- **UNEXPLAINED ≠ buy signal.** It means Google News hasn't indexed an
  explanation — not that none exists. Market-moving information often breaks
  on X/Telegram/Discord first, or is informed flow that never gets a
  headline. Treat UNEXPLAINED rows as a manual-review queue.
- **Live sports markets trip the detector during games.** Those swings are
  the scoreboard, not sentiment (see the example above). The attached
  headlines usually make this obvious in seconds.
- **Keyword extraction is crude.** Verify the headlines actually relate to
  the market before trusting a NEWS-DRIVEN classification.
- **Dips can keep dipping.** Even a genuine overreaction has no obligation
  to revert on your schedule. Whether unexplained Polymarket dips actually
  mean-revert is an open empirical question — log the CSVs and check prices
  24/48h later before betting on the assumption.

**Nothing here is financial advice. Trade at your own risk, with money you
can afford to lose, where it is legal for you to do so.**

## Roadmap

- [ ] Outcome tracker: record each dip's price at +6/24/48h to measure
      whether unexplained dips actually revert
- [ ] Loop mode: rescan every 15 minutes, alert only on *new* dips
- [ ] Spike detection (overreactions go both ways)
- [ ] Smarter news matching and source expansion beyond Google News

## Data sources

- Gamma API — markets and volume (public, unauthenticated)
- CLOB API — hourly price candles (public, unauthenticated)
- Google News RSS — headline correlation (no key needed)

The script includes polite request delays — please don't remove them and
hammer the endpoints.

## License

MIT — see [LICENSE](LICENSE).
