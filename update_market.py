import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ==================================================
# НАСТРОЙКИ
# ==================================================

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

INTERVAL = "15min"

# Берём увеличенную историю M15.
# После удаления свечей закрытого Forex-рынка
# данных должно хватить для построения:
# 192 M15 / 120 H1 / 60 H4.
OUTPUTSIZE = 2000

TARGET_M15 = 192
TARGET_H1 = 120
TARGET_H4 = 60

NEW_YORK = ZoneInfo("America/New_York")


# ==================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==================================================

def parse_utc(value):
    return datetime.strptime(
        value,
        "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)


def dt_to_string(dt):
    return dt.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def is_forex_open(dt_utc):
    """
    Проверяет, находится ли время свечи внутри
    стандартной торговой недели Forex.

    Используется America/New_York,
    поэтому переход между летним и зимним временем
    учитывается автоматически.

    Рабочая неделя:

    воскресенье 17:00 New York
    ->
    пятница 17:00 New York
    """

    ny = dt_utc.astimezone(NEW_YORK)

    weekday = ny.weekday()

    # Python:
    # Monday    = 0
    # Tuesday   = 1
    # Wednesday = 2
    # Thursday  = 3
    # Friday    = 4
    # Saturday  = 5
    # Sunday    = 6

    # Суббота полностью закрыта.
    if weekday == 5:
        return False

    # В воскресенье Forex открывается в 17:00 NY.
    if weekday == 6:
        return ny.hour >= 17

    # В пятницу Forex закрывается в 17:00 NY.
    if weekday == 4:
        return ny.hour < 17

    # Понедельник–четверг.
    return True


def valid_ohlc(candle):
    """
    Базовая проверка математической корректности OHLC.
    """

    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    if h < l:
        return False

    if h < max(o, c):
        return False

    if l > min(o, c):
        return False

    return True


# ==================================================
# ПОЛУЧЕНИЕ ДАННЫХ TWELVE DATA
# ==================================================

def fetch_all_m15():
    """
    Получает M15 сразу по всем валютным парам
    одним batch-запросом.
    """

    params = {
        "symbol": ",".join(SYMBOLS),
        "interval": INTERVAL,
        "outputsize": OUTPUTSIZE,
        "timezone": "UTC",
        "order": "asc",
        "apikey": API_KEY,
    }

    url = (
        "https://api.twelvedata.com/time_series?"
        + urllib.parse.urlencode(params)
    )

    data = None

    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "forex-market-data-github-action"
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=90
            ) as response:
                data = json.load(response)

            break

        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                print(
                    "Twelve Data rate limit reached. "
                    "Waiting 65 seconds before retry..."
                )

                time.sleep(65)
                continue

            raise

    if data is None:
        raise RuntimeError(
            "Twelve Data response was not received"
        )

    if not isinstance(data, dict):
        raise RuntimeError(
            "Unexpected Twelve Data response format"
        )

    if data.get("status") == "error":
        raise RuntimeError(
            data.get(
                "message",
                "Twelve Data API error"
            )
        )

    result = {}

    for symbol in SYMBOLS:
        item = data.get(symbol)

        if item is None:
            raise RuntimeError(
                f"{symbol}: missing from batch response"
            )

        if isinstance(item, dict):

            if item.get("status") == "error":
                raise RuntimeError(
                    f"{symbol}: "
                    f"{item.get('message', 'API error')}"
                )

            rows = item.get("values")

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


# ==================================================
# НОРМАЛИЗАЦИЯ И ФИЛЬТР M15
# ==================================================

def normalize_and_filter(rows):
    """
    1. Удаляет текущую незавершённую M15.
    2. Удаляет свечи закрытого Forex-рынка.
    3. Проверяет OHLC.
    4. Удаляет дубликаты.
    5. Сортирует историю.
    """

    now = datetime.now(timezone.utc)

    current_m15_start = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0,
    )

    raw_count = len(rows)

    closed_market_count = 0
    unfinished_count = 0
    invalid_ohlc_count = 0

    result = []

    for row in rows:
        dt = parse_utc(
            row["datetime"]
        )

        # Не используем текущую незакрытую свечу.
        if dt >= current_m15_start:
            unfinished_count += 1
            continue

        # Удаляем свечи, относящиеся
        # к закрытому Forex-рынку.
        if not is_forex_open(dt):
            closed_market_count += 1
            continue

        candle = {
            "datetime_utc":
                row["datetime"],

            "open":
                float(row["open"]),

            "high":
                float(row["high"]),

            "low":
                float(row["low"]),

            "close":
                float(row["close"]),
        }

        if not valid_ohlc(candle):
            invalid_ohlc_count += 1
            continue

        result.append(candle)

    result.sort(
        key=lambda x: x["datetime_utc"]
    )

    # Удаление возможных дубликатов timestamp.
    unique = {}

    for candle in result:
        unique[
            candle["datetime_utc"]
        ] = candle

    duplicate_count = (
        len(result) - len(unique)
    )

    result = [
        unique[key]
        for key in sorted(unique)
    ]

    diagnostics = {
        "raw_m15_count":
            raw_count,

        "filtered_closed_market_m15_count":
            closed_market_count,

        "filtered_unfinished_m15_count":
            unfinished_count,

        "filtered_invalid_ohlc_count":
            invalid_ohlc_count,

        "duplicate_m15_count":
            duplicate_count,

        "valid_completed_m15_count":
            len(result),
    }

    return result, diagnostics


