"""Static Mammotion map renderer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageColor, ImageDraw

CANVAS_SIZE = (1024, 768)
BACKGROUND = (245, 245, 245, 255)
AREA_FILL = (59, 191, 97, 75)
AREA_STROKE = (59, 191, 97, 255)
OBSTACLE_FILL = (255, 149, 20, 85)
OBSTACLE_STROKE = (255, 149, 20, 230)
PATH_STROKE = (45, 45, 45, 240)
TRAIL_SHADOW_STROKE = (20, 28, 36, 95)
TRAIL_STROKE = (215, 220, 225, 150)
TRAIL_RECENT_STROKE = (255, 255, 255, 230)
VIRTUAL_STROKE = (220, 40, 40, 230)
MOWER_FILL = (245, 247, 248, 255)
MOWER_STROKE = (82, 90, 98, 220)
OSM_MAX_ZOOM = 19
OSM_TILE_SIZE = 256
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT = "HomeAssistant-Mammotion-Map/1.0"
TRAIL_RENDER_MIN_DISTANCE_METERS = 0.8
TRAIL_RENDER_MAX_SEGMENT_METERS = 25.0
TRAIL_RENDER_SPIKE_MIN_METERS = 4.0
TRAIL_RENDER_SIMPLIFY_METERS = 2.0
TRAIL_RENDER_SMOOTHING_PASSES = 2
TRAIL_RENDER_DENOISE_WINDOW = 5
TRAIL_RECENT_POINTS = 40


@dataclass(frozen=True)
class MapTileProvider:
    """Describe a raster tile source used by the static map renderer."""

    key: str
    url_template: str
    attribution: str
    user_agent: str = OSM_USER_AGENT

    def tile_url(self, zoom: int, tile_x: int, tile_y: int) -> str:
        """Return the URL for one XYZ map tile."""
        return self.url_template.format(z=zoom, x=tile_x, y=tile_y)


OPENSTREETMAP_TILE_PROVIDER = MapTileProvider(
    key="openstreetmap",
    url_template=OSM_TILE_URL,
    attribution="© OpenStreetMap contributors",
)
ESRI_WORLD_IMAGERY_TILE_PROVIDER = MapTileProvider(
    key="esri_world_imagery",
    url_template=(
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    attribution="Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics",
)


class _TileProviderUnavailable(Exception):
    """Raised after a tile provider request fails during one render."""


@dataclass(frozen=True)
class GeoBounds:
    """Geographic bounds in WGS84 lon/lat."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @property
    def width(self) -> float:
        return self.max_lon - self.min_lon

    @property
    def height(self) -> float:
        return self.max_lat - self.min_lat

    @property
    def center(self) -> tuple[float, float]:
        return (
            (self.min_lon + self.max_lon) / 2,
            (self.min_lat + self.max_lat) / 2,
        )

    def expanded(self) -> GeoBounds:
        center_lat = self.center[1]
        pad_lat = max(self.height * 0.08, _meters_to_lat_degrees(3.0))
        pad_lon = max(self.width * 0.08, _meters_to_lon_degrees(3.0, center_lat))
        return GeoBounds(
            self.min_lon - pad_lon,
            self.min_lat - pad_lat,
            self.max_lon + pad_lon,
            self.max_lat + pad_lat,
        )


