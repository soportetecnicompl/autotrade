# autotrade — Señales de Telegram (XAUUSD) → MetaTrader 5

Lee un canal de Telegram con tu cuenta, interpreta las señales y las opera en tu cuenta MT5.

## Instalador gráfico para Windows (recomendado)

`AutoTrade.exe` incluye un **asistente paso a paso** (Bienvenida → Telegram → Canal → MetaTrader 5 → Operativa → Horario → Avisos → Probar un mensaje → Analizar el canal → Inicio automático → Panel). No hace falta usar la terminal ni editar archivos: el asistente conecta tu Telegram (con el código que te llega), te deja **elegir el canal de una lista**, prueba MetaTrader 5, guarda la configuración, lanza el análisis del canal y activa el arranque automático. Al final queda un **panel** con los botones Iniciar/Detener, el modo (simulación/real) y el registro en vivo.

**Conseguir el instalador** (necesitas un PC con Windows o GitHub; yo no puedo compilar para Windows desde Linux):

- *Sin tener que instalar nada:* en GitHub abre la pestaña **Actions → Build Windows installer → Run workflow**; al terminar descarga el artefacto `AutoTrade-Setup` (contiene `AutoTrade-Setup-x.y.z.exe`).
- *En un Windows con Python 3.12:* ejecuta `installer\build_windows.bat` (compila `dist\AutoTrade\AutoTrade.exe` y, si tienes [Inno Setup 6](https://jrsoftware.org/isdl.php), el instalador en `dist-installer\`).

**Detalles útiles**

- Se instala por usuario (sin pedir administrador). Tus datos (`config.ini`, sesión de Telegram, registro, informes) viven en `%APPDATA%\AutoTrade` y **se conservan al desinstalar**; el desinstalador detiene el bot y quita el inicio automático.
- El ejecutable no está firmado digitalmente: Windows SmartScreen puede mostrar «Windows protegió tu PC» (pulsa *Más información → Ejecutar de todos modos*) y algunos antivirus desconfían de los programas hechos con PyInstaller. Si te preocupa, compílalo tú mismo con el script de arriba.
- Reabrir el programa más adelante te lleva directo al panel; puedes volver a cualquier paso desde la barra lateral.
- Con `AutoTrade.exe --bot` el bot corre sin interfaz (con reinicio automático si se cae); `--backtest ...` usa los mismos argumentos que `backtest.py`.

> **Qué se probó y qué no.** La interfaz y toda la lógica del asistente están cubiertas por pruebas automáticas (con Telegram y MT5 simulados) y se revisó visualmente en pantalla; el paquete se compiló y ejecutó en Linux para comprobar que incluye todos los módulos. **No se pudo probar en Windows**: la compilación del `.exe`, el instalador de Inno Setup, el inicio automático y la conexión real con tu Telegram y tu MetaTrader 5 son lo primero que debes verificar (en simulación y con una cuenta demo).

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

## Avisos por Telegram

Con `[alerts] enabled = true` el bot te escribe a tu propio chat de **Mensajes guardados** (no hace falta crear otro bot):

| Aviso | Nivel |
|---|---|
| 🟢 Bot iniciado / 🔁 reiniciado automáticamente (con modo, cuenta MT5, canal, lote, horario) | siempre |
| ❌ Orden fallida, sin precio, 💥 error procesando un mensaje o caída del bot | siempre |
| 🔴/🟢 MT5 desconectado del broker, sin respuesta o con *Algo Trading* apagado / recuperado | siempre |
| ✅ Señal operada (órdenes, lotes, SL, TP) y 🔧 comandos del canal aplicados | `notify_signals` |
| ⚠️ Señal ignorada (fuera de horario, SL inválido, sin TP, límite de posiciones, retraso) | `notify_skips` |

En `dry_run` todos los avisos llevan el prefijo `[DRY-RUN]`. El bot revisa MT5 cada `mt5_check_seconds` e intenta reconectar solo si el terminal no responde.

**Si el equipo se apaga o pierde internet, el bot no puede avisarte** (está apagado). Para eso existe el *latido*: crea un chequeo en un servicio como healthchecks.io (verifica su plan gratuito y condiciones), pega su URL en `heartbeat_url` y configúralo para que te avise (correo o Telegram) si pasan más de ~10 minutos sin ping. El bot solo hace ping cuando Telegram **y** MT5 están sanos, así que cubre también esos casos.

## Arranque automático (Windows)

`scripts/run_bot.bat` ejecuta el bot y lo reinicia a los 15 s si se cae por error o desconexión. Ctrl+C lo detiene de verdad (responde `S`/`Y` a "¿Terminar el trabajo por lotes?"). Para que arranque solo con Windows:

1. Instala la tarea: `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1`
   (quitarla: mismo comando con `-Uninstall`). Se lanza al **iniciar sesión**, con 30 s de espera, y abre una ventana con el bot. Si pones `path` a `terminal64.exe` en `config.ini`, el bot también abre MT5 si no está corriendo.
2. **Inicio de sesión automático**: MT5 es una app de escritorio, así que la tarea solo corre con la sesión abierta. Configura el autologin con la herramienta *Autologon* de Sysinternals o `netplwiz`. Ojo: deja la contraseña recuperable en el equipo; úsalo solo en un equipo de acceso físico controlado y con una cuenta de Windows dedicada.
3. En el **BIOS**: activa "Restore on AC Power Loss" para que el equipo encienda solo al volver la luz. En Windows: sin suspensión/hibernación y horario activo para actualizaciones.
4. Prueba el ciclo completo: reinicia el equipo y comprueba que llega el aviso "🔁 Bot REINICIADO" sin tocar nada.

> Los scripts de Windows (`.bat` y `.ps1`) no pudieron ejecutarse en el entorno donde se desarrollaron (Linux). Pruébalos primero con el bot en `dry_run`.

## Órdenes rechazadas: reintentos y distancia mínima

- **Requote / precio cambiado** (códigos 10004, 10020, 10021): en órdenes a mercado el bot reintenta con el precio actual hasta `requote_retries` veces. Al **abrir**, si el precio empeora más de `retry_max_slippage` USD respecto al de la señal, cancela y te avisa (no persigue el mercado). Un precio que mejora se acepta. Los **cierres** reintentan sin ese límite, porque cerrar importa más que el precio.
- **Distancia mínima del broker** (*stops level*): antes de enviar, el bot revisa que SL y TP (y las pendientes respecto al precio) cumplan el mínimo del símbolo, midiéndolo como MT5. Con `stops_policy = skip` (por defecto) la señal no se opera y te avisa; con `adjust` aleja el SL/TP hasta el mínimo, lo que cambia el riesgo u objetivo de la señal, y te lo avisa. El break-even y "mover SL" también respetan ese mínimo y avisan si no pueden aplicarse.
- Todo lo demás (margen insuficiente, mercado cerrado, Algo Trading apagado...) se rechaza igual y llega como aviso ❌ con el código de MT5.

## Filtro de horario

En `config.ini`, sección `[filters]`, limitas en qué días y horas se aceptan señales **nuevas**:

```ini
[filters]
trading_days = Mon-Fri
trading_hours = 08:00-12:00, 14:00-18:00
tz_offset_hours = -6
```

- La hora que cuenta es la del mensaje en Telegram (no la del momento en que el bot lo procesa), escrita en la zona `tz_offset_hours` (Honduras = -6). El fin de cada ventana es exclusivo; se admite cruzar medianoche (`22:00-02:00`).
- Vacío = sin restricción. Los comandos del canal (BE, close all, cancelar) **siempre** se obedecen, aunque estén fuera de horario, para no dejar posiciones abiertas sin gestionar.
- Las posiciones ya abiertas no se cierran al terminar el horario; eso lo gestionan su SL/TP.
- Pruébalo antes con el backtester usando las mismas reglas: `python backtest.py run ... --trading-days Mon-Fri --trading-hours "08:00-12:00" --report-tz -6`. Cuidado con sobreajustar: elegir horas mirando solo el pasado de un canal suele dar resultados que no se repiten; busca patrones con muchas señales, no con 3 o 4.

## Backtest del canal

Antes de operar, mide si las señales del canal habrían sido rentables con TUS reglas (mismo parser y planificador que el bot).

```bat
python backtest.py fetch --days 90 --out history.json
python backtest.py run --history history.json --prices mt5 --server-offset 2 --us-dst --lot 0.02
```

- `--prices mt5` usa las velas M1 de tu terminal (Windows). El broker solo guarda cierta historia: abre el gráfico M1 del símbolo, sube *Max bars in chart* y desplázate hacia atrás. Alternativa: `--prices xauusd_m1.csv` (formato `time,open,high,low,close` o export de MT5).
- **`--server-offset` es crítico**: horas del servidor de tu broker respecto a UTC en invierno (muchos brokers: 2, con `--us-dst` suman 1 h en verano de EE. UU.). Si lo pones mal, señales y precios quedan desalineados y el resultado no sirve. Verifícalo mirando la hora de un mensaje del canal contra la vela del mismo movimiento.
- **Informe completo**: además de la tabla en consola, genera `report.html` (ábrelo en el navegador) con retorno y drawdown en % sobre `--balance` (por defecto 1000), acierto con intervalo de confianza, factor de beneficio, curva de capital, resultados por mes, por hora y por día, y **conclusiones en texto**.
- **Mejores formas de operar el canal**: compara 1, 2 y 3 TPs, siguiendo o no los mensajes del canal (6 variantes con el mismo lote total) y marca la de mejor relación ganancia/drawdown, con el `max_tps` y `enable_commands` equivalentes para `config.ini`. También mide la **sensibilidad al retraso** (5 s a 5 min) y sugiere un **lote total** para que un drawdown del doble del histórico no pase del 20% del saldo.
- **Horario sugerido, validado**: elige las horas/días rentables con el 70% inicial del historial y las comprueba con el 30% final. El informe solo recomienda el filtro si «se sostiene» fuera de muestra; si no, avisa de que es sobreajuste. Con pocas señales (menos de 20) no propone nada.
- Opciones útiles: `--balance 2000`, `--report mi_informe.html`, `--min-hour-signals 5` (más exigente al proponer horas) y `--no-sweep` (más rápido).
- Honestidad estadística: con menos de ~30 señales el informe lo advierte; elegir la mejor de 6 variantes sobre los mismos datos infla su resultado (también se avisa). Si dos variantes dan resultados parecidos, quédate con la más simple.
- Compara dos modos: **static** (solo SL/TP originales) y **follow** (además obedece "BE", "close all", "close half"...). Entrega % de acierto, % de TP1, factor de beneficio, drawdown, peor racha, y tablas por hora y por día. `backtest_trades.csv` trae el detalle orden por orden.
- Reglas conservadoras: si SL y TP caen en la misma vela, gana el SL; en la vela donde se llena una pendiente solo cuenta el SL; spread fijo (`--spread`), retraso de ejecución (`--delay`) y cierre forzado a las `--max-hold-hours`.
- Límites: velas M1 no dicen el orden real dentro del minuto, no modela slippage ni swaps, y los mensajes borrados o editados del canal no aparecen tal como se vieron en vivo. Úsalo para descartar canales malos, no como promesa de resultados.

## Importante

- Nada de esto garantiza ganancias; muchos canales de señales pierden dinero. Valida con demo.
- El formato de cada canal es distinto. Si un mensaje no se interpreta bien, pásalo por `--test` y ajusta
  las expresiones en `autotrade/parser.py` (añade el caso a `tests/test_parser.py`).
- Para correr 24/7 usa un VPS Windows. Si el PC se apaga, el bot no opera ni gestiona (los SL/TP ya puestos sí quedan en el broker).
- Usar tu cuenta de usuario con Telethon es válido para leer canales de los que eres miembro, pero no abuses de la API.
