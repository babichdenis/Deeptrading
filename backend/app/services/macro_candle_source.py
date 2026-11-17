import json
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import certifi

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class MacroInstrument:
    engine: str
    market: str
    board: str
    secid: str

    @property
    def series_id(self) -> str:
        return f"MOEX:{self.board}:{self.secid}"

    @property
    def storage_id(self) -> str:
        return "BBG00KDWPPW2" if self == IMOEX else self.series_id


IMOEX = MacroInstrument("stock", "index", "SNDX", "IMOEX")
CNYRUB = MacroInstrument("currency", "selt", "CETS", "CNYRUB_TOM")
INDEX_SERIES = (
    IMOEX,
    *(MacroInstrument("stock", "index", "SNDX", secid)
      for secid in ("IMOEX2", "RTSI", "MOEXOG", "MOEXFN", "MOEXMM", "MOEXEU",
                    "MOEXCN", "MOEXCH", "MOEXTN")),
)


def futures_contract(secid: str) -> MacroInstrument:
    if not secid.isascii() or not secid.isalnum():
        raise ValueError("Expected a concrete MOEX futures contract code")
    return MacroInstrument("futures", "forts", "RFUD", secid)


def _read_iss_json(url):
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    with urlopen(url, timeout=15, context=context) as response:
        return json.load(response)


def read_closed_macro_candles(instrument, start, end, *, read_json=_read_iss_json):
    if any(at.tzinfo is None or at.utcoffset() is None for at in (start, end)) or start >= end:
        raise ValueError("Expected an increasing timezone-aware range")
    path = (
        f"https://iss.moex.com/iss/engines/{instrument.engine}/markets/{instrument.market}"
        f"/boards/{instrument.board}/securities/{instrument.secid}/candles.json"
    )
    query = {
        "from": start.astimezone(MSK).date().isoformat(),
        "till": end.astimezone(MSK).date().isoformat(),
        "interval": 1,
        "iss.meta": "off",
    }
    offset = 0
    bars = []
    last_seen = None
    while True:
        page = read_json(f"{path}?{urlencode(dict(query, start=offset))}")["candles"]
        if not page["data"]:
            return bars
        for values in page["data"]:
            row = dict(zip(page["columns"], values, strict=True))
            at = datetime.fromisoformat(row["begin"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=MSK)
            at = at.astimezone(timezone.utc)
            if at.second or at.microsecond:
                raise ValueError(f"Off-grid {instrument.series_id} candle at {at.isoformat()}")
            if last_seen is not None and at <= last_seen:
                raise ValueError(f"Non-increasing {instrument.series_id} candle at {at.isoformat()}")
            last_seen = at
            if at + timedelta(minutes=1) > end:
                return bars
            if at < start:
                continue
            candle = {key: row[key] for key in ("open", "high", "low", "close")}
            candle.update(ts=at, volume=int(row["volume"] or 0))
            bars.append(candle)
        offset += len(page["data"])
        if offset > 200_000:
            raise ValueError(f"Excessive pagination for {instrument.series_id}")


def macro_instrument_for_storage_id(storage_id: str) -> MacroInstrument | None:
    for instrument in (*INDEX_SERIES, CNYRUB):
        if instrument.storage_id == storage_id:
            return instrument
    if storage_id.startswith("MOEX:RFUD:"):
        return futures_contract(storage_id.removeprefix("MOEX:RFUD:"))
    if storage_id.startswith("MOEX:"):
        raise ValueError(f"Unregistered MOEX candle series {storage_id}")
    return None


def listed_futures_for_collection(assets, *, as_of, read_json=_read_iss_json):
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Timezone-aware futures catalog clock required")
    url = (
        "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities.json"
        "?iss.only=securities&iss.meta=off&securities.columns=SECID,ASSETCODE,LASTTRADEDATE"
    )
    table = read_json(url)["securities"]
    today = as_of.astimezone(MSK).date()
    candidates = {asset: [] for asset in assets}
    for values in table["data"]:
        row = dict(zip(table["columns"], values, strict=True))
        asset, expiration = row["ASSETCODE"], row["LASTTRADEDATE"]
        if asset not in candidates or not expiration:
            continue
        last_trade = date.fromisoformat(expiration)
        if last_trade >= today:
            candidates[asset].append((last_trade, row["SECID"]))
    return {
        asset: tuple(futures_contract(secid) for _, secid in sorted(set(matches))[:2])
        for asset, matches in candidates.items()
    }
