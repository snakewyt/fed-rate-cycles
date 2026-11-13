# -*- coding: utf-8 -*-
"""Build the Fed hike/cut cycle dashboard.

Data:
  Federal Reserve H.15 monthly averages
    RIFSPFF_N.M     effective federal funds rate
    RIFLGFCY10_N.M  10-year Treasury yield
  Yahoo Finance  ^GSPC, ^IXIC, ^RUT monthly closes (Stooq as fallback)

FRED itself is not used: from this network the St. Louis Fed download stalls.

Cycle rule (see README):
  A turn is confirmed only after a 1.00 percentage-point reversal.
  If the rate then stays within 0.25 point of that extreme for at least
  9 months, the stretch is a hold. Zero-rate years and post-hike plateaus
  are not counted as cuts or hikes.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REVERSAL_PP = 1.0
PIVOT_BAND_PP = 0.30
FLAT_PP = 0.25
HOLD_MIN_MONTHS = 9
MIN_ANN_MONTHS = 6

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
WEB = ROOT / "web"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
UTC = timezone.utc

ASSETS = (
    ("spx", "标普 500", "^GSPC", "^spx"),
    ("ixic", "纳斯达克", "^IXIC", "^ndq"),
    ("rut", "罗素 2000", "^RUT", "^rut"),
)


def month_span(a: str, b: str) -> int:
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return (yb - ya) * 12 + (mb - ma)


def add_months(month: str, k: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + k
    y2, m2 = divmod(idx, 12)
    return f"{y2:04d}-{m2 + 1:02d}"


def enumerate_months(start: str, n: int) -> list[str]:
    return [add_months(start, i) for i in range(n)]


def dedupe(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    merged: dict[str, float] = {}
    for month, value in pairs:
        merged[month] = value
    return sorted(merged.items())


def detect_phases(series: list[tuple[str, float]]) -> list[dict]:
    """Split a monthly rate series into hike, cut, and hold phases.

    The last phase is marked ongoing: it has not been closed by a reversal.
    A phase that begins before the first confirmed turn is marked censored.
    """
    if FLAT_PP >= REVERSAL_PP:
        raise ValueError("plateau band must be narrower than the reversal")
    months = [m for m, _ in series]
    vals = [v for _, v in series]
    n = len(vals)
    if n < 3:
        return []

    pivots: list[dict] = []
    max_v = min_v = vals[0]
    max_i = min_i = 0
    mode = "init"
    best = vals[0]
    last_band = 0

    for i, v in enumerate(vals):
        if mode == "init":
            if v > max_v:
                max_v, max_i = v, i
            if v < min_v:
                min_v, min_i = v, i
            if max_v - min_v + 1e-6 >= REVERSAL_PP:
                if max_i > min_i:
                    pivots.append({"i": min_i, "kind": "trough", "best": min_v})
                    mode = "up"
                    best, last_band = max_v, max_i
                else:
                    pivots.append({"i": max_i, "kind": "peak", "best": max_v})
                    mode = "down"
                    best, last_band = min_v, min_i
            continue

        if mode == "up":
            if v > best + 1e-9:
                best, last_band = v, i
            elif v + 1e-6 >= best - PIVOT_BAND_PP:
                last_band = i
            if best - v + 1e-6 >= REVERSAL_PP:
                pivots.append(
                    {"i": last_band, "kind": "peak", "best": best, "provisional": False}
                )
                mode = "down"
                best, last_band = v, i
        else:
            if v < best - 1e-9:
                best, last_band = v, i
            elif v <= best + PIVOT_BAND_PP + 1e-6:
                last_band = i
            if v - best + 1e-6 >= REVERSAL_PP:
                pivots.append(
                    {"i": last_band, "kind": "trough", "best": best, "provisional": False}
                )
                mode = "up"
                best, last_band = v, i

    if mode == "up":
        pivots.append({"i": last_band, "kind": "peak", "best": best, "provisional": True})
    elif mode == "down":
        pivots.append({"i": last_band, "kind": "trough", "best": best, "provisional": True})

    phases: list[dict] = []

    def add(direction: str, i0: int, i1: int, *, ongoing: bool, censored: bool) -> None:
        if i1 <= i0:
            return
        window = vals[i0 : i1 + 1]
        phases.append(
            {
                "direction": direction,
                "start": months[i0],
                "end": months[i1],
                "months": month_span(months[i0], months[i1]),
                "ongoing": ongoing,
                "censored": censored,
                "rate_start": round(vals[i0], 4),
                "rate_end": round(vals[i1], 4),
                "rate_high": round(max(window), 4),
                "rate_low": round(min(window), 4),
            }
        )

    if pivots and pivots[0]["i"] > 0:
        kind = pivots[0]["kind"]
        # The sample opens mid-move, into the first extreme.
        add(
            "cut" if kind == "trough" else "hike",
            0,
            pivots[0]["i"],
            ongoing=False,
            censored=True,
        )

    for left, right in zip(pivots, pivots[1:]):
        if {left["kind"], right["kind"]} != {"peak", "trough"}:
            continue
        i0 = left["i"]
        extreme_i = right["i"]
        end_i = (n - 1) if right.get("provisional") else extreme_i
        if extreme_i <= i0:
            continue
        direction = "hike" if left["kind"] == "trough" else "cut"
        plateau = plateau_start(vals, i0, extreme_i, right["best"], FLAT_PP, HOLD_MIN_MONTHS)
        ongoing = bool(right.get("provisional"))
        if plateau is not None and plateau > i0:
            add(direction, i0, plateau, ongoing=False, censored=False)
            add("hold", plateau, end_i, ongoing=ongoing, censored=False)
        else:
            add(direction, i0, end_i, ongoing=ongoing, censored=False)

    for i, phase in enumerate(phases, start=1):
        phase["id"] = f"{i:02d}-{phase['direction']}-{phase['start']}"
    return phases


def next_confirmation(series: list[tuple[str, float]]) -> dict | None:
    """How far the latest rate is from the next 1.00-point reversal.

    Uses the same turn rule as detect_phases. After a trough, the next
    confirmation is a hike at trough + 1.00. After a peak, it is a cut at
    peak - 1.00. A further move in the current direction only shifts that line.
    """
    vals = [v for _, v in series]
    if len(vals) < 2:
        return None
    max_v = min_v = vals[0]
    max_i = min_i = 0
    mode = "init"
    best = vals[0]
    for i, v in enumerate(vals):
        if mode == "init":
            if v > max_v:
                max_v, max_i = v, i
            if v < min_v:
                min_v, min_i = v, i
            if max_v - min_v + 1e-6 >= REVERSAL_PP:
                if max_i > min_i:
                    mode = "up"
                    best = max_v
                else:
                    mode = "down"
                    best = min_v
            continue
        if mode == "up":
            if v > best + 1e-9:
                best = v
            if best - v + 1e-6 >= REVERSAL_PP:
                mode = "down"
                best = v
        else:
            if v < best - 1e-9:
                best = v
            if v - best + 1e-6 >= REVERSAL_PP:
                mode = "up"
                best = v
    latest = vals[-1]
    if mode == "init":
        span = max_v - min_v
        return {
            "direction": None,
            "trigger": None,
            "latest": round(latest, 4),
            "extreme": None,
            "extreme_kind": None,
            "gap_pp": round(max(0.0, REVERSAL_PP - span), 4),
            "done_pp": round(span, 4),
            "need_pp": REVERSAL_PP,
        }
    if mode == "down":
        trigger = best + REVERSAL_PP
        done = latest - best
        direction = "hike"
        extreme_kind = "trough"
    else:
        trigger = best - REVERSAL_PP
        done = best - latest
        direction = "cut"
        extreme_kind = "peak"
    return {
        "direction": direction,
        "trigger": round(trigger, 4),
        "latest": round(latest, 4),
        "extreme": round(best, 4),
        "extreme_kind": extreme_kind,
        "gap_pp": round(REVERSAL_PP - done, 4),
        "done_pp": round(done, 4),
        "need_pp": REVERSAL_PP,
    }


def plateau_start(
    vals: list[float], i0: int, i1: int, best: float, band: float, min_months: int
) -> int | None:
    """Start of the run that sits within `band` of the extreme and lasts min_months.

    Months after the rate has already left that neighborhood are skipped, so the
    early part of the next move does not break the plateau.
    """
    end = i1
    while end > i0 and abs(vals[end] - best) > band + 1e-6:
        end -= 1
    k = end
    while k > i0 and abs(vals[k - 1] - best) <= band + 1e-6:
        k -= 1
    if end - k >= min_months:
        return k
    return None


def par_bond_mod_duration_convexity(y: float, n: int = 10) -> tuple[float, float]:
    """Modified duration and convexity of an annual-pay par bond."""
    y = max(y, 1e-4)
    cashflows = [y] * (n - 1) + [1.0 + y]
    price = 0.0
    mac_num = 0.0
    conv_num = 0.0
    for t, cashflow in enumerate(cashflows, start=1):
        pv = cashflow / (1.0 + y) ** t
        price += pv
        mac_num += t * pv
        conv_num += t * (t + 1) * cashflow / (1.0 + y) ** (t + 2)
    modified = (mac_num / price) / (1.0 + y)
    convexity = conv_num / price
    return modified, convexity


def bond_index(yields_pct: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Approximate a 10-year constant-maturity total-return index, base 100.

    Monthly return ≈ carry − modified duration × Δy + ½ convexity × (Δy)²,
    with y in decimal. This is a model, not a published bond index.
    """
    if not yields_pct:
        return []
    level = 100.0
    out = [(yields_pct[0][0], level)]
    prev_y = yields_pct[0][1] / 100.0
    for month, pct in yields_pct[1:]:
        y = pct / 100.0
        modified, convexity = par_bond_mod_duration_convexity(prev_y)
        dy = y - prev_y
        ret = prev_y / 12.0 - modified * dy + 0.5 * convexity * dy * dy
        level *= 1.0 + ret
        if level <= 0:
            raise RuntimeError(f"bond index collapsed at {month}")
        out.append((month, level))
        prev_y = y
    return out


