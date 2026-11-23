"""
DESCRIPCIÓN:
ETL para el CSV exportado por Web Scraper (Mercado Libre, motos 101-200 cc).

Uso:
  python etl_motos.py nuevas.csv usadas.csv
  python etl_motos.py nuevas.csv --fecha 2026-11-21
  python etl_motos.py data/raw/*.csv --fecha 2026-11-21 --salida data/procesado

Acepta uno o varios CSV (se deduplican entre sí por URL).
Genera: motos_limpias.csv, motos_hoja.csv (para Sheets), motos_revisar.csv, motos_excluidas.csv.
Solo librería estándar.

Columnas de entrada: web_scraper_start_url, titulo (URL), modelo (texto del título),
precio, atributos, ubicacion, vendedor.
"""
import argparse
import csv
import os
import re
from datetime import date

CC_MIN, CC_MAX = 101, 200

# Ids de condición en la URL de Mercado Libre (ITEM*CONDITION_<id>)
CONDICION_POR_ID = {"2230284": "nueva", "2230581": "usada"}

# No son motos (o no son del alcance del proyecto)
EXCLUIR = re.compile(
    r"cuatrimoto|buggy|\butv\b|\batv\b|karting|motocarro|moto\s?carro|"
    r"carguero|karguero|\brzr\b|\braptor\b", re.I)

# Títulos sin la palabra "cuatrimoto" que probablemente lo son: se mandan a revisar
SOSPECHOSO = re.compile(
    r"^(commander|shark|coyote|viper|panther|xpedition)\b|\bplr\b", re.I)

# Si el título dice claramente que es moto (incluye pit bike/enduro/motocross), no es sospechoso
ES_MOTO = re.compile(r"\bmoto\b|motos\b|pit\s?bike|pitbike|enduro|motocross|\byz\b|\bkx\d*\b", re.I)

MARCAS = {
    "yamaha", "hero", "bajaj", "sym", "akt", "suzuki", "ktm", "honda", "kawasaki",
    "vespa", "tvs", "victory", "kymco", "cfmoto", "aprilia", "auteco", "jialing",
    "polaris", "benelli", "royal", "bmw", "ducati", "triumph", "keeway", "um",
    "apollo", "srm", "plr",
}
ALIAS_MARCA = {"yamha": "yamaha", "kymko": "kymco"}

# Modelo (sin marca en el título) -> marca. Valores por conocimiento general: verificar.
MODELO_MARCA = {
    "hunk": "Hero", "xpulse": "Hero", "x-pulse": "Hero", "xoom": "Hero", "splendor": "Hero",
    "nmax": "Yamaha", "aerox": "Yamaha", "mt15": "Yamaha", "mt-15": "Yamaha", "mt": "Yamaha",
    "fz": "Yamaha", "xtz": "Yamaha", "crypton": "Yamaha", "r15": "Yamaha",
    "adx": "Sym", "adxtg": "Sym", "nhr": "Sym", "drg": "Sym", "crox": "Sym", "angel": "Sym",
    "nkd": "AKT", "nkd125": "AKT", "cr4": "AKT", "tt": "AKT", "flex": "AKT", "dynamic": "AKT",
    "pulsar": "Bajaj", "boxer": "Bajaj", "discover": "Bajaj",
    "duke": "KTM", "raider": "TVS", "apache": "TVS", "address": "Suzuki", "mrx": "Victory", "gixxer": "Suzuki",
}

# Modelos cuyo título NO trae ningún número de cilindrada. Valores nominales
# por conocimiento general: VERIFICAR contra ficha técnica antes de citarlos.
MODELO_CC = {
    "mt15": 155, "mt-15": 155, "mt 15": 155,
    "nmax": 155, "aerox": 155, "r15": 155,
    "n-max": 155, "fz 3.0": 149, "fz 2.0": 149,
    "crypton": 115,
}


def col(fila, *nombres):
    for n in nombres:
        v = fila.get(n)
        if v and v.strip():
            return v.strip()
    return ""


def solo_digitos(texto):
    d = re.sub(r"\D", "", texto or "")
    return int(d) if d else None


def anio_y_km(atributos):
    """'20224.514 Km' = 2022 + 4.514 Km; '20270 Km' = 2027 + 0 Km."""
    a = (atributos or "").strip()
    m = re.match(r"((?:19[89]\d|20[0-3]\d))\s*(.*)$", a)
    anio, resto = (int(m.group(1)), m.group(2)) if m else (None, a)
    mk = re.search(r"([\d\.\,]+)\s*km", resto, flags=re.I)
    return anio, (solo_digitos(mk.group(1)) if mk else None)


