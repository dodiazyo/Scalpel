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

## Conclusiones de la investigación (BTC/ETH, reversión a la media)

Se barrió la estrategia a través de timeframes, midiendo el **ratio coste/edge**
(coste por trade ÷ edge bruto por trade) como variable maestra:

| Timeframe | PnL bruto (señal) | Profit factor | Ratio coste/edge |
|-----------|-------------------|---------------|------------------|
| 1m  | +$5  (apenas)     | 0.62 | 5.2× |
| 5m  | +$33 (positivo)   | 0.77 | 4.0× |
| 15m | +$19 (positivo)   | **0.90** | **2.4×** |
| 1H  | −$39 (negativo)   | 0.82 | señal perdedora |

**Hallazgos:**

1. **Los costes son el adversario principal del scalping.** En 1m, una estrategia
   con 60%+ de aciertos pierde porque el edge bruto (~0.01% del notional) es 5×
   menor que el coste de entrar/salir.
2. **Las órdenes maker (límite) casi reducen los costes a la mitad** vs taker, pero
   no bastan cuando el edge es muy fino.
3. **El SL debe escalar con la volatilidad (ATR), no ser un % fijo** — un stop fijo
   penaliza injustamente los timeframes altos.
4. **La reversión a la media tiene un punto dulce intradía (5m–15m).** En 1m los
   costes dominan; en 1H la señal misma se vuelve perdedora porque 1H ya es
   territorio de tendencia (donde la reversión fracasa por naturaleza).
5. **Veredicto:** en su mejor configuración (15m, maker, SL por ATR) llega a
   profit factor **0.90** — cerca pero sin cruzar a verde. El edge es real pero
   demasiado fino para despejar los costes de forma confiable. **No operar.**

La lección transferible: cada estrategia tiene su hábitat de timeframe. Reversión a
la media → intradía corto; seguimiento de tendencia → swing (1H+). El backtester
cumplió su función: descartar una estrategia sin ventaja **sin arriesgar capital**.