def value_at_start(series: dict[str, float], month: str) -> tuple[float | None, str | None]:
    if month in series:
        return series[month], month
    nxt = add_months(month, 1)
    if nxt in series:
        return series[nxt], nxt
    return None, None


def value_at_end(series: dict[str, float], month: str) -> tuple[float | None, str | None]:
    if month in series:
        return series[month], month
    prev = add_months(month, -1)
    if prev in series:
        return series[prev], prev
    return None, None


def phase_return(series: dict[str, float], start: str, end: str) -> dict | None:
    p0, m0 = value_at_start(series, start)
    p1, m1 = value_at_end(series, end)
    if p0 is None or p1 is None or p0 <= 0 or p1 <= 0 or m0 is None or m1 is None:
        return None
    span = month_span(m0, m1)
    if span <= 0:
        return None
    cumulative = p1 / p0 - 1.0
    if cumulative <= -1:
        annualized = None
    else:
        annualized = (1.0 + cumulative) ** (12.0 / span) - 1.0
    prices = [v for m, v in sorted(series.items()) if m0 <= m <= m1 and v > 0]
    drawdown = None
    if len(prices) >= 2:
        peak = prices[0]
        worst = 0.0
        for price in prices:
            peak = max(peak, price)
            worst = min(worst, price / peak - 1.0)
        drawdown = worst
    return {
        "cum": round(cumulative, 6),
        "ann": None if annualized is None else round(annualized, 6),
        "mdd": None if drawdown is None else round(drawdown, 6),
    }


