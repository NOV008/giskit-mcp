# Qoder Desktop 端到端演示记录

- 日期：2026-11-16
- 客户端：Qoder Desktop，用户级 MCP 连接器 `giskit-mcp`（stdio）
- 服务端：`main.py`（mcp 1.30.0 / FastMCP）
- 数据：真实 Nominatim + Overpass 调用，无 mock

## 提问

> 只使用 giskit-mcp 这个 MCP 服务器提供的工具，不要使用其他数据源，不要编造数据。
>
> 1. 调用 `geocode_place`，name="聊城大学"。
> 2. 用返回的 lat/lon 调用 `search_poi`，category="医院"，radius_m=3000。
> 3. 把 search_poi 返回的数组按 distance_m 升序整理成 Markdown 表格。
> 4. 写明实际调用的工具、关键参数和数据来源。

## 模型实际调用

Qoder 对 MCP 工具使用懒加载包装，会话日志中表现为先用 `mcp_get` 读取 schema，再用 `mcp_call` 真正调用。实际 MCP 工具名如下。

1. `geocode_place`，参数 `name="聊城大学"`

```json
{
  "lat": 36.4318169,
  "lon": 116.005428,
  "display_name": "聊城大学东校区, 1, 聊城大学东校区后街, 东城街道, 东昌府区, 聊城市, 山东省, 252000, 中国"
}
```

2. `search_poi`，参数 `category="医院"`、`lat=36.4318169`、`lon=116.005428`、`radius_m=3000`

```json
[
  {
    "name": "聊城大学东校区医院",
    "lat": 36.4282044,
    "lon": 116.0125651,
    "distance_m": 755.2
  },
  {
    "name": "鲁西骨科医院",
    "lat": 36.421279,
    "lon": 116.0189161,
    "distance_m": 1682.4
  }
]
```

## 模型最终回答

**实际调用的工具**

1. `geocode_place` — name="聊城大学" → lat=36.4318169, lon=116.005428（匹配到：聊城大学东校区）
2. `search_poi` — category="医院", lat=36.4318169, lon=116.005428, radius_m=3000

**3 km 内医院（按 distance_m 升序）**

| 名称 | 距离(m) | 纬度 | 经度 |
| --- | ---: | ---: | ---: |
| 聊城大学东校区医院 | 755.2 | 36.4282044 | 116.0125651 |
| 鲁西骨科医院 | 1682.4 | 36.421279 | 116.0189161 |

数据来源：OpenStreetMap（Nominatim 地理编码 + Overpass API POI 检索）。OSM 覆盖可能不全，未命名要素会以“类别 #类型/id”代替。未调用 `analyze_service_area`。

> 说明：OSM 数据随时间变化，且“聊城大学”在 OSM 中拆分为东/西校区，本记录是当天真实结果；换网络或换时间后数量可能不同。