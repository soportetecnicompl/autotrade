# autotrade — Señales de Telegram (XAUUSD) → MetaTrader 5

Lee un canal de Telegram con tu cuenta, interpreta las señales y las opera en tu cuenta MT5.

## Qué hace

| Mensaje del canal | Acción |
|---|---|
| `XAUUSD BUY NOW @ 2650 SL 2640 TP1 2655 TP2 2660` | Abre una orden por cada TP (máx. `max_tps`, por defecto 2); `lot_size` es el lote total y se reparte entre ellas |
| `GOLD SELL 2650-2655 ...` | Rango: entra a mercado si el precio está en la zona, si no deja pendiente en el borde |
| `BUY LIMIT 2640 ...` / `SELL STOP ...` | Orden pendiente (con vencimiento `pending_expiry_min`) |
| `Move SL to BE` / `break even` | SL a precio de apertura (solo posiciones ya en ganancia) |
| `Close half` / `close 30%` | Cierre parcial |
| `Close all` / `cerrar todo` | Cierra todo lo del bot |
| `Move SL to 2652` | Mueve el SL |
| `Cancel` | Borra pendientes |

Si el comando es una **respuesta (reply)** a la señal original, solo afecta a esa señal; si no, afecta a todas las del bot.
También entiende español (`COMPRA`, `VENTA`, `STOP LOSS`, `TAKE PROFIT`, `cerrar todo`…).

Protecciones: `dry_run` activado por defecto, SL obligatorio, valida que SL/TP estén del lado correcto,
descarta señales viejas o con entrada lejana, tope de posiciones abiertas, y solo toca operaciones con su `magic`.

## Requisitos

- **Windows** (PC o VPS) con el terminal **MetaTrader 5** instalado y la sesión iniciada, con *Algo Trading* activado.
  La librería `MetaTrader5` de Python no funciona en Linux/Mac.
- Python 3.10+
- `api_id` y `api_hash` de https://my.telegram.org (tu cuenta debe ser miembro del canal).

## Instalación

```bat
pip install -r requirements.txt
copy config.example.ini config.ini
```

Edita `config.ini` (nunca lo subas a git; ya está en `.gitignore`).

1. Prueba el parser con un mensaje real del canal (no necesita Telegram ni MT5):
   ```bat
   python main.py --test "XAUUSD BUY 2650 SL 2640 TP1 2655 TP2 2660" --price 2650.2
   ```
2. Encuentra el ID del canal (pide el código de Telegram la primera vez):
   ```bat
   python main.py --list-dialogs
   ```
3. Arranca con `dry_run = true` y revisa `autotrade.log` unos días:
   ```bat
   python main.py
   ```
4. Cuando las decisiones del log coincidan con lo que esperas, pasa a una **cuenta demo** con `dry_run = false`,
   y solo después a real con lote mínimo.

Tests: `python -m unittest discover -s tests -t .`

## Backtest del canal

Antes de operar, mide si las señales del canal habrían sido rentables con TUS reglas (mismo parser y planificador que el bot).

```bat
python backtest.py fetch --days 90 --out history.json
python backtest.py run --history history.json --prices mt5 --server-offset 2 --us-dst --lot 0.02
```

- `--prices mt5` usa las velas M1 de tu terminal (Windows). El broker solo guarda cierta historia: abre el gráfico M1 del símbolo, sube *Max bars in chart* y desplázate hacia atrás. Alternativa: `--prices xauusd_m1.csv` (formato `time,open,high,low,close` o export de MT5).
- **`--server-offset` es crítico**: horas del servidor de tu broker respecto a UTC en invierno (muchos brokers: 2, con `--us-dst` suman 1 h en verano de EE. UU.). Si lo pones mal, señales y precios quedan desalineados y el resultado no sirve. Verifícalo mirando la hora de un mensaje del canal contra la vela del mismo movimiento.
- Compara dos modos: **static** (solo SL/TP originales) y **follow** (además obedece "BE", "close all", "close half"...). Entrega % de acierto, % de TP1, factor de beneficio, drawdown, peor racha, y tablas por hora y por día. `backtest_trades.csv` trae el detalle orden por orden.
- Reglas conservadoras: si SL y TP caen en la misma vela, gana el SL; en la vela donde se llena una pendiente solo cuenta el SL; spread fijo (`--spread`), retraso de ejecución (`--delay`) y cierre forzado a las `--max-hold-hours`.
- Límites: velas M1 no dicen el orden real dentro del minuto, no modela slippage ni swaps, y los mensajes borrados o editados del canal no aparecen tal como se vieron en vivo. Úsalo para descartar canales malos, no como promesa de resultados.

## Importante

- Nada de esto garantiza ganancias; muchos canales de señales pierden dinero. Valida con demo.
- El formato de cada canal es distinto. Si un mensaje no se interpreta bien, pásalo por `--test` y ajusta
  las expresiones en `autotrade/parser.py` (añade el caso a `tests/test_parser.py`).
- Para correr 24/7 usa un VPS Windows. Si el PC se apaga, el bot no opera ni gestiona (los SL/TP ya puestos sí quedan en el broker).
- Usar tu cuenta de usuario con Telethon es válido para leer canales de los que eres miembro, pero no abuses de la API.