def median(values: list[float]) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def download(url: str) -> bytes:
    # urllib hangs on FRED from this machine; curl completes the same request.
    result = subprocess.run(
        ["curl.exe", "-fsSL", "--max-time", "60", "-A", UA, url],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"curl exit {result.returncode}")
    return result.stdout


def fetch(url: str, cache_name: str, offline: bool) -> bytes:
    RAW.mkdir(parents=True, exist_ok=True)
    dest = RAW / cache_name
    if not offline:
        try:
            data = download(url)
            dest.write_bytes(data)
            return data
        except (TimeoutError, OSError, RuntimeError) as exc:
            print(f"download failed {cache_name}: {exc}")
            if not dest.exists():
                raise
            print(f"using cache {cache_name}")
    if not dest.exists():
        raise FileNotFoundError(f"no cache for {cache_name}; run without --offline")
    return dest.read_bytes()


# Prebuilt H.15 monthly package. The series id is the Fed's package hash for
# the monthly-average file that includes the effective fed funds rate.
H15_MONTHLY_URL = (
    "https://www.federalreserve.gov/datadownload/Output.aspx"
    "?rel=H15&series=d7e27b7b09a3a7feae95b9c61781fcd8"
    "&filetype=csv&label=include&layout=seriescolumn"
)
SHILLER_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500/master/data/data.csv"
# World Bank pink sheet, via the gold-prices dataset. Before 1968 the file
# repeats the official dollar peg, which is not a market price.
GOLD_URL = "https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv"
GOLD_START = "1968-01"
# Monthly CSI 300 closes. Yahoo's chart API only returns a single bar for this index.
CSI_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid=1.000300&klt=103&fqt=1&beg=0&end=20500101"
    "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53"
)


