"""Carga de la librería MetaTrader5.

Su extensión nativa (_core.pyd) importa numpy DESDE C, y PyInstaller solo ve los
imports de Python: sin un `import numpy` visible en nuestro código, numpy no se
empaqueta y `import MetaTrader5` falla dentro del .exe. Por eso los imports viven
aquí, a la vista del análisis estático, y todo el programa carga MT5 con import_mt5().
"""


def import_mt5():
    import numpy  # noqa: F401 - necesario para MetaTrader5 (ver docstring del módulo)
    import MetaTrader5
    return MetaTrader5
