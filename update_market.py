import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone


API_KEY = os.environ["TWELVE_DATA_API_KEY"]
SYMBOL = "EUR/USD"


def fetch_m15():
    params = {
        "symbol": SYMBOL,
        "interval": "15min",
        "outputsize": 1000,
        "timezone": "UTC",
        "order": "asc",
        "apikey": API_KEY,
    }

    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)

    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data API error"))

    return data["values"]


def normalize_completed(rows):
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    current_m15_start = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0
    )

    result = []

    for row in rows:
        dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")

        # Не берём текущую незавершённую M15 свечу
        if dt >= current_m15_start:
            continue

        result.append({
            "datetime_utc": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    return result


def aggregate(candles, hours):
    groups = {}

    for candle in candles:
        dt = datetime.strptime(
            candle["datetime_utc"],
            "%Y-%m-%d %H:%M:%S"
        )

        bucket_hour = (dt.hour // hours) * hours
        bucket = dt.replace(
            hour=bucket_hour,
            minute=0,
            second=0,
            microsecond=0
        )

        key = bucket.strftime("%Y-%m-%d %H:%M:%S")
        groups.setdefault(key, []).append(candle)

    result = []

    for key, group in sorted(groups.items()):
        expected = hours * 4

        # Добавляем только полностью сформированную H1/H4 свечу
        if len(group) != expected:
            continue

        result.append({
            "datetime_utc": key,
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
        })

    return result


m15 = normalize_completed(fetch_m15())

if not m15:
    raise RuntimeError("No completed M15 candles received")

h1 = aggregate(m15, 1)
h4 = aggregate(m15, 4)

output = {
    "symbol": SYMBOL,
    "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    "coverage": {
        "source_completed_m15_count": len(m15),
        "first_completed_m15_utc": m15[0]["datetime_utc"],
        "last_completed_m15_utc": m15[-1]["datetime_utc"],
        "returned_m15_count": min(len(m15), 192),
        "returned_h1_count": min(len(h1), 120),
        "returned_h4_count": min(len(h4), 60),
    },
    "latest_completed_m15_close": m15[-1]["close"],
    "last_m15": m15[-192:],
    "last_h1": h1[-120:],
    "last_h4": h4[-60:],
}

with open("market.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print("market.json updated successfully")