def parse_h15(text: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    rows = list(csv.reader(text.splitlines()))
    header = None
    start = 0
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Time Period":
            header = row
            start = i + 1
            break
    if header is None:
        raise RuntimeError("H.15 file has no Time Period header")
    try:
        fed_col = header.index("RIFSPFF_N.M")
        yield_col = header.index("RIFLGFCY10_N.M")
    except ValueError as exc:
        raise RuntimeError("H.15 layout changed; fed funds or 10-year column is missing") from exc

    def take(column: int) -> list[tuple[str, float]]:
        parsed: list[tuple[str, float]] = []
        for row in rows[start:]:
            if not row or len(row[0]) < 7 or not row[0][:4].isdigit():
                continue
            if len(row) <= column or row[column] in {"", "ND"}:
                continue
            parsed.append((row[0][:7], round(float(row[column]), 2)))
        return dedupe(parsed)

    return take(fed_col), take(yield_col)


def load_h15(offline: bool) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    raw = fetch(H15_MONTHLY_URL, "h15_monthly.csv", offline)
    return parse_h15(raw.decode("utf-8-sig", errors="replace"))


def parse_yahoo(raw: bytes) -> list[tuple[str, float]]:
    payload = json.loads(raw)
    result = payload["chart"]["result"][0]
    granularity = result.get("meta", {}).get("dataGranularity")
    if granularity and granularity != "1mo":
        raise ValueError(f"expected monthly bars, got {granularity}")
    timestamps = result.get("timestamp") or []
    indicators = result["indicators"]
    closes = None
    if indicators.get("adjclose"):
        closes = indicators["adjclose"][0].get("adjclose")
    if not closes:
        closes = indicators["quote"][0]["close"]
    rows: dict[str, float] = {}
    for stamp, close in zip(timestamps, closes):
        if close is None:
            continue
        dt = datetime.fromtimestamp(stamp, tz=UTC)
        rows[f"{dt.year:04d}-{dt.month:02d}"] = float(close)
    if len(rows) < 24:
        raise ValueError("yahoo series too short")
    return sorted(rows.items())


def parse_stooq(raw: bytes) -> list[tuple[str, float]]:
    text = raw.decode("utf-8", errors="replace")
    rows: dict[str, float] = {}
    for i, line in enumerate(text.splitlines()):
        if i == 0 or not line.strip() or line.lower().startswith("no data"):
            continue
        parts = line.split(",")
        if len(parts) < 5 or parts[0].lower() == "date":
            continue
        try:
            rows[parts[0][:7]] = float(parts[4])
        except ValueError:
            continue
    if len(rows) < 24:
        raise ValueError("stooq series too short")
    return sorted(rows.items())


def load_equity(key: str, yahoo: str, stooq: str, offline: bool) -> tuple[list[tuple[str, float]], str]:
    yahoo_url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(yahoo)}?interval=1mo&period1=0&period2=2000000000"
        "&includeAdjustedClose=true"
    )
    errors = []
    try:
        raw = fetch(yahoo_url, f"yahoo_{key}.json", offline)
        return parse_yahoo(raw), "Yahoo Finance"
    except Exception as exc:  # noqa: BLE001 - try the next public source
        errors.append(f"yahoo: {exc}")
    stooq_url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(stooq)}&i=m"
    try:
        raw = fetch(stooq_url, f"stooq_{key}.csv", offline)
        return parse_stooq(raw), "Stooq"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"stooq: {exc}")
    raise RuntimeError(f"{key} unavailable ({'; '.join(errors)})")


def parse_shiller(raw: bytes) -> list[tuple[str, float]]:
    text = raw.decode("utf-8-sig", errors="replace")
    rows: list[tuple[str, float]] = []
    for row in csv.DictReader(text.splitlines()):
        raw_price = (row.get("SP500") or "").strip()
        if not raw_price:
            continue
        price = float(raw_price)
        if price <= 0:
            continue
        rows.append((row["Date"][:7], price))
    return dedupe(rows)


