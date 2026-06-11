# Scalpel

Backtester de **scalping** honesto — hereda la infraestructura probada del backtester de Axiom
(descarga OKX con caché, loop vela a vela sin lookahead, costes realistas, métricas honestas)
con un motor **limpio**, sin el equipaje de las estrategias de swing.

## Estrategia inicial: reversión a la media + filtro de régimen

- **Entrada LONG**: el precio cierra por debajo de la banda inferior de Bollinger **y** el ADX
  es bajo (mercado lateral, no en tendencia).
- **Entrada SHORT**: espejo — cierre por encima de la banda superior con ADX bajo.
- **Salidas**: TP en la media (SMA central), SL fijo en %, o máximo de velas en posición.

El filtro de ADX ataca el punto débil conocido de la reversión a la media: muere en tendencias.

## Uso

```bash
# Validar el motor sin internet (datos sintéticos)
python3 scalpel.py --synthetic

# Backtest real de BTC/ETH en velas de 1m, últimos 90 días
python3 scalpel.py --symbols BTC/USDT ETH/USDT --days 90

# Palancas de experimentación
python3 scalpel.py --days 90 --bb-std 2.5         # desviaciones más extremas
python3 scalpel.py --days 90 --adx-max 18         # solo mercados muy laterales
python3 scalpel.py --days 90 --spread-bps 3       # prueba de costes más duros
```

## La lección del scalping

En scalping los **costes** (fee + spread + slippage) no son un detalle: son el adversario
principal. Una estrategia puede acertar el 60%+ de las veces y aun así perder, porque cada
uno de los cientos de trades diarios paga peaje. El reporte separa el **PnL bruto** del
**coste total** justo para que veas dónde se decide la vida o muerte de la estrategia.

## Salidas

- `scalpel_data/` — caché de velas descargadas
- `scalpel_trades.csv` — todos los trades con detalle
- `scalpel_equity.csv` — curva de capital
- `scalpel_report.txt` — resumen de métricas