def condicion_de(start_url, texto):
    """Prioridad: id de condición en la URL de inicio; si no, texto del título/atributos."""
    m = re.search(r"CONDITION_(\d+)", start_url or "")
    if m and m.group(1) in CONDICION_POR_ID:
        return CONDICION_POR_ID[m.group(1)], "url"
    t = (texto or "").lower()
    if "usado" in t:
        return "usada", "texto"
    if "nuevo" in t or "nueva" in t:
        return "nueva", "texto"
    return "sin_dato", ""


def rango_url(start_url):
    m = re.search(r"(\d+)cc-(\d+)cc", start_url or "")
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def rango_url_valido(rango):
    if not rango:
        return False
    a, b = (int(x) for x in rango.split("-"))
    return a >= CC_MIN - 1 and b <= CC_MAX


def extraer_cc(titulo):
    """Devuelve (cc, origen). Orden: 'NNNcc' -> diccionario -> número nominal en el título."""
    t = titulo or ""
    m = re.search(r"(\d{2,3})\s?(?:cc|c\.c\.?)\b", t, flags=re.I)
    if m:
        return int(m.group(1)), "titulo"
    m = re.search(r"(?<![\d\.])([1-9]\d{2})(?![\d\.])", t)
    if m:
        return int(m.group(1)), "numero_titulo"
    tl = t.lower()
    for clave, cc in MODELO_CC.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(clave)}(?![a-z0-9])", tl):
            return cc, "diccionario"
    return None, ""


def marca_y_modelo(titulo):
    palabras = (titulo or "").split()
    if not palabras:
        return "", ""
    for i, p in enumerate(palabras[:4]):
        p0 = ALIAS_MARCA.get(p.lower().strip(",.;!"), p.lower().strip(",.;!"))
        if p0 in MARCAS:
            marca = p0.upper() if p0 in ("akt", "sym", "ktm", "tvs", "srm", "plr") else p0.capitalize()
            return marca, " ".join(palabras[:i] + palabras[i + 1:])
    for p in palabras[:3]:
        marca = MODELO_MARCA.get(p.lower().strip(",.!"))
        if marca:
            return marca, titulo
    return "", titulo