def parse_gold(raw: bytes) -> list[tuple[str, float]]:
    text = raw.decode("utf-8-sig", errors="replace")
    rows: list[tuple[str, float]] = []
    for row in csv.DictReader(text.splitlines()):
        month = (row.get("Date") or "")[:7]
        raw_price = (row.get("Price") or "").strip()
        if len(month) < 7 or not raw_price or month < GOLD_START:
            continue
        price = float(raw_price)
        if price <= 0:
            continue
        rows.append((month, price))
    return dedupe(rows)


def load_gold(offline: bool) -> tuple[list[tuple[str, float]], str]:
    raw = fetch(GOLD_URL, "gold_monthly.csv", offline)
    pairs = parse_gold(raw)
    if len(pairs) < 24:
        raise RuntimeError("gold series is too short")
    return pairs, "黄金现货月平均（美元/盎司），1968 年起；更早是官价，不纳入"


def parse_csi(raw: bytes) -> list[tuple[str, float]]:
    payload = json.loads(raw.decode("utf-8"))
    klines = ((payload.get("data") or {}).get("klines")) or []
    rows: list[tuple[str, float]] = []
    for item in klines:
        date, price = str(item).split(",")[:2]
        month = date[:7]
        if len(month) < 7 or not month[:4].isdigit():
            continue
        value = float(price)
        if value <= 0:
            continue
        rows.append((month, value))
    return dedupe(rows)


def load_csi(offline: bool) -> tuple[list[tuple[str, float]], str]:
    raw = fetch(CSI_URL, "csi300.json", offline)
    pairs = parse_csi(raw)
    if len(pairs) < 24:
        raise RuntimeError("CSI 300 series is too short")
    return pairs, "沪深 300 月收盘，2005 年起，价格指数、不含分红"


def describe_now(phases: list[dict]) -> dict:
    """Plain-language label for the latest phase, using the previous hike or cut."""
    current = phases[-1]
    previous = None
    for phase in reversed(phases[:-1]):
        if phase["direction"] in ("hike", "cut"):
            previous = phase
            break
    ongoing = bool(current.get("ongoing"))
    lo = float(current["rate_low"])
    hi = float(current["rate_high"])
    band = f"{lo:.2f}%" if abs(hi - lo) < 0.005 else f"{lo:.2f}%–{hi:.2f}%"

    def cn(month: str) -> str:
        return month.replace("-", ".")

    if current["direction"] == "hike":
        title = "加息进行中" if ongoing else "加息"
        detail = (
            f"这一段从 {cn(current['start'])} 开始，"
            f"利率从 {current['rate_start']:.2f}% 升到 {current['rate_high']:.2f}%，"
            f"已经 {current['months']} 个月。"
        )
    elif current["direction"] == "cut":
        title = "降息进行中" if ongoing else "降息"
        detail = (
            f"这一段从 {cn(current['start'])} 开始，"
            f"利率从 {current['rate_start']:.2f}% 降到 {current['rate_low']:.2f}%，"
            f"已经 {current['months']} 个月。"
        )
    elif previous and previous["direction"] == "cut":
        title = "降息之后的暂停" if ongoing else "降息之后的暂停（已结束）"
        detail = (
            f"上一轮降息是 {cn(previous['start'])} 到 {cn(previous['end'])}，"
            f"利率从 {previous['rate_start']:.2f}% 降到 {previous['rate_end']:.2f}%。"
            f"从 {cn(current['start'])} 起停在 {band}，已经 {current['months']} 个月"
            + ("，还没有开始新的加息。" if ongoing else "。")
        )
    elif previous and previous["direction"] == "hike":
        title = "加息之后的高位暂停" if ongoing else "加息之后的高位暂停（已结束）"
        detail = (
            f"上一轮加息是 {cn(previous['start'])} 到 {cn(previous['end'])}，"
            f"利率从 {previous['rate_start']:.2f}% 升到 {previous['rate_end']:.2f}%。"
            f"从 {cn(current['start'])} 起停在 {band}，已经 {current['months']} 个月"
            + ("，还没有开始降息。" if ongoing else "。")
        )
    else:
        title = "持稳" + (" · 进行中" if ongoing else "")
        detail = f"从 {cn(current['start'])} 起利率停在 {band}，已经 {current['months']} 个月。"
    return {
        "title": title,
        "detail": detail,
        "direction": current["direction"],
        "ongoing": ongoing,
    }


