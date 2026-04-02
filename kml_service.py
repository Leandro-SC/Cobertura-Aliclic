import sys
from pathlib import Path
from typing import Optional, List, Dict, Any
from xml.etree import ElementTree as ET

import pandas as pd
import geopandas as gpd
import requests

from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.validation import make_valid


EPSG_WGS84 = "EPSG:4326"

KML_NS = {
    "kml": "http://www.opengis.net/kml/2.2"
}


class KMLProcessingError(Exception):
    pass


def read_file_bytes(file_path: str | Path) -> bytes:
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"No existe el archivo: {file_path}")

    return file_path.read_bytes()


def extract_networklink_url(kml_bytes: bytes) -> Optional[str]:
    """
    Detecta si el KML local es un contenedor con NetworkLink.
    """
    try:
        root = ET.fromstring(kml_bytes)

        href = root.find(".//kml:NetworkLink/kml:Link/kml:href", KML_NS)
        if href is not None and href.text:
            return href.text.strip()

        href = root.find(".//kml:NetworkLink/kml:Url/kml:href", KML_NS)
        if href is not None and href.text:
            return href.text.strip()

        return None

    except Exception as e:
        raise KMLProcessingError(
            f"No se pudo inspeccionar el XML del KML: {e}"
        ) from e


def download_linked_kml(url: str) -> bytes:
    """
    Descarga el KML real desde el NetworkLink.
    """
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    except Exception as e:
        raise KMLProcessingError(
            f"No se pudo descargar el KML enlazado: {url}"
        ) from e


def parse_coordinates_text(coords_text: str) -> List[tuple]:
    """
    Convierte texto KML de coordenadas en lista de (lon, lat).
    Formato KML: lon,lat[,alt]
    """
    coords = []

    for token in coords_text.strip().split():
        parts = token.split(",")

        if len(parts) < 2:
            continue

        lon = float(parts[0])
        lat = float(parts[1])

        coords.append((lon, lat))

    return coords


def parse_linear_ring(ring_el: ET.Element) -> List[tuple]:
    coords_el = ring_el.find(".//kml:coordinates", KML_NS)

    if coords_el is None or not coords_el.text:
        return []

    return parse_coordinates_text(coords_el.text)


def build_polygon_from_kml(polygon_el: ET.Element):
    """
    Convierte un elemento KML Polygon a shapely Polygon.
    Soporta outerBoundaryIs e innerBoundaryIs.
    """
    outer_ring_el = polygon_el.find(
        ".//kml:outerBoundaryIs/kml:LinearRing", KML_NS
    )

    if outer_ring_el is None:
        return None

    shell = parse_linear_ring(outer_ring_el)

    if len(shell) < 4:
        return None

    holes = []

    inner_rings = polygon_el.findall(
        ".//kml:innerBoundaryIs/kml:LinearRing", KML_NS
    )

    for inner in inner_rings:
        hole = parse_linear_ring(inner)

        if len(hole) >= 4:
            holes.append(hole)

    try:
        poly = Polygon(shell, holes)

        if not poly.is_valid:
            poly = make_valid(poly)

        return poly

    except Exception:
        return None


def extract_style_definitions(root: ET.Element):
    """
    Extrae:
    - styles: style_id -> color_kml
    - stylemaps: stylemap_id -> style_url_normal
    """
    styles: Dict[str, Optional[str]] = {}
    stylemaps: Dict[str, Optional[str]] = {}

    # Styles
    for style_el in root.findall(".//kml:Style", KML_NS):
        style_id = style_el.attrib.get("id")

        if not style_id:
            continue

        color_el = style_el.find(
            ".//kml:PolyStyle/kml:color", KML_NS
        )

        color_kml = (
            color_el.text.strip().lower()
            if color_el is not None and color_el.text
            else None
        )

        styles[f"#{style_id}"] = color_kml

    # StyleMaps
    for sm_el in root.findall(".//kml:StyleMap", KML_NS):
        sm_id = sm_el.attrib.get("id")

        if not sm_id:
            continue

        style_url_normal = None

        for pair_el in sm_el.findall(".//kml:Pair", KML_NS):
            key_el = pair_el.find("kml:key", KML_NS)
            style_url_el = pair_el.find("kml:styleUrl", KML_NS)

            key = (
                key_el.text.strip()
                if key_el is not None and key_el.text
                else None
            )

            style_url = (
                style_url_el.text.strip()
                if style_url_el is not None and style_url_el.text
                else None
            )

            if key == "normal":
                style_url_normal = style_url
                break

        stylemaps[f"#{sm_id}"] = style_url_normal

    return styles, stylemaps


def resolve_placemark_color(
    placemark_el: ET.Element,
    styles: dict,
    stylemaps: dict
) -> Optional[str]:
    """
    Resuelve el color efectivo del Placemark:
    1. Inline Style
    2. styleUrl -> Style
    3. styleUrl -> StyleMap -> Style
    """
    # 1) Inline
    inline_color_el = placemark_el.find(
        ".//kml:Style/kml:PolyStyle/kml:color", KML_NS
    )

    if inline_color_el is not None and inline_color_el.text:
        return inline_color_el.text.strip().lower()

    # 2) styleUrl
    style_url_el = placemark_el.find("kml:styleUrl", KML_NS)

    if style_url_el is None or not style_url_el.text:
        return None

    style_url = style_url_el.text.strip()

    if style_url in styles:
        return styles[style_url]

    if style_url in stylemaps:
        normal_style = stylemaps[style_url]

        if normal_style in styles:
            return styles[normal_style]

    return None