def leer(rutas):
    filas = []
    for ruta in rutas:
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            lector = csv.DictReader(f)
            print(f"{ruta}: columnas {lector.fieldnames}")
            for r in lector:
                r["_archivo"] = ruta
                filas.append(r)
    return filas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+", help="CSV(s) exportados de Web Scraper")
    ap.add_argument("--fecha", default=date.today().isoformat(),
                    help="fecha real de extracción YYYY-MM-DD (por defecto: hoy)")
    ap.add_argument("--salida", default=".", help="carpeta donde se escriben los CSV de salida")
    args = ap.parse_args()
    os.makedirs(args.salida, exist_ok=True)
    try:
        date.fromisoformat(args.fecha)
    except ValueError:
        ap.error("--fecha debe tener formato YYYY-MM-DD")

    filas = leer(args.csvs)
    print(f"Filas leídas: {len(filas)}")

    vistos, limpias, revisar, excluidas = set(), [], [], []
    duplicadas = sin_url = 0

    for r in filas:
        titulo_col = col(r, "titulo")
        modelo_txt = col(r, "modelo", "titulo_texto")
        url = col(r, "titulo-href", "titulo_href", "url")
        if not url and titulo_col.lower().startswith("http"):
            url = titulo_col
        if not modelo_txt and titulo_col and not titulo_col.lower().startswith("http"):
            modelo_txt = titulo_col
        if not url:
            sin_url += 1
            continue

        patrocinado = "click1.mercadolibre" in url or "/brand_ads/" in url
        url_base = url.split("#")[0] if patrocinado else url.split("#")[0].split("?")[0]
        if url_base in vistos:
            duplicadas += 1
            continue
        vistos.add(url_base)

        start_url = col(r, "web_scraper_start_url", "web-scraper-start-url")
        atributos = col(r, "atributos")
        anio, km = anio_y_km(atributos)
        condicion, cond_origen = condicion_de(start_url, f"{modelo_txt} {atributos}")
        if patrocinado:
            # Los anuncios patrocinados no respetan el filtro del sitio: se deduce por km/año
            anio_fecha = int(args.fecha[:4])
            if km and km > 0:
                condicion, cond_origen = "usada", "km"
            elif km == 0 and anio and anio >= anio_fecha:
                condicion, cond_origen = "nueva", "km"
            else:
                condicion, cond_origen = "verificar", "patrocinado"
        cc, cc_origen = extraer_cc(modelo_txt)
        rango = rango_url(start_url)
        marca, modelo = marca_y_modelo(modelo_txt)

        reg = {
            "titulo": modelo_txt, "marca": marca, "modelo": modelo,
            "precio": solo_digitos(col(r, "precio")),
            "cc": cc, "cc_rango": rango, "cc_origen": cc_origen,
            "condicion": condicion, "condicion_origen": cond_origen,
            "anio": anio, "km": km,
            "ubicacion": col(r, "ubicacion"), "vendedor": col(r, "vendedor"),
            "url": url_base, "patrocinado": "si" if patrocinado else "no",
            "fecha_extraccion": args.fecha, "motivo": "",
        }

        if EXCLUIR.search(modelo_txt):
            reg["motivo"] = "no_es_moto (cuatrimoto/buggy/motocarro/etc.)"
            excluidas.append(reg)
        elif SOSPECHOSO.search(modelo_txt) and not ES_MOTO.search(modelo_txt):
            reg["motivo"] = "posible cuatrimoto sin etiqueta: verificar en el sitio"
            revisar.append(reg)
        elif cc is None:
            if rango_url_valido(rango) and marca:
                reg["cc_origen"] = "filtro_url"   # solo sabemos que está en el rango del filtro
                limpias.append(reg)
            else:
                reg["motivo"] = "sin cilindraje ni marca identificables"
                revisar.append(reg)
        elif CC_MIN <= cc <= CC_MAX:
            limpias.append(reg)
        else:
            reg["motivo"] = f"cc {cc} fuera de {CC_MIN}-{CC_MAX}"
            excluidas.append(reg)

    campos = ["titulo", "marca", "modelo", "precio", "cc", "cc_rango", "cc_origen",
              "condicion", "condicion_origen", "anio", "km", "ubicacion", "vendedor",
              "url", "patrocinado", "fecha_extraccion", "motivo"]
    for nombre, datos in (("motos_limpias.csv", limpias),
                          ("motos_revisar.csv", revisar),
                          ("motos_excluidas.csv", excluidas)):
        with open(os.path.join(args.salida, nombre), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            w.writeheader()
            w.writerows(datos)

    # Archivo para pegar en Google Sheets: las 7 primeras columnas respetan el orden
    # que lee Codigo.gs (titulo, precio, anio, kilometraje, ubicacion, vendedor, enlace);
    # las columnas extra van al final para no romper el dashboard actual.
    hoja = ["titulo", "precio", "anio", "kilometraje", "ubicacion", "vendedor", "enlace",
            "condicion", "marca", "modelo", "cc", "cc_rango", "fecha_extraccion"]
    with open(os.path.join(args.salida, "motos_hoja.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(hoja)
        for x in limpias:
            w.writerow([x["titulo"], x["precio"], x["anio"], x["km"], x["ubicacion"],
                        x["vendedor"], x["url"], x["condicion"], x["marca"], x["modelo"],
                        x["cc"], x["cc_rango"], x["fecha_extraccion"]])

    def cuenta(lista, campo, valor):
        return sum(1 for x in lista if x[campo] == valor)

    print(f"Fecha de extracción: {args.fecha}")
    print(f"Sin URL (ignoradas): {sin_url} | Duplicadas: {duplicadas}")
    print(f"Excluidas: {len(excluidas)} -> motos_excluidas.csv")
    print(f"A revisar: {len(revisar)} -> motos_revisar.csv")
    print(f"Limpias: {len(limpias)} -> motos_limpias.csv y motos_hoja.csv (para Google Sheets)")
    print("  condición:", {c: cuenta(limpias, 'condicion', c)
                           for c in ("nueva", "usada", "sin_dato", "verificar")})
    print("  origen del cc:", {o: cuenta(limpias, 'cc_origen', o)
                               for o in ("titulo", "diccionario", "numero_titulo", "filtro_url")})
    print("  sin marca detectada:", sum(1 for x in limpias if not x["marca"]))


if __name__ == "__main__":
    main()