def placeholder_png() -> bytes:
    """Return a placeholder when map geometry is not available."""
    image = Image.new("RGBA", CANVAS_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    text = "No mower map available yet"
    text_bounds = draw.textbbox((0, 0), text)
    draw.text(
        (
            (CANVAS_SIZE[0] - (text_bounds[2] - text_bounds[0])) / 2,
            (CANVAS_SIZE[1] - (text_bounds[3] - text_bounds[1])) / 2,
        ),
        text,
        fill=(120, 120, 120, 255),
    )
    return _encode(image)


def render_map_png(
    geojson: dict[str, Any] | None,
    tile_cache_dir: str | None = None,
    mower_location: Any | None = None,
    mower_trail: list[tuple[float, float]] | None = None,
    tile_provider: MapTileProvider = OPENSTREETMAP_TILE_PROVIDER,
) -> bytes:
    """Render a Mammotion GeoJSON map into a static PNG."""
    mower_point = _geo_location_point(mower_location)
    trail_segments = _geo_trail_segments(_valid_geo_points(mower_trail or []))
    trail_points = [point for segment in trail_segments for point in segment]
    points = _geometry_points(geojson or {})
    points.extend(trail_points)
    if mower_point is not None:
        points.append(mower_point)
    if not points:
        return placeholder_png()

    bounds = _geo_bounds(points).expanded()
    center_lon, center_lat = bounds.center
    zoom = OSM_MAX_ZOOM
    min_pixel = _lonlat_to_pixel(bounds.min_lon, bounds.max_lat, zoom)
    max_pixel = _lonlat_to_pixel(bounds.max_lon, bounds.min_lat, zoom)
    pixel_width = max(max_pixel[0] - min_pixel[0], 1.0)
    pixel_height = max(max_pixel[1] - min_pixel[1], 1.0)
    scale = min(
        (CANVAS_SIZE[0] * 0.90) / pixel_width,
        (CANVAS_SIZE[1] * 0.90) / pixel_height,
    )
    scale = max(min(scale, 8.0), 0.2)

    center_pixel = _lonlat_to_pixel(center_lon, center_lat, zoom)
    source_width = CANVAS_SIZE[0] / scale
    source_height = CANVAS_SIZE[1] / scale
    source_min_x = center_pixel[0] - source_width / 2
    source_min_y = center_pixel[1] - source_height / 2
    source = _render_osm_source(
        zoom,
        source_min_x,
        source_min_y,
        source_width,
        source_height,
        tile_cache_dir,
        tile_provider,
    )
    image = source.resize(CANVAS_SIZE, Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(image, "RGBA")

    def project(coord: tuple[float, float]) -> tuple[float, float]:
        pixel_x, pixel_y = _lonlat_to_pixel(coord[0], coord[1], zoom)
        return (
            (pixel_x - source_min_x) * scale,
            (pixel_y - source_min_y) * scale,
        )

    for feature in (geojson or {}).get("features", []):
        _draw_geojson_feature(draw, feature, project)

    _draw_trail(draw, trail_segments, project)

    if mower_point is not None:
        _draw_mower_marker(draw, project(mower_point))

    _draw_attribution(draw, tile_provider.attribution)

    return _encode(image)


def _render_osm_source(
    zoom: int,
    source_min_x: float,
    source_min_y: float,
    source_width: float,
    source_height: float,
    tile_cache_dir: str | None,
    tile_provider: MapTileProvider,
) -> Image.Image:
    source = Image.new(
        "RGBA",
        (math.ceil(source_width), math.ceil(source_height)),
        BACKGROUND,
    )
    max_tile = (2**zoom) - 1
    min_tile_x = max(math.floor(source_min_x / OSM_TILE_SIZE), 0)
    max_tile_x = min(
        math.floor((source_min_x + source_width) / OSM_TILE_SIZE), max_tile
    )
    min_tile_y = max(math.floor(source_min_y / OSM_TILE_SIZE), 0)
    max_tile_y = min(
        math.floor((source_min_y + source_height) / OSM_TILE_SIZE), max_tile
    )

    allow_download = True
    for tile_x in range(min_tile_x, max_tile_x + 1):
        for tile_y in range(min_tile_y, max_tile_y + 1):
            try:
                tile = _load_osm_tile(
                    zoom,
                    tile_x,
                    tile_y,
                    tile_cache_dir,
                    tile_provider,
                    allow_download=allow_download,
                )
            except _TileProviderUnavailable:
                allow_download = False
                continue
            if tile is None:
                continue
            source.alpha_composite(
                tile.convert("RGBA"),
                (
                    round(tile_x * OSM_TILE_SIZE - source_min_x),
                    round(tile_y * OSM_TILE_SIZE - source_min_y),
                ),
            )
    return source


def _load_osm_tile(
    zoom: int,
    tile_x: int,
    tile_y: int,
    tile_cache_dir: str | None,
    tile_provider: MapTileProvider,
    *,
    allow_download: bool = True,
) -> Image.Image | None:
    cache_path: Path | None = None
    if tile_cache_dir:
        cache_path = Path(tile_cache_dir) / str(zoom) / str(tile_x) / f"{tile_y}.png"
        if cache_path.exists():
            try:
                return Image.open(cache_path).copy()
            except OSError:
                cache_path.unlink(missing_ok=True)

    if not allow_download:
        return None

    tile_url = tile_provider.tile_url(zoom, tile_x, tile_y)
    if not tile_url.startswith("https://"):
        return None
    request = Request(  # noqa: S310 - providers are fixed HTTPS tile services
        tile_url,
        headers={"User-Agent": tile_provider.user_agent},
    )
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            tile_bytes = response.read()
    except (HTTPError, OSError, TimeoutError, URLError) as err:
        raise _TileProviderUnavailable from err

    try:
        tile = Image.open(BytesIO(tile_bytes)).copy()
    except OSError as err:
        raise _TileProviderUnavailable from err

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(tile_bytes)

    return tile


def _draw_attribution(draw: ImageDraw.ImageDraw, attribution: str) -> None:
    """Draw required tile-source attribution on the rendered image."""
    padding = 5
    text_bounds = draw.textbbox((0, 0), attribution)
    width = text_bounds[2] - text_bounds[0]
    height = text_bounds[3] - text_bounds[1]
    left = CANVAS_SIZE[0] - width - (padding * 2)
    top = CANVAS_SIZE[1] - height - (padding * 2)
    draw.rounded_rectangle(
        (left, top, CANVAS_SIZE[0], CANVAS_SIZE[1]),
        radius=4,
        fill=(255, 255, 255, 190),
    )
    draw.text(
        (left + padding, top + padding),
        attribution,
        fill=(35, 35, 35, 230),
    )


def _draw_geojson_feature(
    draw: ImageDraw.ImageDraw, feature: dict[str, Any], project
) -> None:
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    geometry_type = geometry.get("type")
    type_name = str(properties.get("type_name", "")).lower()
    name = properties.get("Name") or properties.get("title")
    stroke, fill = _feature_colours(type_name, properties)

    if geometry_type == "Polygon":
        for ring in geometry.get("coordinates", []):
            polygon = [project((float(coord[0]), float(coord[1]))) for coord in ring]
            if len(polygon) >= 3:
                draw.polygon(polygon, fill=fill, outline=stroke)
                draw.line([*polygon, polygon[0]], fill=stroke, width=3, joint="curve")
        if name and type_name == "area":
            _draw_label(draw, str(name), _centroid(_geometry_points(geometry)), project)
    elif geometry_type == "MultiPolygon":
        for polygon_coordinates in geometry.get("coordinates", []):
            for ring in polygon_coordinates:
                polygon = [
                    project((float(coord[0]), float(coord[1]))) for coord in ring
                ]
                if len(polygon) >= 3:
                    draw.polygon(polygon, fill=fill, outline=stroke)
                    draw.line(
                        [*polygon, polygon[0]], fill=stroke, width=3, joint="curve"
                    )
    elif geometry_type in {"LineString", "MultiLineString"}:
        lines = (
            [geometry.get("coordinates", [])]
            if geometry_type == "LineString"
            else geometry.get("coordinates", [])
        )
        for coordinates in lines:
            if type_name == "trail":
                _draw_trail(
                    draw,
                    _geo_trail_segments(_valid_geo_points(coordinates)),
                    project,
                )
                continue
            line = [
                project((float(coord[0]), float(coord[1]))) for coord in coordinates
            ]
            if len(line) >= 2:
                draw.line(line, fill=stroke, width=4, joint="curve")
    elif geometry_type == "Point":
        coord = geometry.get("coordinates", [])
        if len(coord) < 2:
            return
        center = project((float(coord[0]), float(coord[1])))
        if type_name == "station":
            _draw_station_marker(draw, center)
            if name:
                _draw_text(draw, str(name), (center[0] + 14, center[1] - 8))
            return
        radius = 8 if type_name == "station" else 6
        draw.ellipse(
            (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            ),
            fill=fill,
            outline=stroke,
            width=2,
        )
        if name:
            _draw_text(draw, str(name), (center[0] + 10, center[1] - 6))


def _draw_mower_marker(draw: ImageDraw.ImageDraw, center: tuple[float, float]) -> None:
    """Draw a small Yuka-style mower marker."""
    x, y = center
    width = 24
    height = 17
    outline_width = 1
    draw.rounded_rectangle(
        (
            x - width / 2 - 2,
            y - height / 2 - 2,
            x + width / 2 + 2,
            y + height / 2 + 2,
        ),
        radius=7,
        fill=(0, 0, 0, 65),
    )
    draw.rounded_rectangle(
        (x - width / 2, y - height / 2, x + width / 2, y + height / 2),
        radius=6,
        fill=MOWER_FILL,
        outline=MOWER_STROKE,
        width=outline_width,
    )
    draw.rounded_rectangle(
        (
            x - width * 0.28,
            y - height * 0.18,
            x + width * 0.28,
            y + height * 0.18,
        ),
        radius=3,
        fill=(35, 42, 48, 235),
    )
    wheel_radius = 2
    for wx in (x - width * 0.38, x + width * 0.38):
        draw.ellipse(
            (
                wx - wheel_radius,
                y + height * 0.18 - wheel_radius,
                wx + wheel_radius,
                y + height * 0.18 + wheel_radius,
            ),
            fill=(80, 190, 120, 255),
        )


def _draw_station_marker(draw: ImageDraw.ImageDraw, center: tuple[float, float]) -> None:
    """Draw a compact mower dock/station marker."""
    x, y = center
    half_w = 13
    half_h = 8
    draw.rounded_rectangle(
        (x - half_w, y - half_h, x + half_w, y + half_h),
        radius=5,
        fill=(248, 249, 250, 245),
        outline=(84, 92, 100, 220),
        width=1,
    )
    draw.rounded_rectangle(
        (x - 8, y - 2, x + 8, y + 2),
        radius=2,
        fill=(36, 42, 48, 235),
    )
    draw.ellipse((x + 7, y + 3, x + 11, y + 7), fill=(80, 190, 120, 255))


def _draw_trail(
    draw: ImageDraw.ImageDraw,
    trail_segments: list[list[tuple[float, float]]],
    project,
) -> None:
    total_points = sum(len(segment) for segment in trail_segments)
    if total_points < 2:
        return
    recent_start = max(total_points - TRAIL_RECENT_POINTS, 0)
    seen_points = 0
    for segment in trail_segments:
        if len(segment) < 2:
            seen_points += len(segment)
            continue
        line = [project(point) for point in segment]
        draw.line(line, fill=TRAIL_SHADOW_STROKE, width=7, joint="curve")
        draw.line(line, fill=TRAIL_STROKE, width=4, joint="curve")
        segment_recent_start = max(recent_start - seen_points, 0)
        recent = line[segment_recent_start:]
        if len(recent) >= 2:
            draw.line(recent, fill=TRAIL_RECENT_STROKE, width=5, joint="curve")
        seen_points += len(segment)


def _feature_colours(
    type_name: str, properties: dict[str, Any]
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    if type_name == "area":
        return AREA_STROKE, AREA_FILL
    if type_name in {"obstacle", "visual_obstacle_zone", "visual_safety_zone"}:
        return OBSTACLE_STROKE, OBSTACLE_FILL
    if type_name == "path":
        return PATH_STROKE, (45, 45, 45, 120)
    if type_name == "trail":
        return TRAIL_RECENT_STROKE, TRAIL_STROKE
    if type_name == "station":
        colour = _parse_colour(properties.get("color"), (90, 78, 181, 255))
        return colour, (*colour[:3], 180)
    if type_name == "virtual_wall":
        return VIRTUAL_STROKE, VIRTUAL_STROKE
    stroke = _parse_colour(properties.get("color"), PATH_STROKE)
    fill_colour = _parse_colour(properties.get("fillColor"), (*stroke[:3], 85))
    return stroke, (*fill_colour[:3], min(fill_colour[3], 110))


def _parse_colour(
    value: Any, fallback: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    if not value:
        return fallback
    try:
        return ImageColor.getcolor(str(value), "RGBA")
    except ValueError:
        return fallback


def _draw_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    coord: tuple[float, float] | None,
    project,
) -> None:
    if coord is not None:
        _draw_text(draw, text, project(coord))


def _draw_text(draw: ImageDraw.ImageDraw, text: str, xy: tuple[float, float]) -> None:
    text_bounds = draw.textbbox((0, 0), text)
    text_width = text_bounds[2] - text_bounds[0]
    text_height = text_bounds[3] - text_bounds[1]
    x, y = xy
    padding = 4
    draw.rounded_rectangle(
        (
            x - padding,
            y - padding,
            x + text_width + padding,
            y + text_height + padding,
        ),
        radius=5,
        fill=(255, 255, 255, 210),
        outline=(60, 60, 60, 90),
    )
    draw.text((x, y), text, fill=(30, 30, 30, 255))


def _geo_trail_segments(
    points: list[tuple[float, float]],
) -> list[list[tuple[float, float]]]:
    """Return display-ready trail segments from raw lon/lat samples."""
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    for index, point in enumerate(points):
        if not current:
            current.append(point)
            continue

        distance = _geo_distance_meters(current[-1], point)
        if distance < TRAIL_RENDER_MIN_DISTANCE_METERS:
            continue

        next_point = points[index + 1] if index + 1 < len(points) else None
        if _is_geo_spike(current[-1], point, next_point):
            continue

        if distance > TRAIL_RENDER_MAX_SEGMENT_METERS:
            if (
                next_point is not None
                and _geo_distance_meters(current[-1], next_point)
                <= TRAIL_RENDER_MAX_SEGMENT_METERS
            ):
                continue
            if len(current) >= 2:
                segments.append(_prepare_geo_segment(current))
            current = [point]
            continue

        current.append(point)

    if len(current) >= 2:
        segments.append(_prepare_geo_segment(current))

    return segments


def _is_geo_spike(
    previous: tuple[float, float],
    point: tuple[float, float],
    next_point: tuple[float, float] | None,
) -> bool:
    """Return True for a single GPS sample that darts away then immediately returns."""
    if next_point is None:
        return False
    distance = _geo_distance_meters(previous, point)
    if distance < TRAIL_RENDER_SPIKE_MIN_METERS:
        return False
    next_distance = _geo_distance_meters(previous, next_point)
    return next_distance <= max(TRAIL_RENDER_MIN_DISTANCE_METERS * 2, distance * 0.35)


def _prepare_geo_segment(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return a denoised, simplified, and visually smoothed trail segment."""
    denoised = _denoise_geo_segment(points)
    simplified = _simplify_geo_segment(denoised, TRAIL_RENDER_SIMPLIFY_METERS)
    return _smooth_geo_segment(simplified)


def _denoise_geo_segment(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Dampen short alternating GPS wobble in the display-only trail."""
    if len(points) < TRAIL_RENDER_DENOISE_WINDOW:
        return points

    radius = TRAIL_RENDER_DENOISE_WINDOW // 2
    denoised = []
    for index in range(len(points)):
        start = max(index - radius, 0)
        end = min(index + radius + 1, len(points))
        weighted_lon = 0.0
        weighted_lat = 0.0
        total_weight = 0
        for sample_index, sample in enumerate(points[start:end], start=start):
            weight = radius + 1 - abs(sample_index - index)
            weighted_lon += sample[0] * weight
            weighted_lat += sample[1] * weight
            total_weight += weight
        denoised.append((weighted_lon / total_weight, weighted_lat / total_weight))
    return denoised


def _simplify_geo_segment(
    points: list[tuple[float, float]], tolerance_meters: float
) -> list[tuple[float, float]]:
    """Remove display-only GPS jitter while preserving meaningful turns."""
    if len(points) < 3:
        return points

    max_distance = 0.0
    split_index = 0
    start = points[0]
    end = points[-1]
    for index, point in enumerate(points[1:-1], start=1):
        distance = _geo_perpendicular_distance_meters(point, start, end)
        if distance > max_distance:
            max_distance = distance
            split_index = index

    if max_distance <= tolerance_meters:
        return [start, end]

    first = _simplify_geo_segment(points[: split_index + 1], tolerance_meters)
    second = _simplify_geo_segment(points[split_index:], tolerance_meters)
    return [*first[:-1], *second]


def _smooth_geo_segment(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Round visual corners in a trail segment without changing raw storage."""
    smoothed = points
    for _ in range(TRAIL_RENDER_SMOOTHING_PASSES):
        if len(smoothed) < 3:
            break
        next_points = [smoothed[0]]
        for first, second in zip(smoothed, smoothed[1:], strict=False):
            next_points.extend(
                (
                    _interpolate_geo_point(first, second, 0.25),
                    _interpolate_geo_point(first, second, 0.75),
                )
            )
        next_points.append(smoothed[-1])
        smoothed = next_points
    return smoothed


def _interpolate_geo_point(
    first: tuple[float, float],
    second: tuple[float, float],
    fraction: float,
) -> tuple[float, float]:
    return (
        first[0] + (second[0] - first[0]) * fraction,
        first[1] + (second[1] - first[1]) * fraction,
    )


def _geo_distance_meters(
    first: tuple[float, float], second: tuple[float, float]
) -> float:
    first_lon, first_lat = first
    second_lon, second_lat = second
    mean_lat = (first_lat + second_lat) / 2
    return math.hypot(
        (second_lon - first_lon) / _meters_to_lon_degrees(1.0, mean_lat),
        (second_lat - first_lat) / _meters_to_lat_degrees(1.0),
    )


def _geo_perpendicular_distance_meters(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Return point-to-line distance in a local metre plane."""
    start_x, start_y = 0.0, 0.0
    end_x, end_y = _geo_to_local_meters(end, start)
    point_x, point_y = _geo_to_local_meters(point, start)
    line_dx = end_x - start_x
    line_dy = end_y - start_y
    line_length_squared = line_dx**2 + line_dy**2
    if line_length_squared == 0:
        return math.hypot(point_x - start_x, point_y - start_y)

    projection = max(
        0.0,
        min(
            1.0,
            ((point_x - start_x) * line_dx + (point_y - start_y) * line_dy)
            / line_length_squared,
        ),
    )
    closest_x = start_x + projection * line_dx
    closest_y = start_y + projection * line_dy
    return math.hypot(point_x - closest_x, point_y - closest_y)


def _geo_to_local_meters(
    point: tuple[float, float], origin: tuple[float, float]
) -> tuple[float, float]:
    lon, lat = point
    origin_lon, origin_lat = origin
    mean_lat = (lat + origin_lat) / 2
    return (
        (lon - origin_lon) / _meters_to_lon_degrees(1.0, mean_lat),
        (lat - origin_lat) / _meters_to_lat_degrees(1.0),
    )


def _geometry_points(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, dict):
        return []
    geometry_type = value.get("type")
    if geometry_type == "FeatureCollection":
        return [
            point
            for feature in value.get("features", [])
            for point in _geometry_points(feature)
        ]
    if geometry_type == "Feature":
        return _geometry_points(value.get("geometry") or {})
    if geometry_type == "GeometryCollection":
        return [
            point
            for geometry in value.get("geometries", [])
            for point in _geometry_points(geometry)
        ]
    return _coordinate_points(value.get("coordinates", []))


def _coordinate_points(coordinates: Any) -> list[tuple[float, float]]:
    if (
        isinstance(coordinates, list)
        and len(coordinates) >= 2
        and isinstance(coordinates[0], int | float)
        and isinstance(coordinates[1], int | float)
    ):
        lon = float(coordinates[0])
        lat = float(coordinates[1])
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            return [(lon, lat)]
        return []
    if isinstance(coordinates, list):
        return [point for item in coordinates for point in _coordinate_points(item)]
    return []


def _geo_bounds(points: list[tuple[float, float]]) -> GeoBounds:
    return GeoBounds(
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    sin_lat = math.sin(math.radians(lat))
    world_size = OSM_TILE_SIZE * (2**zoom)
    return (
        (lon + 180.0) / 360.0 * world_size,
        (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi))
        * world_size,
    )


def _meters_to_lat_degrees(meters: float) -> float:
    return meters / 111_111.0


def _meters_to_lon_degrees(meters: float, lat: float) -> float:
    return meters / (111_111.0 * max(math.cos(math.radians(lat)), 0.01))


def _geo_location_point(location: Any | None) -> tuple[float, float] | None:
    if location is None:
        return None
    latitude = getattr(location, "latitude", None)
    longitude = getattr(location, "longitude", None)
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None
    if -90 <= latitude <= 90 and -180 <= longitude <= 180 and (
        latitude != 0 or longitude != 0
    ):
        return longitude, latitude
    return None


def _valid_geo_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    valid_points: list[tuple[float, float]] = []
    for point in points:
        try:
            lon = float(point[0])
            lat = float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if -180 <= lon <= 180 and -90 <= lat <= 90 and (lat != 0 or lon != 0):
            valid_points.append((lon, lat))
    return valid_points


def _encode(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
