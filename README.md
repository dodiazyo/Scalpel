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
la media → intradía corto; seguimiento de tendencia → swing (4H+). El backtester
cumplió su función: descartar una estrategia sin ventaja **sin arriesgar capital**.

## Segunda investigación: MOMENTUM (ruptura Donchian) — un edge real

Tras descartar la reversión, se probó la estrategia **espejo**: comprar rupturas del
canal Donchian con tendencia (ADX alto) y dejar correr con trailing stop por ATR.

```bash
python3 scalpel.py --strategy momentum --bar 4H --days 730 \
    --don-period 20 --adx-min 25 --trail-atr-mult 3.0
```

**Resultado (4H, 2 años, taker realista, 6 activos):** +196%, profit factor 2.18.

| Activo | PnL | | Activo | PnL |
|--------|-----|--|--------|-----|
| XRP | +$965 ✅ | | BNB | +$243 ✅ |
| ETH | +$501 ✅ | | SOL | +$85 ✅ |
| DOGE | +$412 ✅ | | **BTC** | **−$49 ❌** |

**Validación pasada:**
- Robusto a **parámetros** (8 configuraciones, todas PF 1.58–2.36)
- Robusto en **tiempo** (1, 1.5 y 2 años, todas verdes)
- Sobrevive **costes taker realistas** + slippage alto
- Generaliza a **5 de 6 activos** — el único perdedor es BTC

**Tesis:** el momentum es un edge de **altcoins**. Las alts (alto beta) rompen en
tendencias limpias; BTC, como activo dominante/reserva, es más reversivo — por eso es
el único que pierde con breakouts. Reversión → intradía; momentum → swing en alts.

**Caveats honestos (lo que falta antes de creerle con dinero):**
1. **Sesgo de supervivencia:** los 6 activos son alts que siguen vivas hoy. Hace 2
   años no era obvio cuáles sobrevivirían; en real habría alts que murieron.
2. El backtest asume que el trailing stop llena al precio exacto (sin gaps).
3. Entradas modeladas al open siguiente; en rupturas rápidas el fill real es peor.
4. 2 años = un ciclo macro; los regímenes de crypto cambian.
5. Sin gestión de riesgo de cartera (sizing fijo por trade).

**Conclusión:** Scalpel encontró un edge candidato real (momentum 4H en alts), bien
validado en lo medible, pero pendiente de validación de ejecución y sesgo de
supervivencia antes de arriesgar capital. La metodología — descartar lo que no sirve,
confirmar lo que sí con múltiples pruebas de robustez — es el verdadero producto.
