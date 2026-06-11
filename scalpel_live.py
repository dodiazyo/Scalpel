#!/usr/bin/env python3
"""
Scalpel LIVE — paper-trading en tiempo real (SIMULADO, sin dinero real).
=======================================================================
Corre la MISMA lógica del backtester sobre datos en vivo de OKX, en velas de
15m, y registra entradas/salidas SIMULADAS. No envía órdenes reales: es un
forward-test en papel para ver el bot operar sin arriesgar capital.

Estrategia: reversión a la media (Bollinger + filtro ADX) — la mejor en 15m.
Recuerda: el backtest la dejó sub-breakeven (PF ~0.87). Esto es para observar
y validar en vivo, NO para esperar ganancias.

Uso:
    python3 scalpel_live.py --symbols BTC/USDT ETH/USDT
    # alertas opcionales a Telegram:
    export TG_TOKEN="..."; export TG_CHAT="..."
    python3 scalpel_live.py --symbols BTC/USDT ETH/USDT --telegram
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from scalpel import add_indicators

OKX = "https://www.okx.com"


def fetch_live(sym: str, bar: str = "15m", limit: int = 300) -> pd.DataFrame:
    """Trae las últimas `limit` velas en vivo de OKX (la última está en formación)."""
    inst = sym.replace("/", "-") + "-SWAP"
    r = requests.get(f"{OKX}/api/v5/market/candles",
                     params={"instId": inst, "bar": bar, "limit": limit}, timeout=10)
    r.raise_for_status()
    data = r.json().get("data", [])
    cols = ["ts", "open", "high", "low", "close", "volume", "a", "b", "c"]
    df = pd.DataFrame(data, columns=cols)[["ts", "open", "high", "low", "close", "volume"]]
    df = df.astype({"ts": "int64", "open": float, "high": float, "low": float,
                    "close": float, "volume": float})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def tg(msg: str, args):
    if not args.telegram:
        return
    token, chat = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except Exception:
        pass


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Scalpel LIVE — paper trading 15m")
    ap.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    ap.add_argument("--bar", default="15m")
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--bb-period", type=int, default=20)
    ap.add_argument("--bb-std", type=float, default=2.5)
    ap.add_argument("--adx-period", type=int, default=14)
    ap.add_argument("--adx-max", type=float, default=18.0)
    ap.add_argument("--rsi-period", type=int, default=14)
    ap.add_argument("--don-period", type=int, default=20)
    ap.add_argument("--sl-atr-mult", type=float, default=1.5)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--poll", type=int, default=30, help="segundos entre sondeos")
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args()

    notional = args.capital * args.leverage
    balance = args.balance
    pos: dict[str, dict] = {}
    last_ts: dict[str, pd.Timestamp] = {}
    wins = losses = 0

    print("╔══════════════════════════════════════════════╗")
    print("║  Scalpel LIVE · paper trading 15m · SIMULADO  ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"Símbolos: {', '.join(args.symbols)} · balance inicial ${balance:,.2f}")
    print("Reversión a la media (BB+ADX). NO usa dinero real.\n")
    tg("🧪 <b>Scalpel LIVE</b> arrancó (paper trading 15m, sin dinero real)", args)

    while True:
        try:
            for sym in args.symbols:
                df = fetch_live(sym, args.bar)
                d = add_indicators(df, args.bb_period, args.bb_std, args.adx_period,
                                   args.rsi_period, args.don_period)
                if len(d) < 30:
                    continue
                closed = d.iloc[-2]          # última vela CERRADA (la -1 está en formación)
                price = float(d.iloc[-1]["open"])  # precio vivo aprox
                cts = closed["ts"]

                # gestionar posición abierta (con el precio vivo)
                if sym in pos:
                    p = pos[sym]
                    hit_sl = price <= p["sl"] if p["dir"] == "LONG" else price >= p["sl"]
                    hit_tp = price >= p["tp"] if p["dir"] == "LONG" else price <= p["tp"]
                    if hit_sl or hit_tp:
                        exitp = p["sl"] if hit_sl else p["tp"]
                        gan = (exitp - p["entry"]) * p["qty"] if p["dir"] == "LONG" \
                            else (p["entry"] - exitp) * p["qty"]
                        gan -= notional * args.fee * 2
                        balance += gan
                        if gan >= 0: wins += 1
                        else: losses += 1
                        motivo = "TP ✅" if hit_tp else "SL ❌"
                        msg = (f"CIERRE {sym} {p['dir']} {motivo} | "
                               f"PnL ${gan:+.2f} | balance ${balance:,.2f}")
                        log(msg); tg(f"📉 {msg}", args)
                        del pos[sym]
                    continue

                # ¿vela nueva cerrada? solo evaluamos entrada una vez por vela
                if last_ts.get(sym) == cts:
                    continue
                last_ts[sym] = cts

                if pd.isna(closed["bb_dn"]) or pd.isna(closed["adx"]):
                    continue
                if closed["adx"] >= args.adx_max:
                    continue
                long_sig = closed["close"] <= closed["bb_dn"]
                short_sig = closed["close"] >= closed["bb_up"]
                if not (long_sig or short_sig):
                    continue

                direction = "LONG" if long_sig else "SHORT"
                entry = price
                qty = notional / entry
                atr = float(closed["atr"])
                sl = entry - args.sl_atr_mult * atr if direction == "LONG" \
                    else entry + args.sl_atr_mult * atr
                tp = float(closed["sma"])
                pos[sym] = {"dir": direction, "entry": entry, "qty": qty, "sl": sl, "tp": tp}
                msg = (f"ENTRADA {sym} {direction} @ ${entry:,.4f} | "
                       f"SL ${sl:,.4f} | TP ${tp:,.4f} | RSI {closed['rsi']:.0f}")
                log(msg); tg(f"🟢 {msg}", args)

            # resumen periódico
            abiertas = len(pos)
            total = wins + losses
            wr = (wins / total * 100) if total else 0
            log(f"… {abiertas} abierta(s) · {total} cerradas · WR {wr:.0f}% · balance ${balance:,.2f}")
        except Exception as e:
            log(f"⚠️ error: {e}")
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
