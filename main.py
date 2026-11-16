"""giskit-mcp: a small GIS toolbox exposed as MCP tools."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP
from pyproj import CRS, Geod, Transformer
from shapely import force_2d
from shapely.geometry import Point, mapping, shape
from shapely.ops import transform as shapely_transform
from shapely.validation import make_valid

USER_AGENT = os.environ.get(
    "GISKIT_USER_AGENT", "giskit-mcp/0.1 (local GIS MCP demo)"
)
# nominatim.openstreetmap.org is DNS-poisoned in some networks; osm.org is the official alias.
NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.osm.org/search"
)
NOMINATIM_MIN_INTERVAL_S = 1.1
OVERPASS_URL = os.environ.get("OVERPASS_URL", "").strip()
# The public main instance is often at capacity; prefer the official dev node and fall back.
OVERPASS_ENDPOINTS = (
    [OVERPASS_URL]
    if OVERPASS_URL
    else [
        "https://dev.overpass-api.de/api_drolbr/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]
)
OVERPASS_MIN_INTERVAL_S = 1.1
OVERPASS_CACHE_TTL_S = 300

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "out"
GEOD = Geod(ellps="WGS84")

mcp = FastMCP("giskit-mcp", log_level="WARNING")
logger = logging.getLogger("giskit_mcp")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_nominatim_lock = threading.Lock()
_last_nominatim_at = 0.0

_overpass_lock = threading.Lock()
_last_overpass_at = 0.0
_poi_cache: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}
_poi_cache_lock = threading.Lock()

_CATEGORY_TAGS: dict[str, tuple[str, str, str]] = {
    "医院": ("amenity", "hospital", "医院"),
    "诊所": ("amenity", "clinic", "诊所"),
    "药店": ("amenity", "pharmacy", "药店"),
    "学校": ("amenity", "school", "学校"),
    "大学": ("amenity", "university", "大学"),
    "高校": ("amenity", "university", "大学"),
    "餐厅": ("amenity", "restaurant", "餐厅"),
    "饭店": ("amenity", "restaurant", "餐厅"),
    "餐馆": ("amenity", "restaurant", "餐厅"),
    "快餐": ("amenity", "fast_food", "快餐"),
    "咖啡": ("amenity", "cafe", "咖啡馆"),
    "咖啡店": ("amenity", "cafe", "咖啡馆"),
    "银行": ("amenity", "bank", "银行"),
    "atm": ("amenity", "atm", "ATM"),
    "取款机": ("amenity", "atm", "ATM"),
    "超市": ("shop", "supermarket", "超市"),
    "便利店": ("shop", "convenience", "便利店"),
    "商场": ("shop", "mall", "商场"),
    "购物中心": ("shop", "mall", "商场"),
    "公园": ("leisure", "park", "公园"),
    "酒店": ("tourism", "hotel", "酒店"),
    "宾馆": ("tourism", "hotel", "酒店"),
    "景点": ("tourism", "attraction", "景点"),
    "景区": ("tourism", "attraction", "景点"),
    "加油站": ("amenity", "fuel", "加油站"),
    "停车场": ("amenity", "parking", "停车场"),
    "公交站": ("highway", "bus_stop", "公交站"),
    "公交车站": ("highway", "bus_stop", "公交站"),
    "客运站": ("amenity", "bus_station", "客运站"),
    "地铁站": ("railway", "station", "地铁站"),
    "派出所": ("amenity", "police", "派出所"),
    "公安局": ("amenity", "police", "派出所"),
    "图书馆": ("amenity", "library", "图书馆"),
}
_CATEGORY_ALIASES = {
    "hospital": "医院",
    "clinic": "诊所",
    "pharmacy": "药店",
    "drugstore": "药店",
    "school": "学校",
    "university": "大学",
    "college": "大学",
    "restaurant": "餐厅",
    "fast_food": "快餐",
    "cafe": "咖啡",
    "bank": "银行",
    "supermarket": "超市",
    "convenience": "便利店",
    "mall": "商场",
    "park": "公园",
    "hotel": "酒店",
    "attraction": "景点",
    "fuel": "加油站",
    "gas_station": "加油站",
    "parking": "停车场",
    "bus_stop": "公交站",
    "bus_station": "客运站",
    "metro": "地铁站",
    "subway": "地铁站",
    "police": "派出所",
    "library": "图书馆",
}


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}


def _error_from_exception(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ToolError):
        return exc.payload()
    return {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": f"{type(exc).__name__}: {exc}",
        }
    }


def _nominatim_search(name: str) -> list[dict[str, Any]]:
    global _last_nominatim_at
    params = {
        "q": name,
        "format": "jsonv2",
        "limit": 10,
        "addressdetails": 1,
        "namedetails": 1,
        "accept-language": "zh-CN,zh,en",
    }
    with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _last_nominatim_at)
        if wait > 0:
            time.sleep(wait)
        try:
            response = _session.get(NOMINATIM_URL, params=params, timeout=(10, 25))
        except requests.Timeout as exc:
            raise ToolError(
                "UPSTREAM_TIMEOUT",
                "Nominatim 请求超时。",
                "稍后重试；若持续失败，检查网络或改用更短的地名。",
            ) from exc
        except requests.RequestException as exc:
            raise ToolError(
                "UPSTREAM_ERROR",
                f"连接 Nominatim 失败：{exc}",
                "检查网络与代理设置。",
            ) from exc
        finally:
            _last_nominatim_at = time.monotonic()

    if response.status_code == 429:
        raise ToolError(
            "RATE_LIMITED",
            "Nominatim 返回 429（请求过快）。",
            "该服务要求每秒最多 1 次请求，请稍后再试。",
        )
    if response.status_code >= 400:
        raise ToolError(
            "UPSTREAM_ERROR",
            f"Nominatim 返回 HTTP {response.status_code}。",
            "稍后重试。",
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolError("UPSTREAM_ERROR", "Nominatim 返回了非 JSON 内容。") from exc
    if not isinstance(data, list):
        raise ToolError("UPSTREAM_ERROR", "Nominatim 返回格式异常。")
    return data


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _place_score(query: str, result: dict[str, Any], rank: int) -> float:
    normalized_query = _normalize_text(query)
    names: list[str] = [result.get("name") or ""]
    names.extend(str(v) for v in (result.get("namedetails") or {}).values())
    display = result.get("display_name") or ""
    names.append(display.split(",")[0])

    best = 0.0
    for raw_name in names:
        candidate = _normalize_text(str(raw_name))
        if not candidate or not normalized_query:
            continue
        if candidate == normalized_query:
            score = 100.0
        elif normalized_query in candidate:
            score = 85.0 - min(15.0, (len(candidate) - len(normalized_query)) * 0.25)
        elif candidate in normalized_query:
            score = 70.0
        else:
            query_chars = set(normalized_query)
            overlap = len(query_chars & set(candidate)) / len(query_chars)
            score = 40.0 * overlap
        best = max(best, score)

    if normalized_query and normalized_query in _normalize_text(display):
        best = max(best, 80.0)
    best += min(float(result.get("importance") or 0.0), 1.0) * 0.1
    return best - rank * 0.01


def _pick_best_place(query: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        enumerate(results),
        key=lambda item: _place_score(query, item[1], item[0]),
    )[1]


@mcp.tool()
def geocode_place(name: str) -> dict[str, Any]:
    """把地名或地址解析为经纬度（数据源：OpenStreetMap Nominatim）。

    参数:
        name: 地名或地址，中文或英文均可，例如 "聊城大学"、"Liaocheng University"。

    成功返回:
        {"lat": float, "lon": float, "display_name": str}
        lat/lon 为 WGS84 十进制度。从多个候选中按名称相似度选最佳匹配，
        因此“聊城大学”可能返回“聊城大学东校区”，用它做周边检索仍在同一区域。

    失败返回:
        {"error": {"code": str, "message": str, "hint": str?}}
    """
    name = name.strip()
    if not name:
        return _error_from_exception(
            ToolError("INVALID_ARGUMENT", "地名不能为空。", "例如：聊城大学")
        )
    try:
        data = _nominatim_search(name)
        if not data:
            raise ToolError(
                "PLACE_NOT_FOUND",
                f"未找到地名：{name}",
                "尝试更完整的地址，例如“山东省聊城市东昌府区”。",
            )
        best = _pick_best_place(name, data)
        return {
            "lat": float(best["lat"]),
            "lon": float(best["lon"]),
            "display_name": best.get("display_name", name),
        }
    except Exception as exc:
        return _error_from_exception(exc)


_RAW_TAG_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z0-9_:.\-]+)$")


def _resolve_category(category: str) -> tuple[str, str, str]:
    if not isinstance(category, str) or not category.strip():
        raise ToolError(
            "INVALID_ARGUMENT",
            "category 不能为空。",
            "例如：医院、学校、餐厅，或原始标签 amenity=clinic。",
        )
    raw = category.strip()
    alias = _CATEGORY_ALIASES.get(raw.lower())
    if alias:
        return _CATEGORY_TAGS[alias]
    if raw in _CATEGORY_TAGS:
        return _CATEGORY_TAGS[raw]
    match = _RAW_TAG_RE.match(raw)
    if match:
        key, value = match.groups()
        return key, value, f"{key}={value}"
    supported = "、".join(
        [
            "医院",
            "诊所",
            "药店",
            "学校",
            "大学",
            "餐厅",
            "银行",
            "超市",
            "公园",
            "酒店",
            "加油站",
            "公交站",
            "停车场",
        ]
    )
    raise ToolError(
        "INVALID_CATEGORY",
        f"不支持的类别：{category}",
        f"可用类别：{supported}；也可传原始 OSM 标签，如 amenity=clinic。",
    )


def _overpass_throttle() -> None:
    global _last_overpass_at
    with _overpass_lock:
        wait = OVERPASS_MIN_INTERVAL_S - (time.monotonic() - _last_overpass_at)
        if wait > 0:
            time.sleep(wait)
        _last_overpass_at = time.monotonic()


def _poi_cache_get(cache_key: tuple[Any, ...]) -> list[dict[str, Any]] | None:
    now = time.time()
    with _poi_cache_lock:
        item = _poi_cache.get(cache_key)
        if item is None:
            return None
        created_at, value = item
        if now - created_at > OVERPASS_CACHE_TTL_S:
            _poi_cache.pop(cache_key, None)
            return None
        return value


def _poi_cache_set(cache_key: tuple[Any, ...], value: list[dict[str, Any]]) -> None:
    with _poi_cache_lock:
        if len(_poi_cache) >= 32:
            oldest = min(_poi_cache, key=lambda key: _poi_cache[key][0])
            _poi_cache.pop(oldest, None)
        _poi_cache[cache_key] = (time.time(), value)


def _extract_poi_name(
    tags: dict[str, Any], osm_type: str, osm_id: Any, label: str
) -> str:
    for key in ("name:zh", "name:zh-Hans", "name", "brand", "operator"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{label} #{osm_type}/{osm_id}"


def _overpass_fetch(
    key: str, value: str, lat: float, lon: float, radius_m: float, label: str
) -> list[dict[str, Any]]:
    cache_key = (key, value, round(lat, 4), round(lon, 4), round(radius_m))
    cached = _poi_cache_get(cache_key)
    if cached is not None:
        return cached

    query = (
        "[out:json][timeout:25];"
        f'nwr["{key}"="{value}"](around:{radius_m:g},{lat:.7f},{lon:.7f});'
        "out center tags;"
    )
    last_error: ToolError | None = None

    for endpoint_index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        for attempt in range(2):
            _overpass_throttle()
            try:
                response = _session.post(
                    endpoint,
                    data={"data": query},
                    headers={"Accept": "application/json"},
                    timeout=(10, 25),
                )
            except requests.Timeout:
                last_error = ToolError(
                    "UPSTREAM_TIMEOUT",
                    f"Overpass 查询超过 25 秒（{endpoint}）。",
                    "缩小半径或换用更常见的类别；也可稍后重试。",
                )
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                break
            except requests.RequestException as exc:
                last_error = ToolError(
                    "UPSTREAM_ERROR",
                    f"连接 Overpass 失败（{endpoint}）：{exc}",
                    "检查网络后重试。",
                )
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                break

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    last_error = ToolError(
                        "UPSTREAM_ERROR", f"Overpass 返回非 JSON 内容（{endpoint}）。"
                    )
                    break
                if not isinstance(data, dict) or not isinstance(
                    data.get("elements"), list
                ):
                    last_error = ToolError(
                        "UPSTREAM_ERROR", f"Overpass 返回格式异常（{endpoint}）。"
                    )
                    break

                pois: list[dict[str, Any]] = []
                for element in data["elements"]:
                    if not isinstance(element, dict):
                        continue
                    osm_type = str(element.get("type") or "")
                    osm_id = element.get("id")
                    if "lat" in element and "lon" in element:
                        poi_lat = element.get("lat")
                        poi_lon = element.get("lon")
                    else:
                        center = element.get("center") or {}
                        poi_lat = center.get("lat")
                        poi_lon = center.get("lon")
                    if poi_lat is None or poi_lon is None:
                        continue
                    tags = element.get("tags") or {}
                    if not isinstance(tags, dict):
                        tags = {}
                    pois.append(
                        {
                            "name": _extract_poi_name(tags, osm_type, osm_id, label),
                            "lat": float(poi_lat),
                            "lon": float(poi_lon),
                        }
                    )
                _poi_cache_set(cache_key, pois)
                return pois

            if response.status_code in (429, 500, 502, 503, 504):
                code = "RATE_LIMITED" if response.status_code == 429 else "UPSTREAM_ERROR"
                last_error = ToolError(
                    code,
                    f"Overpass 返回 HTTP {response.status_code}（{endpoint}）。",
                    "公共实例可能繁忙或限流，稍后重试或缩小查询范围。",
                )
                if attempt == 0:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 1.5
                    except ValueError:
                        delay = 1.5
                    time.sleep(min(max(delay, 1.5), 10.0))
                    continue
                break

            raise ToolError(
                "UPSTREAM_ERROR",
                f"Overpass 拒绝查询（HTTP {response.status_code}）：{response.text[:200]}",
                "检查类别标签是否为合法 OSM 标签。",
            )

        if endpoint_index < len(OVERPASS_ENDPOINTS) - 1:
            logger.warning(
                "Overpass endpoint %s failed; trying fallback %s",
                endpoint,
                OVERPASS_ENDPOINTS[endpoint_index + 1],
            )

    if last_error is not None:
        raise last_error
    raise ToolError("UPSTREAM_ERROR", "没有可用的 Overpass 端点。")


def _search_poi_impl(
    category: str, lat: float, lon: float, radius_m: float
) -> tuple[list[dict[str, Any]], str]:
    lat, lon = _validated_coordinates(lat, lon)
    radius_m = _validated_radius(radius_m)
    key, value, label = _resolve_category(category)
    raw_pois = _overpass_fetch(key, value, lat, lon, radius_m, label)

    results = []
    for poi in raw_pois:
        _, _, distance_m = GEOD.inv(lon, lat, poi["lon"], poi["lat"])
        results.append(
            {
                "name": poi["name"],
                "lat": poi["lat"],
                "lon": poi["lon"],
                "distance_m": round(distance_m, 1),
            }
        )
    results.sort(key=lambda item: (item["distance_m"], item["name"]))
    return results, label


# Success is a list, failure is an error envelope; skip FastMCP's list output validation.
@mcp.tool(structured_output=False)
def search_poi(
    category: str, lat: float, lon: float, radius_m: float
) -> str:
    """检索某点附近指定类别的 POI，按距离升序返回（数据源：OpenStreetMap Overpass API）。

    参数:
        category: 类别。支持中文常用类别（医院、诊所、药店、学校、大学、餐厅、银行、
                  超市、公园、酒店、加油站、公交站、停车场等），也支持英文别名
                  （hospital、school 等）或原始 OSM 标签，如 "amenity=clinic"。
        lat: 中心点纬度（WGS84）。
        lon: 中心点经度（WGS84）。
        radius_m: 检索半径（米），范围 (0, 50000]。

    成功返回（JSON 数组文本）:
        [{"name": str, "lat": float, "lon": float, "distance_m": float}, ...]
        已按 distance_m 升序。数据来自 OSM，覆盖可能不全；未命名要素用
        "类别 #类型/id" 作为 name。
    失败返回:
        {"error": {"code": str, "message": str, "hint": str?}}

    网络策略:
        单次查询超时 25 秒；失败自动重试 1 次；官方节点繁忙时自动切换备用
        Overpass 节点；相同查询 5 分钟内使用内存缓存。请勿高频调用公共 API。
    """
    try:
        results, _ = _search_poi_impl(category, lat, lon, radius_m)
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            _error_from_exception(exc), ensure_ascii=False, indent=2
        )


def _validated_coordinates(lat: float, lon: float) -> tuple[float, float]:
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError) as exc:
        raise ToolError("INVALID_ARGUMENT", "lat/lon 必须是数字。") from exc
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ToolError("INVALID_ARGUMENT", "lat/lon 必须是有限数字。")
    if not -90 <= lat <= 90:
        raise ToolError("INVALID_ARGUMENT", f"lat 超出 [-90, 90]：{lat}")
    if not -180 <= lon <= 180:
        raise ToolError("INVALID_ARGUMENT", f"lon 超出 [-180, 180]：{lon}")
    return lat, lon


def _validated_radius(radius_m: float) -> float:
    try:
        radius_m = float(radius_m)
    except (TypeError, ValueError) as exc:
        raise ToolError("INVALID_ARGUMENT", "radius_m 必须是数字。") from exc
    if not math.isfinite(radius_m) or not 0 < radius_m <= 50_000:
        raise ToolError(
            "INVALID_ARGUMENT",
            f"radius_m 必须在 (0, 50000] 米内：{radius_m}",
        )
    return radius_m


def _local_aeqd(lat: float, lon: float) -> tuple[Transformer, Transformer]:
    local_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
    )
    to_local = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(local_crs, "EPSG:4326", always_xy=True)
    return to_local, to_wgs84


def _buffer_geometry(lat: float, lon: float, radius_m: float):
    _, to_wgs84 = _local_aeqd(lat, lon)
    circle = Point(0, 0).buffer(radius_m, quad_segs=64)
    return shapely_transform(to_wgs84.transform, circle)


@mcp.tool()
def make_buffer(lat: float, lon: float, radius_m: float) -> dict[str, Any]:
    """在 (lat, lon) 周围生成指定半径的测地缓冲圆，返回 GeoJSON Polygon。

    参数:
        lat: 圆心纬度（WGS84，十进制度）。
        lon: 圆心经度（WGS84，十进制度）。
        radius_m: 半径（米），范围 (0, 50000]。

    实现:
        以圆心建立局部方位等距投影（AEQD），在投影平面内生成 64 段/象限的圆，
        再反投影回 WGS84。城市尺度下与椭球测地距离误差可忽略。

    成功返回:
        GeoJSON Polygon 几何对象：{"type": "Polygon", "coordinates": [[[lon, lat], ...]]}
    失败返回:
        {"error": {"code": str, "message": str, "hint": str?}}
    """
    try:
        lat, lon = _validated_coordinates(lat, lon)
        radius_m = _validated_radius(radius_m)
        geom = _buffer_geometry(lat, lon, radius_m)
        return json.loads(json.dumps(mapping(geom)))
    except Exception as exc:
        return _error_from_exception(exc)


def _read_geojson(path: str) -> tuple[Path, int, list[str], list[Any]]:
    file_path = Path(path).expanduser()
    if not file_path.is_absolute():
        file_path = (BASE_DIR / file_path).resolve()
    else:
        file_path = file_path.resolve()

    if not file_path.exists():
        raise ToolError(
            "FILE_NOT_FOUND",
            f"文件不存在：{file_path}",
            "传入绝对路径，或相对项目根目录的路径。",
        )
    if not file_path.is_file():
        raise ToolError("INVALID_ARGUMENT", f"不是文件：{file_path}")
    try:
        obj = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("INVALID_GEOJSON", f"读取或解析 GeoJSON 失败：{exc}") from exc
    if not isinstance(obj, dict):
        raise ToolError("INVALID_GEOJSON", "GeoJSON 顶层必须是对象。")

    obj_type = obj.get("type")
    geometries: list[dict[str, Any]] = []
    geometry_types: list[str] = []
    feature_count = 0

    if obj_type == "FeatureCollection":
        features = obj.get("features")
        if not isinstance(features, list):
            raise ToolError("INVALID_GEOJSON", "FeatureCollection.features 必须是数组。")
        feature_count = len(features)
        for index, feature in enumerate(features):
            if not isinstance(feature, dict):
                raise ToolError("INVALID_GEOJSON", f"第 {index} 个 feature 不是对象。")
            geometry = feature.get("geometry")
            if geometry is None:
                continue
            if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
                raise ToolError("INVALID_GEOJSON", f"第 {index} 个 feature 的 geometry 无效。")
            geometries.append(geometry)
            geometry_types.append(geometry["type"])
    elif obj_type == "Feature":
        geometry = obj.get("geometry")
        if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
            raise ToolError("INVALID_GEOJSON", "Feature.geometry 无效。")
        feature_count = 1
        geometries.append(geometry)
        geometry_types.append(geometry["type"])
    elif isinstance(obj_type, str) and (
        "coordinates" in obj or obj_type == "GeometryCollection"
    ):
        feature_count = 1
        geometries.append(obj)
        geometry_types.append(obj_type)
    else:
        raise ToolError(
            "INVALID_GEOJSON",
            "不是 FeatureCollection、Feature 或 Geometry。",
        )

    parsed = []
    for geometry in geometries:
        try:
            geom = force_2d(shape(geometry))
        except Exception as exc:
            raise ToolError("INVALID_GEOMETRY", f"无法解析几何：{exc}") from exc
        if geom.is_empty:
            continue
        if not geom.is_valid:
            geom = make_valid(geom)
        parsed.append(geom)

    return file_path, feature_count, sorted(set(geometry_types)), parsed


@mcp.tool()
def geojson_stats(path: str) -> dict[str, Any]:
    """读取本地 GeoJSON 文件，统计要素数、几何类型、包围盒和总面积。

    参数:
        path: GeoJSON 文件路径。相对路径按项目根目录解析。
              支持 FeatureCollection、Feature 或单个 Geometry。

    面积:
        假定坐标为 WGS84 经纬度。按全部要素包围盒的中心经度自动选择 UTM 分区，
        投影后计算面积并求和；输出 m² 和 km²。多个面重叠时按要素分别累加。
        跨多个 UTM 分区的大范围数据存在投影误差。

    成功返回:
        {"path": str, "feature_count": int, "geometry_types": [str],
         "bbox": [minx, miny, maxx, maxy] | null,
         "area_m2": float, "area_km2": float, "utm_zone": str | null}
    失败返回:
        {"error": {"code": str, "message": str, "hint": str?}}
    """
    try:
        file_path, feature_count, geometry_types, geoms = _read_geojson(path)

        bbox = None
        if geoms:
            boxes = [geom.bounds for geom in geoms]
            bbox = [
                round(min(box[0] for box in boxes), 7),
                round(min(box[1] for box in boxes), 7),
                round(max(box[2] for box in boxes), 7),
                round(max(box[3] for box in boxes), 7),
            ]

        area_m2 = 0.0
        utm_zone = None
        if bbox is not None:
            center_lon = (bbox[0] + bbox[2]) / 2
            center_lat = (bbox[1] + bbox[3]) / 2
            zone = min(60, max(1, int((center_lon + 180) // 6) + 1))
            south = center_lat < 0
            epsg = (32700 if south else 32600) + zone
            utm_zone = f"{zone}{'S' if south else 'N'}"
            to_utm = Transformer.from_crs(
                "EPSG:4326", CRS.from_epsg(epsg), always_xy=True
            )
            area_m2 = sum(
                abs(shapely_transform(to_utm.transform, geom).area) for geom in geoms
            )

        return {
            "path": str(file_path),
            "feature_count": feature_count,
            "geometry_types": geometry_types,
            "bbox": bbox,
            "area_m2": round(area_m2, 2),
            "area_km2": round(area_m2 / 1_000_000, 6),
            "utm_zone": utm_zone,
        }
    except Exception as exc:
        return _error_from_exception(exc)


_DIRECTION_SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _short_name(name: str, limit: int = 16) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _summary_lines(label: str, pois: list[dict[str, Any]], counts: dict[str, int]) -> list[str]:
    if not pois:
        return [
            "POI 数量：0",
            f"该范围内没有检索到{label}",
            "方位：N 0 · NE 0 · E 0 · SE 0",
            "      S 0 · SW 0 · W 0 · NW 0",
        ]
    nearest = pois[0]
    farthest = pois[-1]
    mean_distance = sum(poi["distance_m"] for poi in pois) / len(pois)
    return [
        f"POI 数量：{len(pois)}",
        f"最近：{_short_name(nearest['name'])} · {nearest['distance_m']:.0f} m",
        f"最远：{_short_name(farthest['name'])} · {farthest['distance_m']:.0f} m",
        f"平均距离：{mean_distance:.0f} m",
        "方位：N {N} · NE {NE} · E {E} · SE {SE}".format(**counts),
        "      S {S} · SW {SW} · W {W} · NW {NW}".format(**counts),
    ]


def _render_service_area_png(
    png_path: Path,
    buffer_geom,
    center_lat: float,
    center_lon: float,
    radius_m: float,
    label: str,
    pois: list[dict[str, Any]],
    direction_counts: dict[str, int],
    summary_lines: list[str],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    for font_name in ("SimHei", "Microsoft YaHei", "Noto Sans CJK SC", "Arial Unicode MS"):
        if font_name in available_fonts:
            plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
            break
    else:
        logger.warning("No CJK font found; Chinese labels may render as boxes.")
    plt.rcParams["axes.unicode_minus"] = False

    to_local, _ = _local_aeqd(center_lat, center_lon)
    buffer_local = shapely_transform(to_local.transform, buffer_geom)
    buffer_x, buffer_y = buffer_local.exterior.xy

    fig = plt.figure(figsize=(9.6, 9.0), dpi=160, facecolor="#f5f4f0")
    ax = fig.add_axes([0.08, 0.08, 0.84, 0.79])
    ax.set_facecolor("#fbfaf7")
    ax.fill(buffer_x, buffer_y, color="#2F6B8F", alpha=0.10, zorder=1)
    ax.plot(buffer_x, buffer_y, color="#2F6B8F", lw=2.0, zorder=2)

    for fraction in (1 / 3, 2 / 3):
        ring_radius = radius_m * fraction
        ring = plt.Circle(
            (0, 0),
            ring_radius,
            fill=False,
            color="#9AA7A3",
            lw=0.8,
            ls=(0, (3, 3)),
            zorder=1,
        )
        ax.add_patch(ring)
        label_x = ring_radius * 0.707
        ax.text(
            label_x,
            label_x,
            f"{ring_radius / 1000:.1f} km",
            fontsize=7.5,
            color="#6B7A75",
            ha="left",
            va="bottom",
            zorder=3,
        )

    ax.plot(
        0,
        0,
        marker="*",
        markersize=18,
        color="#1F3A4A",
        markeredgecolor="white",
        markeredgewidth=1.2,
        zorder=6,
    )
    ax.annotate(
        "中心",
        xy=(0, 0),
        xytext=(8, -14),
        textcoords="offset points",
        fontsize=8.5,
        color="#1F3A4A",
        zorder=7,
    )

    if pois:
        xs = []
        ys = []
        for poi in pois:
            x, y = to_local.transform(poi["lon"], poi["lat"])
            xs.append(x)
            ys.append(y)
        ax.scatter(
            xs,
            ys,
            s=68,
            c="#E76F51",
            edgecolors="white",
            linewidths=1.1,
            zorder=5,
            label=f"{label}（{len(pois)}）",
        )
        offsets = [(9, 10), (9, -16), (-9, 10), (-9, -16), (14, 0)]
        for index, poi in enumerate(pois[:5]):
            x, y = to_local.transform(poi["lon"], poi["lat"])
            offset_x, offset_y = offsets[index % len(offsets)]
            ax.annotate(
                f"{_short_name(poi['name'], 14)}\n{poi['distance_m']:.0f} m",
                xy=(x, y),
                xytext=(offset_x, offset_y),
                textcoords="offset points",
                fontsize=7.6,
                color="#2B2B2B",
                ha="left" if offset_x >= 0 else "right",
                va="center",
                arrowprops={"arrowstyle": "-", "color": "#7A7A7A", "lw": 0.6},
                zorder=7,
            )
        ax.legend(
            loc="lower left",
            fontsize=8.5,
            frameon=True,
            framealpha=0.92,
            edgecolor="#DCD9CF",
        )
    else:
        ax.text(
            0,
            0,
            f"未检索到{label}",
            ha="center",
            va="center",
            fontsize=12,
            color="#7A8783",
            zorder=5,
        )

    limit = radius_m * 1.13
    ax.set_xlim(-limit, limit)
    ax.set_ylim(-limit, limit)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("东向距离 (m)", fontsize=10)
    ax.set_ylabel("北向距离 (m)", fontsize=10)
    ax.grid(True, color="#E3E0D8", lw=0.6, zorder=0)
    ax.tick_params(labelsize=8, colors="#5C6B66")
    for spine in ax.spines.values():
        spine.set_color("#D5D2C8")

    ax.annotate(
        "",
        xy=(0, limit * 0.98),
        xytext=(0, limit * 0.86),
        arrowprops={"arrowstyle": "-|>", "color": "#1F3A4A", "lw": 1.2},
        zorder=8,
    )
    ax.text(
        0,
        limit * 1.01,
        "N",
        ha="center",
        va="bottom",
        fontsize=12,
        color="#1F3A4A",
    )

    fig.suptitle(
        f"{label} · {radius_m / 1000:g} km 服务区分析",
        x=0.5,
        y=0.975,
        fontsize=18,
        color="#1F3A4A",
    )
    fig.text(
        0.5,
        0.932,
        f"中心 ({center_lat:.5f}, {center_lon:.5f}) · OpenStreetMap 数据",
        ha="center",
        fontsize=9,
        color="#6B7A75",
    )

    ax.text(
        0.015,
        0.985,
        "\n".join(summary_lines),
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=8.8,
        color="#22333B",
        linespacing=1.55,
        bbox={
            "boxstyle": "round,pad=0.45",
            "facecolor": "white",
            "edgecolor": "#DCD9CF",
            "alpha": 0.94,
        },
        zorder=10,
    )

    polar = fig.add_axes([0.76, 0.10, 0.165, 0.165], projection="polar")
    polar.set_theta_zero_location("N")
    polar.set_theta_direction(-1)
    angles = [math.radians(index * 45) for index in range(8)]
    counts = [direction_counts[sector] for sector in _DIRECTION_SECTORS]
    polar.bar(
        angles,
        counts,
        width=math.radians(38),
        color="#4E8C84",
        alpha=0.85,
        edgecolor="white",
        linewidth=0.6,
    )
    polar.set_xticks(angles)
    polar.set_xticklabels(_DIRECTION_SECTORS, fontsize=7)
    polar.set_yticks([])
    polar.set_ylim(0, max(1.0, max(counts) * 1.15))
    polar.grid(True, color="#E3E0D8", lw=0.6)
    polar.set_title("方位分布", fontsize=9, color="#1F3A4A", pad=6)
    polar.set_facecolor("#FBFAF7")
    for spine in polar.spines.values():
        spine.set_color("#DCD9CF")

    fig.text(
        0.98,
        0.02,
        "Data © OpenStreetMap contributors (ODbL) · Nominatim / Overpass",
        ha="right",
        fontsize=7.2,
        color="#7A8783",
        fontfamily="DejaVu Sans",
    )
    fig.savefig(png_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


@mcp.tool()
def analyze_service_area(
    lat: float, lon: float, radius_m: float, category: str
) -> dict[str, Any]:
    """分析某点的服务区：检索半径内 POI，统计距离与八方位分布，并输出 GeoJSON 和 PNG。

    参数:
        lat: 中心点纬度（WGS84）。
        lon: 中心点经度（WGS84）。
        radius_m: 分析半径（米），范围 (0, 50000]。
        category: POI 类别，与 search_poi 相同（如 "医院"、"学校"、amenity=clinic）。

    输出文件（写到项目 out/ 目录）:
        out/service_area_<类别>_<半径>m_<时间戳>.geojson
            包含中心点、缓冲面、全部 POI 点（含距离与方位）及统计元数据。
        out/service_area_<类别>_<半径>m_<时间戳>.png
            服务区地图：缓冲区、POI、最近点标注、统计框、八方位玫瑰图、OSM 署名。

    成功返回:
        {"category": str, "category_label": str,
         "center": {"lat": float, "lon": float}, "radius_m": float,
         "count": int,
         "nearest": {"name": str, "distance_m": float} | null,
         "farthest": {"name": str, "distance_m": float} | null,
         "mean_distance_m": float | null,
         "direction_counts": {"N": int, "NE": int, "E": int, "SE": int,
                              "S": int, "SW": int, "W": int, "NW": int},
         "outputs": {"geojson": str, "png": str}}
    失败返回:
        {"error": {"code": str, "message": str, "hint": str?}}
    """
    try:
        lat, lon = _validated_coordinates(lat, lon)
        radius_m = _validated_radius(radius_m)
        _, tag_value, label = _resolve_category(category)
        pois, _ = _search_poi_impl(category, lat, lon, radius_m)

        to_local, _ = _local_aeqd(lat, lon)
        direction_counts = {sector: 0 for sector in _DIRECTION_SECTORS}
        for poi in pois:
            x, y = to_local.transform(poi["lon"], poi["lat"])
            bearing = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
            direction = _DIRECTION_SECTORS[int(((bearing + 22.5) % 360.0) // 45)]
            poi["bearing_deg"] = round(bearing, 1)
            poi["direction"] = direction
            direction_counts[direction] += 1

        created_at = datetime.now().astimezone()
        stamp = created_at.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        slug = re.sub(r"[^a-z0-9]+", "_", tag_value.lower()).strip("_") or "poi"
        stem = f"service_area_{slug}_{int(round(radius_m))}m_{stamp}"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        geojson_path = OUT_DIR / f"{stem}.geojson"
        png_path = OUT_DIR / f"{stem}.png"

        buffer_geom = _buffer_geometry(lat, lon, radius_m)
        features: list[dict[str, Any]] = [
            {
                "type": "Feature",
                "properties": {"role": "center"},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            },
            {
                "type": "Feature",
                "properties": {"role": "buffer", "radius_m": radius_m},
                "geometry": json.loads(json.dumps(mapping(buffer_geom))),
            },
        ]
        for poi in pois:
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "name": poi["name"],
                        "distance_m": poi["distance_m"],
                        "bearing_deg": poi["bearing_deg"],
                        "direction": poi["direction"],
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [poi["lon"], poi["lat"]],
                    },
                }
            )

        nearest = (
            {"name": pois[0]["name"], "distance_m": pois[0]["distance_m"]}
            if pois
            else None
        )
        farthest = (
            {"name": pois[-1]["name"], "distance_m": pois[-1]["distance_m"]}
            if pois
            else None
        )
        mean_distance = (
            round(sum(poi["distance_m"] for poi in pois) / len(pois), 1)
            if pois
            else None
        )
        geojson = {
            "type": "FeatureCollection",
            "properties": {
                "category": category,
                "category_label": label,
                "center": {"lat": lat, "lon": lon},
                "radius_m": radius_m,
                "count": len(pois),
                "nearest": nearest,
                "farthest": farthest,
                "mean_distance_m": mean_distance,
                "direction_counts": direction_counts,
                "attribution": "Data © OpenStreetMap contributors (ODbL); Nominatim + Overpass",
                "created_at": created_at.isoformat(timespec="seconds"),
            },
            "features": features,
        }
        try:
            geojson_path.write_text(
                json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _render_service_area_png(
                png_path=png_path,
                buffer_geom=buffer_geom,
                center_lat=lat,
                center_lon=lon,
                radius_m=radius_m,
                label=label,
                pois=pois,
                direction_counts=direction_counts,
                summary_lines=_summary_lines(label, pois, direction_counts),
            )
        except OSError as exc:
            raise ToolError(
                "WRITE_ERROR",
                f"写出结果失败：{exc}",
                f"确认 {OUT_DIR} 可写。",
            ) from exc

        return {
            "category": category,
            "category_label": label,
            "center": {"lat": lat, "lon": lon},
            "radius_m": radius_m,
            "count": len(pois),
            "nearest": nearest,
            "farthest": farthest,
            "mean_distance_m": mean_distance,
            "direction_counts": direction_counts,
            "outputs": {
                "geojson": geojson_path.relative_to(BASE_DIR).as_posix(),
                "png": png_path.relative_to(BASE_DIR).as_posix(),
            },
        }
    except Exception as exc:
        return _error_from_exception(exc)


if __name__ == "__main__":
    mcp.run()