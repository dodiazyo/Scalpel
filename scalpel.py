#!/usr/bin/env python3
"""
Scalpel — backtester de scalping (scalping + bisturí).
====================================================
Hereda lo mejor del backtester de Axiom (descarga OKX con caché, loop vela a
vela sin lookahead, costes realistas, métricas honestas) pero con un motor
LIMPIO de scalping, sin el equipaje de MNV/MOM.

Estrategia inicial: REVERSIÓN A LA MEDIA (Bollinger) con FILTRO DE RÉGIMEN (ADX).
La lógica:
  · El precio que se aleja de su media en marcos cortos tiende a volver.
  · PERO la reversión a la media muere en tendencias → solo operamos cuando el
    ADX es bajo (mercado lateral). Ese es el filtro que le faltó a Axiom.

Entradas (sobre vela cerrada, se ejecuta al open de la siguiente):
  · LONG  si close <= banda inferior  Y  ADX < adx_max
  · SHORT si close >= banda superior  Y  ADX < adx_max
Salidas:
  · TP: el precio vuelve a la media (SMA central)
  · SL: stop fijo en % (--sl-pct)
  · Tiempo: máximo de velas en posición (--max-hold)

Uso:
    python3 scalpel.py --symbols BTC/USDT ETH/USDT --days 90
    python3 scalpel.py --synthetic            # sin internet, valida el motor
    python3 scalpel.py --days 90 --bb-std 2.5 --adx-max 18
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

OKX_URL = "https://www.okx.com"
DATA_DIR = Path(__file__).with_name("scalpel_data")
WARMUP = 60  # velas para estabilizar indicadores


# ─────────────────────────────────────────────────────────────────────────────
# Datos
# ─────────────────────────────────────────────────────────────────────────────
def download_okx_candles(sym: str, days: int, bar: str = "1m") -> pd.DataFrame:
    """Descarga velas históricas del swap perpetuo de OKX, con caché local."""
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"{sym.replace('/', '')}_{bar}_{days}d.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        print(f"  [{sym}] {len(df)} velas desde caché ({cache.name})")
        return df

    inst = sym.replace("/", "-") + "-SWAP"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    rows: list = []
    after = end_ms

    print(f"  [{sym}] descargando ~{days * 24 * 60} velas de OKX…", end="", flush=True)
    while True:
        r = requests.get(
            f"{OKX_URL}/api/v5/market/history-candles",
            params={"instId": inst, "bar": bar, "limit": 100, "after": after},
            timeout=(5, 15),
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") not in (None, "0"):
            raise RuntimeError(f"OKX: {payload.get('msg')}")
        data = payload.get("data", [])
        if not data:
            break
        rows.extend(data)
        oldest = int(data[-1][0])
        after = oldest
        if oldest <= start_ms:
            break
        if len(rows) % 5000 < 100:
            print(".", end="", flush=True)
        time.sleep(0.10)
    print(f" {len(rows)} velas")

    cols = ["ts", "open", "high", "low", "close", "volume", "vol_ccy", "vol_quote", "confirm"]
    df = pd.DataFrame(rows, columns=cols)[["ts", "open", "high", "low", "close", "volume"]]
    df = df.astype({"ts": "int64", "open": "float64", "high": "float64",
                    "low": "float64", "close": "float64", "volume": "float64"})
    df = df[df["ts"] >= start_ms].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.to_csv(cache, index=False)
    return df


def synthetic_candles(n: int = 60000, seed: int = 7, p0: float = 50000.0) -> pd.DataFrame:
    """Velas sintéticas de 1m con regímenes de tendencia/rango para validar el motor."""
    rng = np.random.default_rng(seed)
    drift = np.zeros(n)
    i = 0
    while i < n:
        seg = int(rng.integers(800, 3000))
        seg = min(seg, n - i)              # no pasarse del array
        mu = rng.choice([0.00004, -0.00003, 0.0, 0.0, 0.00002, -0.00002])
        drift[i:i + seg] = mu
        i += seg
    rets = drift + rng.normal(0, 0.0009, n)
    close = p0 * np.exp(np.cumsum(rets))
    open_ = np.roll(close, 1); open_[0] = p0
    spread = np.abs(rng.normal(0, 0.0006, n))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    vol = rng.lognormal(8, 0.5, n)
    ts = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low,
                         "close": close, "volume": vol})


# ─────────────────────────────────────────────────────────────────────────────
# Indicadores
# ─────────────────────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame, bb_period: int, bb_std: float,
                   adx_period: int, rsi_period: int = 14, don_period: int = 20) -> pd.DataFrame:
    d = df.copy()
    # Bollinger
    d["sma"] = d["close"].rolling(bb_period).mean()
    sd = d["close"].rolling(bb_period).std(ddof=0)
    d["bb_up"] = d["sma"] + bb_std * sd
    d["bb_dn"] = d["sma"] - bb_std * sd

    # Donchian (máximo/mínimo de las N velas anteriores, sin incluir la actual)
    d["don_hi"] = d["high"].rolling(don_period).max().shift(1)
    d["don_lo"] = d["low"].rolling(don_period).min().shift(1)

    # Ratio de volumen vs su media (convicción del movimiento)
    d["vol_ratio"] = d["volume"] / d["volume"].rolling(20).mean()
    d["hour"] = pd.to_datetime(d["ts"]).dt.hour

    # RSI (Wilder)
    delta = d["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    d["rsi"] = 100 - 100 / (1 + rs)

    # ADX (Wilder)
    high, low, close = d["high"], d["low"], d["close"]
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / adx_period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=d.index).ewm(alpha=1 / adx_period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=d.index).ewm(alpha=1 / adx_period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1 / adx_period, adjust=False).mean()
    d["atr"] = atr
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────────────────────
class ScalpelBT:
    def __init__(self, symbols, dfs, args):
        self.symbols = symbols
        self.args = args
        self.fee = args.fee
        self.slip = args.slippage_bps / 10_000.0
        self.spread = args.spread_bps / 10_000.0
        self.notional = args.capital * args.leverage

        self.prepared = {
            s: add_indicators(dfs[s], args.bb_period, args.bb_std, args.adx_period,
                              args.rsi_period, args.don_period)
            for s in symbols
        }
        # alinear por índice común
        common = None
        for s in symbols:
            tset = set(self.prepared[s]["ts"])
            common = tset if common is None else common & tset
        self.index = pd.DatetimeIndex(sorted(common))
        for s in symbols:
            self.prepared[s] = self.prepared[s].set_index("ts").loc[self.index].reset_index()
        self.n = len(self.index)

        self.balance = float(args.balance)
        self.balance_ini = self.balance
        self.fees_total = 0.0
        self.cross_total = 0.0   # spread + slippage
        self.trades: list[dict] = []
        self.equity: list[dict] = []
        self.pos: dict[str, dict] = {}   # posición abierta por símbolo

    def _open(self, sym, direction, ts, open_px, sma, atr):
        # Modo maker: entras con orden límite → no cruzas el spread, fee maker.
        # Modo taker (default): cruzas el spread + slippage, fee taker.
        if self.args.maker:
            adj = 0.0
            entry = open_px
            fee = self.notional * self.args.maker_fee
        else:
            adj = self.spread / 2 + self.slip
            entry = open_px * (1 + adj) if direction == "LONG" else open_px * (1 - adj)
            fee = self.notional * self.fee
        qty = self.notional / entry
        cross = self.notional * adj
        self.fees_total += fee
        self.cross_total += cross
        self.balance -= fee
        # SL: por ATR (escala con la volatilidad) si --sl-atr-mult > 0; si no, % fijo.
        if self.args.sl_atr_mult > 0 and atr and not np.isnan(atr):
            dist = self.args.sl_atr_mult * atr
            sl = entry - dist if direction == "LONG" else entry + dist
        else:
            sl = entry * (1 - self.args.sl_pct / 100) if direction == "LONG" \
                else entry * (1 + self.args.sl_pct / 100)
        # Momentum: sin TP fijo (deja correr con trailing); reversión: TP en la media.
        tp = None if self.args.strategy == "momentum" else sma
        self.pos[sym] = {
            "dir": direction, "entry": entry, "qty": qty, "sl": sl,
            "tp": tp, "ts_open": str(ts), "barras": 0,
            "peak": entry, "atr": atr,
        }

    def _close(self, sym, ts, exit_px, motivo):
        p = self.pos.pop(sym)
        # El TP es una orden límite (maker) si el modo maker está activo;
        # SL y salidas por tiempo siempre cruzan el mercado (taker).
        maker_exit = self.args.maker and motivo == "TP"
        if maker_exit:
            adj = 0.0
            px = exit_px
            fee = self.notional * self.args.maker_fee
        else:
            adj = self.spread / 2 + self.slip
            px = exit_px * (1 - adj) if p["dir"] == "LONG" else exit_px * (1 + adj)
            fee = self.notional * self.fee
        gan = (px - p["entry"]) * p["qty"] if p["dir"] == "LONG" \
            else (p["entry"] - px) * p["qty"]
        cross = self.notional * adj
        self.fees_total += fee
        self.cross_total += cross
        self.balance += gan - fee
        self.trades.append({
            "ts_open": p["ts_open"], "ts_close": str(ts), "sym": sym, "dir": p["dir"],
            "entrada": p["entry"], "salida": px, "pnl": round(gan - fee, 4),
            "motivo": motivo, "barras": p["barras"],
        })

    def run(self):
        t0 = time.time()
        for i in range(WARMUP, self.n):
            ts = self.index[i]
            unreal = 0.0
            for sym in self.symbols:
                prep = self.prepared[sym]
                c = prep.iloc[i]
                prev = prep.iloc[i - 1]

                # gestionar posición abierta
                if sym in self.pos:
                    p = self.pos[sym]
                    p["barras"] += 1

                    # Trailing stop por ATR (momentum): el SL sigue al precio favorable.
                    tmult = self.args.trail_atr_mult
                    if tmult > 0 and p["atr"] and not np.isnan(p["atr"]):
                        if p["dir"] == "LONG":
                            p["peak"] = max(p["peak"], c["high"])
                            p["sl"] = max(p["sl"], p["peak"] - tmult * p["atr"])
                        else:
                            p["peak"] = min(p["peak"], c["low"])
                            p["sl"] = min(p["sl"], p["peak"] + tmult * p["atr"])

                    hit_sl = (c["low"] <= p["sl"]) if p["dir"] == "LONG" else (c["high"] >= p["sl"])
                    hit_tp = p["tp"] is not None and (
                        (c["high"] >= p["tp"]) if p["dir"] == "LONG" else (c["low"] <= p["tp"]))
                    if hit_sl:
                        self._close(sym, ts, p["sl"], "Trail" if tmult > 0 else "SL")
                    elif hit_tp:
                        self._close(sym, ts, p["tp"], "TP")
                    elif p["barras"] >= self.args.max_hold:
                        self._close(sym, ts, c["close"], "Tiempo")
                    else:
                        unreal += ((c["close"] - p["entry"]) * p["qty"]) if p["dir"] == "LONG" \
                            else ((p["entry"] - c["close"]) * p["qty"])
                    continue

                a = self.args
                if np.isnan(prev["adx"]) or np.isnan(prev["atr"]):
                    continue

                if a.strategy == "momentum":
                    # Ruptura del canal Donchian CON tendencia (ADX alto).
                    if np.isnan(prev["don_hi"]):
                        continue
                    trending = prev["adx"] > a.adx_min
                    if not trending:
                        continue
                    long_ok = prev["close"] > prev["don_hi"]
                    short_ok = prev["close"] < prev["don_lo"]
                else:
                    # Reversión a la media: extremo de banda CON rango (ADX bajo).
                    if np.isnan(prev["bb_dn"]) or np.isnan(prev["rsi"]):
                        continue
                    if prev["adx"] >= a.adx_max:
                        continue
                    if a.require_reversal:
                        long_trig = prev["low"] <= prev["bb_dn"] and prev["close"] > prev["bb_dn"]
                        short_trig = prev["high"] >= prev["bb_up"] and prev["close"] < prev["bb_up"]
                    else:
                        long_trig = prev["close"] <= prev["bb_dn"]
                        short_trig = prev["close"] >= prev["bb_up"]
                    long_ok = long_trig and prev["rsi"] <= a.rsi_long
                    short_ok = short_trig and prev["rsi"] >= a.rsi_short

                # ── Filtros de calidad (principiados, atacan el ratio coste/edge) ──
                # 1) Volatilidad mínima: solo si hay movimiento que capturar.
                if a.min_atr_pct > 0 and prev["close"] > 0:
                    if (prev["atr"] / prev["close"] * 100) < a.min_atr_pct:
                        long_ok = short_ok = False
                # 2) Volumen elevado: convicción.
                if a.min_vol_ratio > 0 and not np.isnan(prev["vol_ratio"]):
                    if prev["vol_ratio"] < a.min_vol_ratio:
                        long_ok = short_ok = False
                # 3) Sesión horaria (UTC).
                if a.hour_start >= 0 and a.hour_end >= 0:
                    h = int(prev["hour"])
                    in_sesion = (a.hour_start <= h < a.hour_end) if a.hour_start <= a.hour_end \
                        else (h >= a.hour_start or h < a.hour_end)
                    if not in_sesion:
                        long_ok = short_ok = False

                if long_ok:
                    self._open(sym, "LONG", ts, c["open"], prev["sma"], prev["atr"])
                elif short_ok:
                    self._open(sym, "SHORT", ts, c["open"], prev["sma"], prev["atr"])

            self.equity.append({"ts": str(ts), "balance": round(self.balance, 2),
                                "equity": round(self.balance + unreal, 2)})
            if (i - WARMUP) % 20000 == 0:
                pct = (i - WARMUP) / (self.n - WARMUP) * 100
                print(f"  … {pct:5.1f}%  ({self.index[i].date()})  equity ${self.balance + unreal:,.2f}")

        # cerrar lo que quede
        for sym in list(self.pos):
            self._close(sym, self.index[-1], float(self.prepared[sym].iloc[-1]["close"]), "Fin")

        print(f"\n  Backtest completado en {time.time() - t0:,.1f}s")
        return self.report()

    def report(self) -> str:
        tr = pd.DataFrame(self.trades)
        L = []
        w = L.append
        w("=" * 64)
        nombre = "MOMENTUM (Donchian)" if self.args.strategy == "momentum" else "REVERSIÓN A LA MEDIA"
        w(f"SCALPEL — RESUMEN ({nombre})")
        w("=" * 64)
        bar_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                   "1H": 60, "1h": 60, "2H": 120, "4H": 240, "6H": 360,
                   "12H": 720, "1D": 1440, "1Dutc": 1440}.get(self.args.bar, 1)
        cpd = 1440 / bar_min  # velas por día
        w(f"Periodo:        {self.index[WARMUP].date()} → {self.index[-1].date()} "
          f"({(self.n - WARMUP) / cpd:.0f} días, velas {self.args.bar})")
        w(f"Símbolos:       {', '.join(self.symbols)}")
        if self.args.strategy == "momentum":
            w(f"Parámetros:     MOMENTUM · Donchian({self.args.don_period}) · "
              f"ADX>{self.args.adx_min} · trail {self.args.trail_atr_mult}xATR · max_hold {self.args.max_hold}")
        else:
            w(f"Parámetros:     MEANREV · BB({self.args.bb_period},{self.args.bb_std}) · "
              f"ADX<{self.args.adx_max} · SL {self.args.sl_pct}pct · max_hold {self.args.max_hold}")
        w(f"Costes:         fee {self.fee*100:.3f}%/lado · spread {self.args.spread_bps}bps · "
          f"slippage {self.args.slippage_bps}bps · notional ${self.notional:,.0f}")
        w("-" * 64)
        ret = (self.balance / self.balance_ini - 1) * 100
        w(f"Balance:        ${self.balance_ini:,.2f} → ${self.balance:,.2f}  ({ret:+.2f}%)")
        w(f"Costes totales: fees ${self.fees_total:,.2f} + cruce ${self.cross_total:,.2f} "
          f"= ${self.fees_total + self.cross_total:,.2f}")

        if tr.empty:
            w("Sin trades — revisa parámetros o periodo.")
            txt = "\n".join(L); print("\n" + txt); return txt

        wins = tr[tr["pnl"] > 0]
        losses = tr[tr["pnl"] < 0]
        bruto = tr["pnl"].sum() + self.fees_total  # PnL antes de fees (aprox)
        pf = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() else float("inf")
        w("-" * 64)
        w(f"Trades:         {len(tr)}")
        w(f"Win rate:       {len(wins)/len(tr)*100:.1f}%   |   Profit factor: {pf:.2f}")
        w(f"PnL neto:       ${tr['pnl'].sum():+,.2f}   |   PnL bruto (sin fees): ${bruto:+,.2f}")
        w(f"Esperanza:      ${tr['pnl'].mean():+.4f}/trade   |   "
          f"Mejor ${tr['pnl'].max():+.2f} / Peor ${tr['pnl'].min():+.2f}")
        w(f"Barras medias:  {tr['barras'].mean():.1f} velas en posición")
        w("-" * 64)
        w("Por motivo de cierre:")
        for mot, g in tr.groupby("motivo"):
            w(f"  {mot:8s}: {len(g):5d} trades | PnL ${g['pnl'].sum():+10.2f}")
        w("Por símbolo:")
        for sym, g in tr.groupby("sym"):
            gw = g[g["pnl"] > 0]
            w(f"  {sym}: {len(g):5d} trades | WR {len(gw)/len(g)*100:5.1f}% | PnL ${g['pnl'].sum():+10.2f}")
        w("=" * 64)

        tr.to_csv(Path(__file__).with_name("scalpel_trades.csv"), index=False)
        pd.DataFrame(self.equity).to_csv(Path(__file__).with_name("scalpel_equity.csv"), index=False)
        txt = "\n".join(L)
        Path(__file__).with_name("scalpel_report.txt").write_text(txt)
        print("\n" + txt)
        print("\nArchivos: scalpel_trades.csv · scalpel_equity.csv · scalpel_report.txt")
        return txt


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Scalpel — backtester de scalping")
    ap.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--bar", default="1m", help="timeframe OKX: 1m, 5m, 15m, 1H…")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--capital", type=float, default=100.0, help="margen por trade")
    ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--strategy", choices=["meanrev", "momentum"], default="meanrev",
                    help="meanrev = reversión a la media; momentum = ruptura Donchian")
    ap.add_argument("--bb-period", type=int, default=20)
    ap.add_argument("--bb-std", type=float, default=2.0)
    ap.add_argument("--don-period", type=int, default=20, help="velas del canal Donchian (momentum)")
    ap.add_argument("--adx-period", type=int, default=14)
    ap.add_argument("--adx-max", type=float, default=25.0, help="meanrev: solo opera si ADX < este valor")
    ap.add_argument("--adx-min", type=float, default=25.0, help="momentum: solo opera si ADX > este valor")
    ap.add_argument("--trail-atr-mult", type=float, default=0.0,
                    help="trailing stop = N x ATR (momentum); 0 = sin trailing")
    ap.add_argument("--sl-pct", type=float, default=0.5, help="stop loss en porciento (fijo)")
    ap.add_argument("--sl-atr-mult", type=float, default=0.0,
                    help="SL = N x ATR (escala con volatilidad); 0 = usa --sl-pct fijo")
    ap.add_argument("--max-hold", type=int, default=30, help="máx velas en posición")
    ap.add_argument("--fee", type=float, default=0.0005, help="fee taker por lado")
    ap.add_argument("--spread-bps", type=float, default=1.0)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--maker", action="store_true",
                    help="entradas y TP como ordenes limite (maker), sin cruzar spread")
    ap.add_argument("--maker-fee", type=float, default=0.0002,
                    help="fee maker por lado (default 0.02 porciento)")
    ap.add_argument("--rsi-period", type=int, default=14)
    ap.add_argument("--rsi-long", type=float, default=100.0,
                    help="solo LONG si RSI <= este valor (default 100 = sin filtro)")
    ap.add_argument("--rsi-short", type=float, default=0.0,
                    help="solo SHORT si RSI >= este valor (default 0 = sin filtro)")
    ap.add_argument("--require-reversal", action="store_true",
                    help="exige vela de rechazo (mecha fuera, cierre dentro de la banda)")
    ap.add_argument("--min-atr-pct", type=float, default=0.0,
                    help="solo entra si ATR/precio >= este pct (filtro de volatilidad)")
    ap.add_argument("--min-vol-ratio", type=float, default=0.0,
                    help="solo entra si volumen/media >= este valor (filtro de convicción)")
    ap.add_argument("--hour-start", type=int, default=-1, help="hora UTC inicio sesión (-1 = off)")
    ap.add_argument("--hour-end", type=int, default=-1, help="hora UTC fin sesión (-1 = off)")
    args = ap.parse_args()

    print("Scalpel — backtester de scalping")
    print("─" * 40)
    if args.synthetic:
        print("Modo sintético (validación del motor):")
        dfs = {s: synthetic_candles(60000, seed=11 + k) for k, s in enumerate(args.symbols)}
    else:
        print(f"Descargando {args.days} días de OKX (velas {args.bar}):")
        dfs = {s: download_okx_candles(s, args.days, args.bar) for s in args.symbols}

    bt = ScalpelBT(args.symbols, dfs, args)
    print(f"\nCorriendo {bt.n - WARMUP:,} velas…")
    bt.run()


if __name__ == "__main__":
    sys.exit(main())
