"""Parser KML conforme convenções da Tab. 4 (cap 6 §6.3 da dissertação).

Lê features de um arquivo KML do Google Earth e separa em 4 categorias:
- Field: polígono-base do talhão (`Field=Rate`)
- Zone polygons: polígonos de inclusão/exclusão (`Label=Rate` ou `Label=0`)
- Circular points: pontos com raio (`Label=Rate:Radius`, ex: `Cupinzeiro=0:3m`)
- Sample points: pontos de amostra para IDW (`Label=Rate` ou `Rate`)
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

EARTH_RADIUS_M = 6_371_000.0
NAMESPACE = {"kml": "http://www.opengis.net/kml/2.2"}

CIRCLE_RE = re.compile(
    r"^\s*([^=]+?)\s*=\s*(-?\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*m?\s*$",
    re.IGNORECASE,
)
# Círculo sem label (label opcional): "0:5m", "120:3m" etc.
RATE_RADIUS_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*m?\s*$",
    re.IGNORECASE,
)
LABEL_RATE_RE = re.compile(r"^\s*([^=]+?)\s*=\s*(-?\d+(?:\.\d+)?)\s*$")


def polygon_area_m2(coords: list[tuple[float, float]]) -> float:
    """Área de um polígono fechado em m² (fórmula do shoelace)."""
    n = len(coords)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


@dataclass
class Polygon:
    label: str
    rate: float
    coords_xy: list[tuple[float, float]] = field(default_factory=list)
    area_m2: float = 0.0


@dataclass
class CircularPoint:
    label: str
    rate: float
    radius_m: float
    x: float
    y: float


@dataclass
class SamplePoint:
    label: str
    rate: float
    x: float
    y: float


@dataclass
class Field:
    rate: float
    coords_xy: list[tuple[float, float]] = field(default_factory=list)
    area_m2: float = 0.0


@dataclass
class KmlData:
    field_polygon: Field | None
    zones: list[Polygon]
    circles: list[CircularPoint]
    samples: list[SamplePoint]
    origin_lat: float
    origin_lon: float

    def bbox(self) -> tuple[float, float, float, float]:
        xs: list[float] = []
        ys: list[float] = []
        if self.field_polygon:
            xs.extend(p[0] for p in self.field_polygon.coords_xy)
            ys.extend(p[1] for p in self.field_polygon.coords_xy)
        for z in self.zones:
            xs.extend(p[0] for p in z.coords_xy)
            ys.extend(p[1] for p in z.coords_xy)
        for c in self.circles:
            xs.append(c.x)
            ys.append(c.y)
        for s in self.samples:
            xs.append(s.x)
            ys.append(s.y)
        if not xs:
            raise ValueError("KML vazio: nenhuma feature reconhecida")
        return min(xs), min(ys), max(xs), max(ys)


def project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Projeção equiretangular local em metros, origem (lat0, lon0)."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def _parse_coords(text: str, lat0: float, lon0: float) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        lon = float(parts[0])
        lat = float(parts[1])
        pts.append(project(lat, lon, lat0, lon0))
    return pts


def _first_coord(text: str) -> tuple[float, float] | None:
    """Devolve (lat, lon) do primeiro token de uma string KML coordinates."""
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            return float(parts[1]), float(parts[0])
    return None


def _classify_name(name: str) -> tuple[str, dict[str, float | str]] | None:
    name = (name or "").strip()
    if not name:
        return None
    # Círculo com label: "Pedra=0:5m"
    m = CIRCLE_RE.match(name)
    if m:
        return "circle", {
            "label": m.group(1).strip(),
            "rate": float(m.group(2)),
            "radius_m": float(m.group(3)),
        }
    # Círculo sem label: "0:5m"
    m = RATE_RADIUS_RE.match(name)
    if m:
        return "circle", {
            "label": "",
            "rate": float(m.group(1)),
            "radius_m": float(m.group(2)),
        }
    # Polígono com label: "Good=100", "Field=0", "Sede=0"
    m = LABEL_RATE_RE.match(name)
    if m:
        label = m.group(1).strip()
        rate = float(m.group(2))
        if label.lower() == "field":
            return "field", {"label": label, "rate": rate}
        return "label", {"label": label, "rate": rate}
    # Polígono ou ponto sem label, só a taxa: "100", "75", "0"
    try:
        return "rate_only", {"label": "", "rate": float(name)}
    except ValueError:
        return None


def parse_kml(path: str | Path) -> KmlData:
    """Lê KML e devolve KmlData com coordenadas projetadas em metros."""
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()

    placemarks = root.findall(".//kml:Placemark", NAMESPACE)
    if not placemarks:
        # KML sem namespace
        placemarks = root.findall(".//Placemark")

    def _find(parent, ns_path: str, plain_path: str):
        node = parent.find(ns_path, NAMESPACE)
        if node is None:
            node = parent.find(plain_path)
        return node

    # Primeira passada: descobrir origem da projeção (lat0, lon0)
    origin_lat = origin_lon = None
    for pm in placemarks:
        coord_node = _find(pm, ".//kml:coordinates", ".//coordinates")
        if coord_node is not None and coord_node.text:
            first = _first_coord(coord_node.text)
            if first is not None:
                origin_lat, origin_lon = first
                break
    if origin_lat is None:
        raise ValueError(f"KML sem coordenadas válidas: {path}")

    field_poly: Field | None = None
    zones: list[Polygon] = []
    circles: list[CircularPoint] = []
    samples: list[SamplePoint] = []

    for pm in placemarks:
        name_node = _find(pm, "kml:name", "name")
        if name_node is None or not name_node.text:
            continue
        classified = _classify_name(name_node.text)

        polygon_coords = _find(pm, ".//kml:Polygon//kml:coordinates", ".//Polygon//coordinates")
        point_coords = _find(pm, ".//kml:Point/kml:coordinates", ".//Point/coordinates")

        # Polígono sem tag de dose (ex.: contorno do talhão "Viçosa") vira
        # field_polygon com rate=0, se ainda não houver um Field=Rate explícito.
        if classified is None:
            if polygon_coords is not None and polygon_coords.text and field_poly is None:
                coords = _parse_coords(polygon_coords.text, origin_lat, origin_lon)
                field_poly = Field(
                    rate=0.0, coords_xy=coords, area_m2=polygon_area_m2(coords)
                )
            continue

        kind, attrs = classified

        if polygon_coords is not None and polygon_coords.text:
            coords = _parse_coords(polygon_coords.text, origin_lat, origin_lon)
            if kind == "field":
                field_poly = Field(
                    rate=float(attrs["rate"]),
                    coords_xy=coords,
                    area_m2=polygon_area_m2(coords),
                )
            else:
                zones.append(
                    Polygon(
                        label=str(attrs["label"]),
                        rate=float(attrs["rate"]),
                        coords_xy=coords,
                        area_m2=polygon_area_m2(coords),
                    )
                )
        elif point_coords is not None and point_coords.text:
            first = _first_coord(point_coords.text)
            if first is None:
                continue
            x, y = project(first[0], first[1], origin_lat, origin_lon)
            if kind == "circle":
                circles.append(
                    CircularPoint(
                        label=str(attrs["label"]),
                        rate=float(attrs["rate"]),
                        radius_m=float(attrs["radius_m"]),
                        x=x,
                        y=y,
                    )
                )
            else:
                samples.append(
                    SamplePoint(
                        label=str(attrs["label"]),
                        rate=float(attrs["rate"]),
                        x=x,
                        y=y,
                    )
                )

    return KmlData(
        field_polygon=field_poly,
        zones=zones,
        circles=circles,
        samples=samples,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
    )