def load_spx(offline: bool) -> tuple[list[tuple[str, float]], str]:
    """S&P composite monthly average back to the 19th century, then Yahoo's latest months.

    Yahoo's chart API only returns the S&P 500 from 1985. Shiller's monthly average
    matches it closely (overlap median ratio about 1.01) and covers the early cycles.
    """
    yahoo, _source = load_equity("spx", "^GSPC", "^spx", offline)
    try:
        raw = fetch(SHILLER_URL, "shiller_spx.csv", offline)
        shiller = parse_shiller(raw)
        if len(shiller) < 200:
            raise RuntimeError("Shiller S&P series is too short")
    except Exception as exc:  # noqa: BLE001
        print(f"shiller unavailable, S&P starts in 1985: {exc}")
        return yahoo, "Yahoo 月收盘，1985 年以后"
    last = shiller[-1][0]
    extended = shiller + [(month, price) for month, price in yahoo if month > last]
    return extended, "席勒月平均，末尾用 Yahoo 月收盘接上"


def pack(pairs: list[tuple[str, float]], digits: int = 4) -> list[dict]:
    return [{"d": month, "v": round(value, digits)} for month, value in pairs]


def coverage(pairs: list[tuple[str, float]]) -> dict:
    if not pairs:
        return {"start": None, "end": None, "n": 0}
    return {"start": pairs[0][0], "end": pairs[-1][0], "n": len(pairs)}


