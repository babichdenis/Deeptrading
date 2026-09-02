#!/usr/bin/env python3
"""Сохранить информацию об инструментах в PostgreSQL."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

PG = "postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading"
engine = create_engine(PG)

# Данные из скрипта sb_instruments_info.py
INSTRUMENTS = [
    {
        "figi": "BBG008F2T3T2",
        "ticker": "RUAL",
        "name": "Русал",
        "lot": 10,
        "currency": "RUB",
        "decimals": 3,
        "api_trade_available": True,
        "bestprice_order_available": True,
        "limit_order_available": True,
        "market_order_available": True,
        "trading_status": 5,
        "buy_max_lots": 25,
        "buy_max_market_lots": 25,
        "buy_money_amount": 6770.52,
        "sell_max_lots": 7,
        "sell_margin_buy_max_lots": 31,
        "sell_margin_buy_money_amount": 8373.02,
        "sell_margin_sell_max_lots": 38,
        "board_group_id": 57,
        "is_primary": 1,
        "is_traded": 1,
        "history_from": "2015-03-30",
        "listed_from": "2015-03-30",
        "title": "Т+: Акции и ДР - безадрес.",
    },
    {
        "figi": "BBG004S681M2",
        "ticker": "SBER",
        "name": "Сбербанк",
        "lot": 10,
        "currency": "RUB",
        "decimals": 2,
        "api_trade_available": True,
        "bestprice_order_available": True,
        "limit_order_available": True,
        "market_order_available": True,
        "trading_status": 5,
        "buy_max_lots": 15,
        "buy_max_market_lots": 15,
        "buy_money_amount": 6770.52,
        "sell_max_lots": 0,
        "sell_margin_buy_max_lots": 19,
        "sell_margin_buy_money_amount": 8373.02,
        "sell_margin_sell_max_lots": 19,
        "board_group_id": 57,
        "is_primary": 1,
        "is_traded": 1,
        "history_from": "2013-03-25",
        "listed_from": "2013-03-25",
        "title": "Т+: Акции и ДР - безадрес.",
    },
    {
        "figi": "BBG004S683W7",
        "ticker": "GAZP",
        "name": "Газпром",
        "lot": 10,
        "currency": "RUB",
        "decimals": 2,
        "api_trade_available": True,
        "bestprice_order_available": True,
        "limit_order_available": True,
        "market_order_available": True,
        "trading_status": 5,
        "buy_max_lots": 20,
        "buy_max_market_lots": 20,
        "buy_money_amount": 6770.52,
        "sell_max_lots": 0,
        "sell_margin_buy_max_lots": 25,
        "sell_margin_buy_money_amount": 8373.02,
        "sell_margin_sell_max_lots": 25,
        "board_group_id": 57,
        "is_primary": 1,
        "is_traded": 1,
        "history_from": "2014-06-09",
        "listed_from": "2014-06-09",
        "title": "Т+: Акции и ДР - безадрес.",
    },
    {
        "figi": "BBG004S68CP5",
        "ticker": "LKOH",
        "name": "Лукойл",
        "lot": 1,
        "currency": "RUB",
        "decimals": 1,
        "api_trade_available": True,
        "bestprice_order_available": True,
        "limit_order_available": True,
        "market_order_available": True,
        "trading_status": 5,
        "buy_max_lots": 145,
        "buy_max_market_lots": 144,
        "buy_money_amount": 6770.52,
        "sell_max_lots": 14,
        "sell_margin_buy_max_lots": 179,
        "sell_margin_buy_money_amount": 8373.02,
        "sell_margin_sell_max_lots": 193,
        "board_group_id": 57,
        "is_primary": 1,
        "is_traded": 1,
        "history_from": "2013-03-25",
        "listed_from": "2013-03-25",
        "title": "Т+: Акции и ДР - безадрес.",
    },
    {
        "figi": "BBG004S681B4",
        "ticker": "ROSN",
        "name": "Роснефть",
        "lot": 10,
        "currency": "RUB",
        "decimals": 2,
        "api_trade_available": True,
        "bestprice_order_available": True,
        "limit_order_available": True,
        "market_order_available": True,
        "trading_status": 5,
        "buy_max_lots": 9,
        "buy_max_market_lots": 9,
        "buy_money_amount": 6770.52,
        "sell_max_lots": 1,
        "sell_margin_buy_max_lots": 11,
        "sell_margin_buy_money_amount": 8372.72,
        "sell_margin_sell_max_lots": 12,
        "board_group_id": 57,
        "is_primary": 1,
        "is_traded": 1,
        "history_from": "2014-06-09",
        "listed_from": "2014-06-09",
        "title": "Т+: Акции и ДР - безадрес.",
    },
]

with Session(engine) as db:
    # Создать таблицу
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS instrument_info (
            figi TEXT PRIMARY KEY,
            ticker TEXT,
            name TEXT,
            lot INT,
            currency TEXT,
            decimals INT,
            api_trade_available BOOL,
            bestprice_order_available BOOL,
            limit_order_available BOOL,
            market_order_available BOOL,
            trading_status INT,
            buy_max_lots INT,
            buy_max_market_lots INT,
            buy_money_amount FLOAT,
            sell_max_lots INT,
            sell_margin_buy_max_lots INT,
            sell_margin_buy_money_amount FLOAT,
            sell_margin_sell_max_lots INT,
            board_group_id INT,
            is_primary INT,
            is_traded INT,
            history_from DATE,
            listed_from DATE,
            title TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    db.commit()
    print("Table instrument_info created/exists")

    for info in INSTRUMENTS:
        db.execute(text("""
            INSERT INTO instrument_info (figi, ticker, name, lot, currency, decimals,
                api_trade_available, bestprice_order_available, limit_order_available,
                market_order_available, trading_status, buy_max_lots, buy_max_market_lots,
                buy_money_amount, sell_max_lots, sell_margin_buy_max_lots,
                sell_margin_buy_money_amount, sell_margin_sell_max_lots,
                board_group_id, is_primary, is_traded, history_from, listed_from, title, updated_at)
            VALUES (:figi, :ticker, :name, :lot, :currency, :decimals,
                :api_trade_available, :bestprice_order_available, :limit_order_available,
                :market_order_available, :trading_status, :buy_max_lots, :buy_max_market_lots,
                :buy_money_amount, :sell_max_lots, :sell_margin_buy_max_lots,
                :sell_margin_buy_money_amount, :sell_margin_sell_max_lots,
                :board_group_id, :is_primary, :is_traded, :history_from, :listed_from, :title, NOW())
            ON CONFLICT (figi) DO UPDATE SET
                ticker=EXCLUDED.ticker, name=EXCLUDED.name, lot=EXCLUDED.lot,
                currency=EXCLUDED.currency, decimals=EXCLUDED.decimals,
                api_trade_available=EXCLUDED.api_trade_available,
                bestprice_order_available=EXCLUDED.bestprice_order_available,
                limit_order_available=EXCLUDED.limit_order_available,
                market_order_available=EXCLUDED.market_order_available,
                trading_status=EXCLUDED.trading_status,
                buy_max_lots=EXCLUDED.buy_max_lots, buy_max_market_lots=EXCLUDED.buy_max_market_lots,
                buy_money_amount=EXCLUDED.buy_money_amount,
                sell_max_lots=EXCLUDED.sell_max_lots,
                sell_margin_buy_max_lots=EXCLUDED.sell_margin_buy_max_lots,
                sell_margin_buy_money_amount=EXCLUDED.sell_margin_buy_money_amount,
                sell_margin_sell_max_lots=EXCLUDED.sell_margin_sell_max_lots,
                board_group_id=EXCLUDED.board_group_id, is_primary=EXCLUDED.is_primary,
                is_traded=EXCLUDED.is_traded, history_from=EXCLUDED.history_from,
                listed_from=EXCLUDED.listed_from, title=EXCLUDED.title, updated_at=NOW()
        """), info)
        print(f"  Saved {info['ticker']}: lot={info['lot']} buy_max={info['buy_max_lots']} sell_max={info['sell_max_lots']}")

    db.commit()
    print("\nDone! All instruments saved.")

    # Проверка
    rows = db.execute(text("SELECT ticker, lot, buy_max_lots, sell_max_lots, trading_status FROM instrument_info ORDER BY ticker")).fetchall()
    print("\n=== Verification ===")
    for r in rows:
        print(f"  {r[0]}: lot={r[1]} buy_max={r[2]} sell_max={r[3]} status={r[4]}")