# ==================================================
# ПОСТРОЕНИЕ H1 И H4
# ==================================================

def aggregate(candles, hours):
    """
    Строит полноценные H1/H4 из M15.

    H1 = 4 последовательных M15.
    H4 = 16 последовательных M15.

    Если внутри старшей свечи отсутствует
    хотя бы одна M15, такая H1/H4 не создаётся.
    """

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

        # H1 должна иметь ровно 4 M15.
        # H4 должна иметь ровно 16 M15.
        if len(group) != expected_count:
            continue

        expected_times = [
            bucket + timedelta(
                minutes=15 * i
            )
            for i in range(
                expected_count
            )
        ]

        actual_times = [
            parse_utc(
                candle["datetime_utc"]
            )
            for candle in group
        ]

        # Если последовательность M15 нарушена,
        # старшую свечу не создаём.
        if actual_times != expected_times:
            continue

        aggregated = {
            "datetime_utc":
                dt_to_string(bucket),

            "open":
                group[0]["open"],

            "high":
                max(
                    candle["high"]
                    for candle in group
                ),

            "low":
                min(
                    candle["low"]
                    for candle in group
                ),

            "close":
                group[-1]["close"],
        }

        if valid_ohlc(aggregated):
            result.append(
                aggregated
            )

    return result


# ==================================================
# ПРОВЕРКА ДОСТАТОЧНОСТИ ИСТОРИИ
# ==================================================

def validate_counts(
    symbol,
    m15,
    h1,
    h4,
):
    problems = []

    if len(m15) < TARGET_M15:
        problems.append(
            f"M15={len(m15)}, "
            f"required >= {TARGET_M15}"
        )

    if len(h1) < TARGET_H1:
        problems.append(
            f"H1={len(h1)}, "
            f"required >= {TARGET_H1}"
        )

    if len(h4) < TARGET_H4:
        problems.append(
            f"H4={len(h4)}, "
            f"required >= {TARGET_H4}"
        )

    if problems:
        raise RuntimeError(
            f"{symbol}: "
            "insufficient valid history: "
            + "; ".join(problems)
        )


# ==================================================
# СОЗДАНИЕ ДАННЫХ ОДНОЙ ПАРЫ
# ==================================================

def build_pair(symbol, rows):

    m15, diagnostics = (
        normalize_and_filter(rows)
    )

    h1 = aggregate(
        m15,
        1
    )

    h4 = aggregate(
        m15,
        4
    )

    validate_counts(
        symbol,
        m15,
        h1,
        h4,
    )

    latest_m15 = m15[-1]

    return {
        "symbol":
            symbol,

        "data_status":
            "OK",

        "fetched_at_utc":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "coverage": {
            **diagnostics,

            "first_valid_m15_utc":
                m15[0][
                    "datetime_utc"
                ],

            "last_completed_m15_utc":
                latest_m15[
                    "datetime_utc"
                ],

            "available_h1_count":
                len(h1),

            "available_h4_count":
                len(h4),

            "returned_m15_count":
                TARGET_M15,

            "returned_h1_count":
                TARGET_H1,

            "returned_h4_count":
                TARGET_H4,
        },

        "latest_completed_m15_close":
            latest_m15["close"],

        "last_m15":
            m15[-TARGET_M15:],

        "last_h1":
            h1[-TARGET_H1:],

        "last_h4":
            h4[-TARGET_H4:],
    }


# ==================================================
# ОСНОВНОЙ ПРОЦЕСС
# ==================================================

print(
    "Fetching Forex data for "
    f"{len(SYMBOLS)} pairs..."
)

batch = fetch_all_m15()

pairs = {}

for symbol in SYMBOLS:

    print(
        f"Filtering and building "
        f"{symbol}..."
    )

    pairs[symbol] = build_pair(
        symbol,
        batch[symbol]
    )

    coverage = (
        pairs[symbol]["coverage"]
    )

    print(
        f"{symbol}: "
        f"raw="
        f"{coverage['raw_m15_count']}, "
        f"closed_market_filtered="
        f"{coverage['filtered_closed_market_m15_count']}, "
        f"invalid_ohlc="
        f"{coverage['filtered_invalid_ohlc_count']}, "
        f"duplicates="
        f"{coverage['duplicate_m15_count']}, "
        f"valid_m15="
        f"{coverage['valid_completed_m15_count']}, "
        f"H1="
        f"{coverage['available_h1_count']}, "
        f"H4="
        f"{coverage['available_h4_count']}"
    )


# ==================================================
# ФИНАЛЬНЫЙ MARKET.JSON
# ==================================================

output = {
    "generated_at_utc":
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

    "data_status":
        "OK",

    "pair_count":
        len(pairs),

    "market_hours_filter":
        (
            "Standard Forex weekly session: "
            "Sunday 17:00 America/New_York "
            "to Friday 17:00 America/New_York"
        ),

    "source":
        "Twelve Data",

    "source_interval":
        INTERVAL,

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