def write_csv(path: Path, phases: list[dict], asset_ids: list[str]) -> None:
    fields = [
        "id",
        "direction",
        "start",
        "end",
        "months",
        "ongoing",
        "censored",
        "rate_start",
        "rate_end",
        "rate_high",
        "rate_low",
    ]
    for asset in asset_ids:
        fields += [f"{asset}_cum", f"{asset}_ann", f"{asset}_mdd"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for phase in phases:
            row = {
                "id": phase["id"],
                "direction": phase["direction"],
                "start": phase["start"],
                "end": phase["end"],
                "months": phase["months"],
                "ongoing": phase["ongoing"],
                "censored": phase["censored"],
                "rate_start": phase["rate_start"],
                "rate_end": phase["rate_end"],
                "rate_high": phase["rate_high"],
                "rate_low": phase["rate_low"],
            }
            for asset in asset_ids:
                stats = (phase.get("returns") or {}).get(asset) or {}
                row[f"{asset}_cum"] = stats.get("cum")
                row[f"{asset}_ann"] = stats.get("ann")
                row[f"{asset}_mdd"] = stats.get("mdd")
            writer.writerow(row)


def self_test() -> None:
    modified, _convexity = par_bond_mod_duration_convexity(0.05)
    if not 7.5 < modified < 8.0:
        raise AssertionError(f"duration {modified}")

    flat = [(m, 5.0) for m in enumerate_months("2000-01", 13)]
    index = bond_index(flat)
    if not 104.5 < index[-1][1] < 105.8:
        raise AssertionError(f"flat bond index {index[-1][1]}")

    jumped = bond_index([("2000-01", 5.0), ("2000-02", 6.0)])
    jump_ret = jumped[-1][1] / jumped[0][1] - 1.0
    if jump_ret > -0.04:
        raise AssertionError(f"yield jump should hurt the bond, got {jump_ret}")

    stats = phase_return({"2000-01": 100.0, "2002-01": 121.0}, "2000-01", "2002-01")
    if stats is None or abs(stats["cum"] - 0.21) > 1e-9 or abs(stats["ann"] - 0.10) > 1e-9:
        raise AssertionError(f"return math {stats}")

    vals = (
        [1, 1, 1, 1.5, 2.5, 3.5, 4.5, 5.2]
        + [5.2] * 12
        + [4.6, 3.6, 2.4, 1.2, 0.4]
        + [0.4] * 12
        + [1.0, 1.6]
    )
    phases = detect_phases(list(zip(enumerate_months("2000-01", len(vals)), vals)))
    dirs = [p["direction"] for p in phases]
    if dirs != ["hike", "hold", "cut", "hold", "hike"]:
        raise AssertionError(f"synthetic phases {dirs} {phases}")
    if not phases[-1]["ongoing"] or phases[0]["ongoing"]:
        raise AssertionError("ongoing flag")
    if phases[1]["months"] < HOLD_MIN_MONTHS or phases[3]["months"] < HOLD_MIN_MONTHS:
        raise AssertionError("plateau was not split into a hold")
    if describe_now(phases)["title"] != "加息进行中":
        raise AssertionError(describe_now(phases))
    opened = next_confirmation(list(zip(enumerate_months("2000-01", len(vals)), vals)))
    if opened["direction"] != "cut" or abs(opened["trigger"] - 0.6) > 1e-9 or abs(opened["gap_pp"] - 1.0) > 1e-9:
        raise AssertionError(f"synthetic confirmation {opened}")
    paused = next_confirmation([
        ("2024-09", 5.13),
        ("2024-11", 4.50),
        ("2025-11", 3.88),
        ("2026-06", 3.63),
        ("2026-10", 3.88),
    ])
    if paused["direction"] != "hike" or abs(paused["trigger"] - 4.63) > 1e-9 or abs(paused["gap_pp"] - 0.75) > 1e-9:
        raise AssertionError(f"pause confirmation {paused}")
    pause = describe_now([
        {
            "direction": "cut", "start": "2024-09", "end": "2025-11", "months": 14,
            "rate_start": 5.13, "rate_end": 3.88, "rate_high": 5.13, "rate_low": 3.88,
        },
        {
            "direction": "hold", "start": "2025-11", "end": "2026-10", "months": 11,
            "rate_start": 3.88, "rate_end": 3.88, "rate_high": 3.88, "rate_low": 3.63,
            "ongoing": True,
        },
    ])
    if pause["title"] != "降息之后的暂停" or "还没有开始新的加息" not in pause["detail"]:
        raise AssertionError(pause)
    print("self-test passed")


def build(offline: bool) -> None:
    warnings: list[str] = []
    fed, gs10 = load_h15(offline)
    if len(fed) < 24:
        raise RuntimeError("federal funds series is too short")
    bond = bond_index(gs10)

    series: dict[str, list[tuple[str, float]]] = {"bond": bond}
    sources = {
        "fed": "美联储 H.15 有效联邦基金利率，月平均",
        "gs10": "美联储 H.15 10 年期国债收益率，月平均",
        "bond": "由 10 年期收益率估算的平价债券总回报",
    }
    for key, _label, yahoo, stooq in ASSETS:
        try:
            if key == "spx":
                pairs, source = load_spx(offline)
                earliest = add_months(fed[0][0], -1)
                pairs = [(month, price) for month, price in pairs if month >= earliest]
            else:
                pairs, source = load_equity(key, yahoo, stooq, offline)
            series[key] = pairs
            sources[key] = source
            print(f"{key}: {pairs[0][0]} .. {pairs[-1][0]}  n={len(pairs)}  {source}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{key} 没有取到行情：{exc}")
            print(f"WARNING {key}: {exc}")

    try:
        pairs, source = load_csi(offline)
        series["csi"] = pairs
        sources["csi"] = source
        print(f"csi: {pairs[0][0]} .. {pairs[-1][0]}  n={len(pairs)}  {source}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"沪深300没有取到行情：{exc}")
        print(f"WARNING csi: {exc}")

    try:
        pairs, source = load_gold(offline)
        series["gold"] = pairs
        sources["gold"] = source
        print(f"gold: {pairs[0][0]} .. {pairs[-1][0]}  n={len(pairs)}  {source}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"黄金没有取到行情：{exc}")
        print(f"WARNING gold: {exc}")

    maps = {key: dict(pairs) for key, pairs in series.items()}
    phases = detect_phases(fed)
    if not phases:
        raise RuntimeError("no phases detected")
    if phases[-1]["end"] != fed[-1][0]:
        warnings.append(
            f"最后一段结束于 {phases[-1]['end']}，联邦基金利率样本结束于 {fed[-1][0]}"
        )

    asset_ids = [key for key, *_rest in ASSETS if key in maps]
    if "csi" in maps:
        asset_ids.append("csi")
    if "gold" in maps:
        asset_ids.append("gold")
    if "bond" in maps:
        asset_ids.append("bond")
    for phase in phases:
        phase["returns"] = {}
        for asset in asset_ids:
            stats = phase_return(maps[asset], phase["start"], phase["end"])
            if stats is not None and phase["months"] < MIN_ANN_MONTHS:
                stats["ann"] = None
            if stats is not None:
                phase["returns"][asset] = stats

    def med(direction: str, asset: str, field: str) -> float | None:
        vals = []
        for phase in phases:
            if phase["direction"] != direction or phase["ongoing"] or phase["censored"]:
                continue
            stats = phase["returns"].get(asset)
            if stats and stats.get(field) is not None:
                vals.append(stats[field])
        value = median(vals)
        return None if value is None else round(value, 6)

    def count(direction: str, asset: str = "spx") -> int:
        n = 0
        for phase in phases:
            if phase["direction"] != direction or phase["ongoing"] or phase["censored"]:
                continue
            if asset in phase["returns"]:
                n += 1
        return n

    current = phases[-1]
    now = describe_now(phases)
    now["next"] = next_confirmation(fed)
    summary = {
        "as_of": fed[-1][0],
        "fed_latest": fed[-1][1],
        "current_id": current["id"],
        "hike_n": count("hike"),
        "cut_n": count("cut"),
        "hold_n": count("hold"),
        "hike_spx_ann_median": med("hike", "spx", "ann"),
        "cut_spx_ann_median": med("cut", "spx", "ann"),
        "hike_spx_cum_median": med("hike", "spx", "cum"),
        "cut_spx_cum_median": med("cut", "spx", "cum"),
        "hike_bond_ann_median": med("hike", "bond", "ann"),
        "cut_bond_ann_median": med("cut", "bond", "ann"),
        "hike_gold_ann_median": med("hike", "gold", "ann"),
        "cut_gold_ann_median": med("cut", "gold", "ann"),
        "hike_csi_ann_median": med("hike", "csi", "ann"),
        "cut_csi_ann_median": med("cut", "csi", "ann"),
        "hike_csi_n": count("hike", "csi"),
        "cut_csi_n": count("cut", "csi"),
        "now": now,
    }

    nxt = now.get("next") or {}
    gap_txt = ""
    if nxt.get("direction") and nxt.get("trigger") is not None:
        gap_txt = f"  next {nxt['direction']} at {nxt['trigger']:.2f}  gap {nxt['gap_pp']:.2f}pp"
    print(f"NOW      {summary['now']['title']}  {summary['now']['detail']}{gap_txt}")
    print(f"FEDFUNDS {fed[0][0]} .. {fed[-1][0]}  latest {fed[-1][1]:.2f}")
    print(f"GS10     {gs10[0][0]} .. {gs10[-1][0]}")
    for i, phase in enumerate(phases):
        spx = phase["returns"].get("spx")
        spx_txt = "" if not spx else f"  spx {spx['cum'] * 100:+.1f}%"
        print(
            f"{i:02d} {phase['direction']:4} {phase['start']}..{phase['end']} "
            f"{phase['months']:3d}m {phase['rate_low']:.2f}-{phase['rate_high']:.2f}"
            f"{' ONG' if phase['ongoing'] else ''}{' CENSOR' if phase['censored'] else ''}{spx_txt}"
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "params": {
            "reversal": REVERSAL_PP,
            "band": FLAT_PP,
            "hold_months": HOLD_MIN_MONTHS,
        },
        "sources": sources,
        "warnings": warnings,
        "coverage": {
            "fed": coverage(fed),
            "gs10": coverage(gs10),
            **{key: coverage(series[key]) for key in asset_ids},
        },
        "summary": summary,
        "series": {
            "fed": pack(fed, 4),
            **{key: pack(series[key], 4) for key in asset_ids},
        },
        "phases": phases,
    }

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(data_dir / "cycles.csv", phases, asset_ids)

    template = (WEB / "template.html").read_text(encoding="utf-8")
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = template.replace("__EMBEDDED_DATA__", embedded)
    (WEB / "index.html").write_text(html, encoding="utf-8")
    print(f"wrote {WEB / 'index.html'}")
    print(f"wrote {data_dir / 'cycles.csv'}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    self_test()
    if "--self-test" in sys.argv:
        return
    build("--offline" in sys.argv)


if __name__ == "__main__":
    main()
