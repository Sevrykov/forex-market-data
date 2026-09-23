import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


API_KEY = os.environ["TWELVE_DATA_API_KEY"]

SYMBOLS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
]


def parse_utc(value):
    return datetime.strptime(
        value,
        "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)


def fetch_m15(symbol):
    params = {
        "symbol": symbol,
        "interval": "15min",
        "outputsize": 1000,
        "timezone": "UTC",
        "order": "asc",
        "apikey": API_KEY,
    }

    url = (
        "https://api.twelvedata.com/time_series?"
        + urllib.parse.urlencode(params)
    )

    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)

    if data.get("status") == "error":
        raise RuntimeError(
            f"{symbol}: "
            f"{data.get('message', 'Twelve Data API error')}"
        )

    if "values" not in data:
        raise RuntimeError(
            f"{symbol}: response does not contain values"
        )

    return data["values"]


def normalize_completed(rows):
    now = datetime.now(timezone.utc)

    current_m15_start = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0,
    )

    result = []

    for row in rows:
        dt = parse_utc(row["datetime"])

        # Не используем текущую незакрытую M15-свечу.
        if dt >= current_m15_start:
            continue

        result.append({
            "datetime_utc": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    result.sort(key=lambda x: x["datetime_utc"])
    return result


def aggregate(candles, hours):
    groups = {}

    for candle in candles:
        dt = parse_utc(candle["datetime_utc"])

        bucket_hour = (dt.hour // hours) * hours

        bucket = dt.replace(
            hour=bucket_hour,
            minute=0,
            second=0,
            microsecond=0,
        )

        groups.setdefault(bucket, []).append(candle)

    result = []
    expected_count = hours * 4

    for bucket in sorted(groups):
        group = sorted(
            groups[bucket],
            key=lambda x: x["datetime_utc"],
        )

        # Проверяем, что внутри H1/H4 присутствуют
        # все необходимые M15-свечи без пропусков.
        expected_times = [
            bucket + timedelta(minutes=15 * i)
            for i in range(expected_count)
        ]

        actual_times = [
            parse_utc(candle["datetime_utc"])
            for candle in group
        ]

        if actual_times != expected_times:
            continue

        result.append({
            "datetime_utc":
                bucket.strftime("%Y-%m-%d %H:%M:%S"),
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
        })

    return result


def build_pair(symbol):
    m15 = normalize_completed(fetch_m15(symbol))

    if not m15:
        raise RuntimeError(
            f"{symbol}: no completed M15 candles received"
        )

    h1 = aggregate(m15, 1)
    h4 = aggregate(m15, 4)

    if not h1 or not h4:
        raise RuntimeError(
            f"{symbol}: could not build H1/H4 candles"
        )

    return {
        "symbol": symbol,

        "fetched_at_utc":
            datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "coverage": {
            "source_completed_m15_count": len(m15),

            "first_completed_m15_utc":
                m15[0]["datetime_utc"],

            "last_completed_m15_utc":
                m15[-1]["datetime_utc"],

            "returned_m15_count":
                min(len(m15), 192),

            "returned_h1_count":
                min(len(h1), 120),

            "returned_h4_count":
                min(len(h4), 60),
        },

        "latest_completed_m15_close":
            m15[-1]["close"],

        "last_m15":
            m15[-192:],

        "last_h1":
            h1[-120:],

        "last_h4":
            h4[-60:],
    }


pairs = {}

for symbol in SYMBOLS:
    print(f"Fetching {symbol}...")
    pairs[symbol] = build_pair(symbol)


output = {
    "generated_at_utc":
        datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

    "pair_count": len(pairs),

    "pairs": pairs,
}


with open(
    "market.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        output,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"market.json updated successfully "
    f"for {len(pairs)} pairs"
)
