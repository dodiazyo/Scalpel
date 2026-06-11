#!/usr/bin/env python3
"""
Scalpel WEB — dashboard de paper trading conectado a OKX.
=========================================================
Backend FastAPI que corre el motor de paper trading (SIMULADO, sin dinero real)
sobre datos en vivo de OKX, y sirve un dashboard para verlo operar.

Estrategias:
  · meanrev  — reversión a la media (Bollinger + ADX bajo), pensada para 15m
  · momentum — ruptura Donchian + trailing ATR, pensada para 4H (alts)

Uso:
    pip install fastapi uvicorn
    python3 scalpel_web.py
    # abre http://127.0.0.1:8005
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path

from scalpel import add_indicators

OKX = "https://www.okx.com"
HERE = Path(__file__).parent


def fetch_live(sym: str, bar: str, limit: int = 300) -> pd.DataFrame:
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


class PaperEngine:
    def __init__(self):
        self.cfg = {
            "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "bar": "15m", "strategy": "meanrev",
            "bb_period": 20, "bb_std": 2.5, "adx_period": 14, "adx_max": 18.0,
            "rsi_period": 14, "don_period": 20, "adx_min": 25.0,
            "sl_atr_mult": 1.5, "trail_atr_mult": 3.0,
            "capital": 100.0, "leverage": 5, "fee": 0.0005,
        }
        self.balance = 1000.0
        self.balance_ini = 1000.0
        self.pos: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.sym_data: dict[str, dict] = {}
        self.last_ts: dict[str, pd.Timestamp] = {}
        self.wins = self.losses = 0
        self.running = False
        self.connected = False
        self.last_update = None
        self.log: list[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _say(self, msg: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insert(0, f"[{stamp}] {msg}")
        self.log = self.log[:80]

    def _loop(self):
        # Lee OKX SIEMPRE (mercado en vivo), opere o no. El trading se hace
        # solo cuando self.running, pero la conexión y los precios son continuos.
        while True:
            ok = False
            for sym in self.cfg["symbols"]:
                try:
                    self._refresh(sym)
                    ok = True
                except Exception as e:
                    self._say(f"⚠️ {sym}: {e}")
            self.connected = ok
            if ok:
                self.last_update = datetime.now().strftime("%H:%M:%S")
            time.sleep(10)

    def _refresh(self, sym: str):
        c = self.cfg
        notional = c["capital"] * c["leverage"]
        df = fetch_live(sym, c["bar"])
        d = add_indicators(df, c["bb_period"], c["bb_std"], c["adx_period"],
                           c["rsi_period"], c["don_period"])
        if len(d) < 30:
            return
        closed = d.iloc[-2]
        prev_close = float(closed["close"])
        price = float(d.iloc[-1]["open"])      # precio vivo aprox (vela en formación)
        live_close = float(d.iloc[-1]["close"])
        cts = closed["ts"]

        with self._lock:
            prevp = self.sym_data.get(sym, {}).get("price")
            self.sym_data[sym] = {
                "price": round(live_close, 6),
                "change": round((live_close - prevp) / prevp * 100, 3) if prevp else 0.0,
                "rsi": round(float(closed["rsi"]), 1),
                "adx": round(float(closed["adx"]), 1),
                "bb_dn": round(float(closed["bb_dn"]), 6),
                "bb_up": round(float(closed["bb_up"]), 6),
                "sma": round(float(closed["sma"]), 6),
                "don_hi": round(float(closed["don_hi"]), 6) if not pd.isna(closed["don_hi"]) else None,
                "don_lo": round(float(closed["don_lo"]), 6) if not pd.isna(closed["don_lo"]) else None,
            }

            # El trading solo ocurre si el motor está en marcha.
            if not self.running:
                return
            price = live_close

            # gestionar posición abierta
            if sym in self.pos:
                p = self.pos[sym]
                if c["strategy"] == "momentum" and c["trail_atr_mult"] > 0:
                    atr = p["atr"]
                    if p["dir"] == "LONG":
                        p["peak"] = max(p["peak"], price)
                        p["sl"] = max(p["sl"], p["peak"] - c["trail_atr_mult"] * atr)
                    else:
                        p["peak"] = min(p["peak"], price)
                        p["sl"] = min(p["sl"], p["peak"] + c["trail_atr_mult"] * atr)
                hit_sl = price <= p["sl"] if p["dir"] == "LONG" else price >= p["sl"]
                hit_tp = p["tp"] is not None and (
                    price >= p["tp"] if p["dir"] == "LONG" else price <= p["tp"])
                if hit_sl or hit_tp:
                    exitp = p["sl"] if hit_sl else p["tp"]
                    gan = (exitp - p["entry"]) * p["qty"] if p["dir"] == "LONG" \
                        else (p["entry"] - exitp) * p["qty"]
                    gan -= notional * c["fee"] * 2
                    self.balance += gan
                    if gan >= 0: self.wins += 1
                    else: self.losses += 1
                    self.trades.insert(0, {
                        "sym": sym, "dir": p["dir"], "entry": p["entry"],
                        "exit": round(exitp, 6), "pnl": round(gan, 2),
                        "motivo": "TP" if hit_tp else "SL",
                        "ts": datetime.now().strftime("%H:%M:%S"),
                    })
                    self.trades = self.trades[:60]
                    self._say(f"CIERRE {sym} {p['dir']} {'TP✅' if hit_tp else 'SL❌'} "
                              f"PnL ${gan:+.2f} | balance ${self.balance:,.2f}")
                    del self.pos[sym]
                return

            # entrada (una vez por vela cerrada)
            if self.last_ts.get(sym) == cts:
                return
            self.last_ts[sym] = cts
            if pd.isna(closed["adx"]) or pd.isna(closed["atr"]):
                return

            direction = None
            if c["strategy"] == "momentum":
                if pd.isna(closed["don_hi"]) or closed["adx"] <= c["adx_min"]:
                    pass
                elif closed["close"] > closed["don_hi"]:
                    direction = "LONG"
                elif closed["close"] < closed["don_lo"]:
                    direction = "SHORT"
            else:
                if not pd.isna(closed["bb_dn"]) and closed["adx"] < c["adx_max"]:
                    if closed["close"] <= closed["bb_dn"]:
                        direction = "LONG"
                    elif closed["close"] >= closed["bb_up"]:
                        direction = "SHORT"

            if direction:
                entry = price
                qty = notional / entry
                atr = float(closed["atr"])
                sl = entry - c["sl_atr_mult"] * atr if direction == "LONG" \
                    else entry + c["sl_atr_mult"] * atr
                tp = None if c["strategy"] == "momentum" else float(closed["sma"])
                self.pos[sym] = {"dir": direction, "entry": entry, "qty": qty,
                                 "sl": sl, "tp": tp, "atr": atr, "peak": entry}
                self._say(f"ENTRADA {sym} {direction} @ ${entry:,.4f} "
                          f"SL ${sl:,.4f} TP {('—' if tp is None else f'${tp:,.4f}')}")

    def status(self) -> dict:
        with self._lock:
            total = self.wins + self.losses
            # Posiciones con P&L en vivo (no realizado) usando el precio actual.
            positions = []
            open_pnl = 0.0
            for s, p in self.pos.items():
                price = self.sym_data.get(s, {}).get("price") or p["entry"]
                pnl = (price - p["entry"]) * p["qty"] if p["dir"] == "LONG" \
                    else (p["entry"] - price) * p["qty"]
                open_pnl += pnl
                positions.append({
                    "sym": s, "dir": p["dir"],
                    "entry": round(p["entry"], 6), "price": round(price, 6),
                    "sl": round(p["sl"], 6),
                    "tp": round(p["tp"], 6) if p["tp"] is not None else None,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / self.cfg["capital"] * 100, 2) if self.cfg["capital"] else 0.0,
                })
            return {
                "running": self.running, "connected": self.connected,
                "last_update": self.last_update,
                "strategy": self.cfg["strategy"], "bar": self.cfg["bar"],
                "balance": round(self.balance, 2), "balance_ini": self.balance_ini,
                "pnl": round(self.balance - self.balance_ini, 2),
                "open_pnl": round(open_pnl, 2),
                "wins": self.wins, "losses": self.losses,
                "win_rate": round(self.wins / total * 100) if total else 0,
                "symbols": self.sym_data,
                "positions": positions,
                "trades": self.trades[:30],
                "log": self.log[:40],
                "cfg": self.cfg,
            }


engine = PaperEngine()
app = FastAPI(title="Scalpel Web")


class Cfg(BaseModel):
    symbols: list[str] | None = None
    bar: str | None = None
    strategy: str | None = None
    adx_max: float | None = None
    adx_min: float | None = None
    bb_std: float | None = None
    don_period: int | None = None
    sl_atr_mult: float | None = None
    trail_atr_mult: float | None = None
    capital: float | None = None
    leverage: int | None = None


@app.get("/")
def index():
    return FileResponse(str(HERE / "static_web" / "index.html"))


@app.get("/api/status")
def status():
    return engine.status()


@app.post("/api/start")
def start():
    engine.running = True
    engine._say("▶ Motor iniciado (paper trading)")
    return {"ok": True}


@app.post("/api/stop")
def stop():
    engine.running = False
    engine._say("⏸ Motor detenido")
    return {"ok": True}


@app.post("/api/config")
def config(cfg: Cfg):
    for k, v in cfg.model_dump().items():
        if v is not None:
            engine.cfg[k] = v
    engine._say(f"⚙️ Config actualizada: {engine.cfg['strategy']} · {engine.cfg['bar']}")
    return {"ok": True, "cfg": engine.cfg}


app.mount("/static", StaticFiles(directory=str(HERE / "static_web")), name="static")


if __name__ == "__main__":
    import uvicorn
    print("Scalpel Web → http://127.0.0.1:8005")
    uvicorn.run(app, host="0.0.0.0", port=8005)
