"""
Sector flows collector for Rafy's market reports.

Downloads State Street's daily NAV history file for the 11 Select Sector SPDR
ETFs, computes daily net flows (change in shares outstanding x NAV), and writes:
  data/latest.json      - summary the morning/afternoon reports read
  data/latest.txt       - the same summary in plain text
  data/daily_<TICKER>.csv - full daily history per fund

Runs on GitHub Actions (see .github/workflows/collect.yml). Uses only public data.
"""
import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

FUNDS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Health Care",
    "XLI": "Industrials",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLU": "Utilities",
    "XLC": "Communication Services",
    "XLB": "Materials",
    "XLRE": "Real Estate",
}

URLS = [
    "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/navhist-us-en-{t}.xlsx",
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/navhist-us-en-{t}.xlsx",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
}

OUT = Path("data")


def download(ticker: str) -> bytes:
    last_err = None
    for pattern in URLS:
        url = pattern.format(t=ticker.lower())
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=60)
                if r.status_code == 200 and r.content[:2] == b"PK":
                    return r.content
                last_err = f"HTTP {r.status_code} from {url}"
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(last_err or "download failed")


def _find_col(cols, *needles):
    for c in cols:
        name = str(c).strip().lower()
        if all(n in name for n in needles):
            return c
    return None


def parse(content: bytes) -> pd.DataFrame:
    """Return DataFrame with columns date, nav, shares sorted by date ascending."""
    raw = pd.read_excel(io.BytesIO(content), header=None)
    header_row = None
    for i in range(min(len(raw), 40)):
        cells = [str(x).strip().lower() for x in raw.iloc[i].tolist()]
        if any(c == "date" or c.startswith("date") for c in cells) and any("nav" in c for c in cells):
            header_row = i
            break
    if header_row is None:
        raise ValueError("could not find the header row (Date / NAV)")

    df = raw.iloc[header_row + 1 :].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_row].tolist()]
    date_col = _find_col(df.columns, "date")
    nav_col = _find_col(df.columns, "nav")
    sh_col = _find_col(df.columns, "shares")
    if not (date_col and nav_col and sh_col):
        raise ValueError(f"missing columns; found {list(df.columns)}")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col], errors="coerce", format="mixed"),
            "nav": pd.to_numeric(df[nav_col].astype(str).str.replace(r"[,$]", "", regex=True), errors="coerce"),
            "shares": pd.to_numeric(df[sh_col].astype(str).str.replace(",", "", regex=False), errors="coerce"),
        }
    ).dropna()
    out = out[(out["nav"] > 0) & (out["shares"] > 0)]
    out = out.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    if len(out) < 6:
        raise ValueError("not enough rows in the file")
    out["flow"] = out["shares"].diff() * out["nav"]
    return out


def last_completed_week(dates: pd.Series):
    """Mon-Fri week ending on the most recent Friday that is <= latest data date."""
    latest = dates.max().normalize()
    friday = latest - timedelta(days=(latest.weekday() - 4) % 7)
    monday = friday - timedelta(days=4)
    return monday, friday


def summarize(ticker: str, df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    last5 = df.tail(5)
    mon, fri = last_completed_week(df["date"])
    wk = df[(df["date"] >= mon) & (df["date"] <= fri + timedelta(hours=23))]
    return {
        "sector": FUNDS[ticker],
        "latest_date": last["date"].strftime("%Y-%m-%d"),
        "nav": round(float(last["nav"]), 4),
        "shares_outstanding": int(last["shares"]),
        "aum_usd": round(float(last["nav"] * last["shares"]), 0),
        "flow_last_5_days_usd": round(float(last5["flow"].sum()), 0),
        "last_5_days_from": last5["date"].iloc[0].strftime("%Y-%m-%d"),
        "last_5_days_to": last5["date"].iloc[-1].strftime("%Y-%m-%d"),
        "flow_last_completed_week_usd": round(float(wk["flow"].sum()), 0) if len(wk) else None,
        "week_from": mon.strftime("%Y-%m-%d"),
        "week_to": fri.strftime("%Y-%m-%d"),
        "week_trading_days_found": int(len(wk)),
        "daily_last_10": [
            {"date": r.date.strftime("%Y-%m-%d"), "flow_usd": round(float(r.flow), 0)}
            for r in df.tail(10).itertuples()
        ],
    }


def pct(flow, aum):
    return round(flow / aum * 100, 1) if flow is not None and aum else None


def main() -> int:
    OUT.mkdir(exist_ok=True)
    funds, errors = {}, {}
    for t in FUNDS:
        try:
            df = parse(download(t))
            df.to_csv(OUT / f"daily_{t}.csv", index=False, date_format="%Y-%m-%d")
            funds[t] = summarize(t, df)
        except Exception as e:  # keep going; report the error in the output
            errors[t] = str(e)[:300]
        time.sleep(1)

    result = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "State Street (SSGA) daily NAV history files; flow = change in shares outstanding x NAV",
        "funds_ok": len(funds),
        "funds": funds,
        "errors": errors,
    }
    (OUT / "latest.json").write_text(json.dumps(result, indent=2))

    lines = [f"Sector SPDR flows - generated {result['generated_utc']}", ""]
    for t, f in funds.items():
        lines.append(
            f"{t} {f['sector']}: data to {f['latest_date']} | AUM ${f['aum_usd']/1e9:.1f}B | "
            f"last 5 days ${f['flow_last_5_days_usd']/1e6:+,.0f}M ({pct(f['flow_last_5_days_usd'], f['aum_usd'])}%) | "
            f"week {f['week_from']}..{f['week_to']} "
            + (
                f"${f['flow_last_completed_week_usd']/1e6:+,.0f}M ({pct(f['flow_last_completed_week_usd'], f['aum_usd'])}%)"
                if f["flow_last_completed_week_usd"] is not None
                else "n/a"
            )
        )
    for t, e in errors.items():
        lines.append(f"ERROR {t}: {e}")
    (OUT / "latest.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if funds else 1


if __name__ == "__main__":
    sys.exit(main())