def classify_coverage_by_color(color_kml: Optional[str]) -> Optional[str]:
    """
    KML usa formato AABBGGRR.
    Rojo puro común en KML: ff0000ff
    """
    if not color_kml:
        return None

    color_kml = color_kml.lower()

    red_variants = {
        "ff0000ff",
        "7f0000ff",
    }

    if color_kml in red_variants:
        return "Atendemos - Alejado"

    return "Atendemos - Cerca"


def parse_placemarks_to_gdf(kml_bytes: bytes) -> gpd.GeoDataFrame:
    """
    Parsea el KML real y extrae polígonos + color + clasificación.
    """
    try:
        root = ET.fromstring(kml_bytes)

    except Exception as e:
        raise KMLProcessingError(
            f"No se pudo parsear el XML del KML real: {e}"
        ) from e

    styles, stylemaps = extract_style_definitions(root)

    rows: List[Dict[str, Any]] = []

    for placemark_el in root.findall(".//kml:Placemark", KML_NS):
        name_el = placemark_el.find("kml:name", KML_NS)

        zona_nombre = (
            name_el.text.strip()
            if name_el is not None and name_el.text
            else "Sin nombre"
        )

        color_kml = resolve_placemark_color(
            placemark_el, styles, stylemaps
        )

        tipo_cobertura = classify_coverage_by_color(color_kml)

        polygons = placemark_el.findall(".//kml:Polygon", KML_NS)

        geometries = []

        for poly_el in polygons:
            poly = build_polygon_from_kml(poly_el)

            if poly is not None and not poly.is_empty:
                geometries.append(poly)

        if not geometries:
            continue

        if len(geometries) == 1:
            geom = geometries[0]
        else:
            geom = MultiPolygon([
                g for g in geometries if isinstance(g, Polygon)
            ])

        if geom is None or geom.is_empty:
            continue

        if not geom.is_valid:
            geom = make_valid(geom)

        rows.append({
            "zona_nombre": zona_nombre,
            "color_kml": color_kml,
            "tipo_cobertura": tipo_cobertura,
            "geometry": geom,
        })

    if not rows:
        raise KMLProcessingError(
            "No se encontraron polígonos válidos en el KML real."
        )

    gdf = gpd.GeoDataFrame(
        rows,
        geometry="geometry",
        crs=EPSG_WGS84
    )

    return gdf


def extract_coverage_geometries(kml_path: str) -> gpd.GeoDataFrame:
    """
    Lee el KML local; si es NetworkLink, descarga el KML real.
    """
    kml_bytes = read_file_bytes(kml_path)

    network_url = extract_networklink_url(kml_bytes)

    if network_url:
        print(
            "[INFO] El archivo es un NetworkLink. Descargando KML real desde:\n"
            f"{network_url}"
        )

        real_kml_bytes = download_linked_kml(network_url)

        return parse_placemarks_to_gdf(real_kml_bytes)

    return parse_placemarks_to_gdf(kml_bytes)


def validate_manual_points(
    points: list[tuple[float, float]]
) -> pd.DataFrame:
    rows = []

    for lat, lon in points:
        if not (-90 <= lat <= 90):
            raise ValueError(f"Latitud fuera de rango: {lat}")

        if not (-180 <= lon <= 180):
            raise ValueError(f"Longitud fuera de rango: {lon}")

        rows.append({
            "latitud": lat,
            "longitud": lon,
        })

    return pd.DataFrame(rows)


def validate_points_against_coverage(points_df, coverage_gdf):

    results = []

    # Spatial index
    sindex = coverage_gdf.sindex

    for _, row in points_df.iterrows():
        lat = row["latitud"]
        lon = row["longitud"]

        pt = Point(lon, lat)

        estado = "Fuera"
        zona_match = None
        color_match = None
        tipo_match = "Fuera de cobertura"

        # 🔥 SOLO candidatos cercanos
        possible_matches_index = list(sindex.intersection(pt.bounds))
        possible_matches = coverage_gdf.iloc[possible_matches_index]

        for _, zona in possible_matches.iterrows():
            geom = zona.geometry

            if geom.contains(pt):
                estado = "Dentro"
            elif geom.covers(pt):
                estado = "Borde"
            else:
                continue

            zona_match = zona["zona_nombre"]
            color_match = zona["color_kml"]
            tipo_match = zona["tipo_cobertura"] or "Atendemos"
            break

        results.append({
            "latitud": lat,
            "longitud": lon,
            "resultado": estado,
            "zona_nombre": zona_match,
            "color_kml": color_match,
            "tipo_cobertura": tipo_match,
        })

    return pd.DataFrame(results)


def export_results(df: pd.DataFrame, output_path: str):
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[OK] Resultado exportado a: {output_path}")


if __name__ == "__main__":
    try:
        kml_file = "./data/cobertura.kml"

        puntos_manual = [
            (-13.537813, -71.9672144),
            (-13.6375834, -72.8899413),
            (-7.2209432, -78.4295833),
        ]

        cobertura_gdf = extract_coverage_geometries(kml_file)

        print("[INFO] Zonas cargadas:", len(cobertura_gdf))
        print(
            cobertura_gdf[
                ["zona_nombre", "color_kml", "tipo_cobertura"]
            ].head(20)
        )

        puntos_df = validate_manual_points(puntos_manual)

        resultado_df = validate_points_against_coverage(
            puntos_df,
            cobertura_gdf
        )

        print("\n=== RESULTADO ===")
        print(resultado_df.to_string(index=False))

        export_results(resultado_df, "resultado_manual.csv")

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        sys.exit(1)
