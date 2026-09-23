import json
import os
import time
import urllib.error
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


def fetch_all_m15():
    params = {
        "symbol": ",".join(SYMBOLS),
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

    # Если мы случайно запустили workflow в минуту,
    # когда лимит уже использован, дождёмся его сброса.
    for attempt in range(2):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "forex-market-data-github-action"
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=60
            ) as response:
                data = json.load(response)

            break

        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                print(
                    "Twelve Data rate limit reached. "
                    "Waiting 65 seconds before retry..."
                )
                time.sleep(65)
                continue

            raise

    if data.get("status") == "error":
        raise RuntimeError(
            data.get(
                "message",
                "Twelve Data batch API error"
            )
        )

    result = {}

    for symbol in SYMBOLS:
        item = data.get(symbol)

        if item is None:
            raise RuntimeError(
                f"{symbol}: missing from batch response"
            )

        # Обычный raw batch-ответ:
        # {
        #   "EUR/USD": {
        #       "meta": {...},
        #       "values": [...]
        #   }
        # }
        if isinstance(item, dict):
            if item.get("status") == "error":
                raise RuntimeError(
                    f"{symbol}: "
                    f"{item.get('message', 'API error')}"
                )

            rows = item.get("values")

        # Запасной вариант формата ответа.
        elif isinstance(item, list):
            rows = item

        else:
            rows = None

        if not rows:
            raise RuntimeError(
                f"{symbol}: no values returned"
            )

        result[symbol] = rows

    return result


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

        # Текущую незакрытую M15 не используем.
        if dt >= current_m15_start:
            continue

        result.append({
            "datetime_utc": row["datetime"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    result.sort(
        key=lambda x: x["datetime_utc"]
    )

    return result


def aggregate(candles, hours):
    groups = {}

    for candle in candles:
        dt = parse_utc(
            candle["datetime_utc"]
        )

        bucket_hour = (
            dt.hour // hours
        ) * hours

        bucket = dt.replace(
            hour=bucket_hour,
            minute=0,
            second=0,
            microsecond=0,
        )

        groups.setdefault(
            bucket,
            []
        ).append(candle)

    result = []
    expected_count = hours * 4

    for bucket in sorted(groups):
        group = sorted(
            groups[bucket],
            key=lambda x:
                x["datetime_utc"],
        )

        expected_times = [
            bucket
            + timedelta(minutes=15 * i)
            for i in range(expected_count)
        ]

        actual_times = [
            parse_utc(
                candle["datetime_utc"]
            )
            for candle in group
        ]

        # Не создаём H1/H4 при пропущенных M15.
        if actual_times != expected_times:
            continue

        result.append({
            "datetime_utc":
                bucket.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),

            "open":
                group[0]["open"],

            "high":
                max(
                    x["high"]
                    for x in group
                ),

            "low":
                min(
                    x["low"]
                    for x in group
                ),

            "close":
                group[-1]["close"],
        })

    return result


def build_pair(symbol, rows):
    m15 = normalize_completed(rows)

    if not m15:
        raise RuntimeError(
            f"{symbol}: no completed M15 candles"
        )

    h1 = aggregate(m15, 1)
    h4 = aggregate(m15, 4)

    if not h1:
        raise RuntimeError(
            f"{symbol}: H1 could not be built"
        )

    if not h4:
        raise RuntimeError(
            f"{symbol}: H4 could not be built"
        )

    return {
        "symbol": symbol,

        "fetched_at_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "coverage": {
            "source_completed_m15_count":
                len(m15),

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


print(
    "Fetching 7 Forex pairs "
    "with one Twelve Data batch request..."
)

batch = fetch_all_m15()

pairs = {}

for symbol in SYMBOLS:
    print(f"Building {symbol}...")
    pairs[symbol] = build_pair(
        symbol,
        batch[symbol]
    )


output = {
    "generated_at_utc":
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

    "pair_count":
        len(pairs),

    "pairs":
        pairs,
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
        indent=2,
    )


print(
    "market.json updated successfully "
    f"for {len(pairs)} pairs"
)
