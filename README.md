# 邻里缓行 · 适老步行路线助手（MVP）

给老人及家属用的「平缓优先」步行路线助手：从指定起点去**社区菜店**或**社区医院**，
输出少台阶、少陡坡、少过街的路线，并给出可读的风险提示与备选方案。

- **离线优先**：默认 `offline_demo=true`，不联网、不需要 Key，`data/` 下的样例 CSV 就能跑通完整主链路。
- **在线可选**：配置高德 Key 后可获取在线 POI，网络异常自动降级回离线；路线仍由本地路网计算。
- **CLI 永远可用**：Streamlit / Folium / 网络 / Key 任意一项缺失，CLI 仍输出完整文字路线。
- **不编造数据**：样例数据标注 `source=样例合成`、`confidence=low`，凡是未实测的结论都写「假设/待验证」。

GitHub 仓库名：`gentle-walk`。

---

## 1. 快速开始（零配置，离线）

```bash
# 目标 Python 3.9—3.12；安装核心依赖
pip install -r requirements-core.txt

# 自检：确认 AGENTS.md 已加载、依赖、配置、样例数据、Key 状态
python -m src.selfcheck
python -m src.selfcheck --data        # 把数据缺失视为失败，适合 CI

# 主链路：默认起点（片区中心）→ 最近的社区菜店
python -m src.cli
python -m src.cli --category clinic
python -m src.cli --from 108.9440,34.2610
python -m src.cli --list --category clinic
python -m src.cli --destination clinic_002
```

输出示例（离线模式，数值以实际运行结果为准）：

```text
【离线演示】起点：108.9455,34.2611
· 已在 5 个社区菜店中比较，推荐「钟楼东生鲜超市」（评分 0.00）。
· 离线演示模式：数据来自 data/ 下的样例 CSV，无需网络与 AMAP_KEY
· 起点距最近路网节点约 11 米，实际步行距离会略长。

== 推荐路线 ==
目的地：社区菜店-钟楼东生鲜超市（样例街 45 号）
总距离约 167 米，预计步行 3 分钟，评分 0.00（越低越平缓）
台阶 0 级（0 段）、最大坡度 0.0%、过街 0 次、沿途休息点 2 个
路线步骤：
  1. 南向步行约 167 米

== 风险提示 ==
[低] 未发现明显障碍：本路线未触发台阶、陡坡、过街等风险项，仍建议以现场情况为准（来源：样例合成，置信度：low）

== 备选路线 ==
备选 1：距离约 443 米、台阶 8 级、最大坡度 0.0%、过街 0 次、评分 11.40
...
数据来源声明：样例数据由人工编写并标注为「样例合成」，坐标近似，仅用于本项目演示，实际路况与设施以现场为准。
```

> 评分越小越平缓。评分单位是「**等效分钟**」：把台阶、坡度、过街折算成多走几分钟，方便和步行时间直接比较。

---

## 2. 高德 Key 的三种设置方式（任选其一，推荐方式一）

Key 只在需要在线数据时使用；**离线演示完全不需要 Key**。

| 方式 | 命令 / 位置 | 说明 |
| --- | --- | --- |
| 方式一（推荐） | `python -m src.cli --set-key` | 写入系统钥匙串（macOS Keychain / Windows 凭据管理器 / Secret Service），不回显、不进仓库 |
| 方式二 | 环境变量 `export AMAP_KEY=你的Key` | 适合临时会话或 CI 密钥注入 |
| 方式三 | 项目根目录 `.env` 里写 `AMAP_KEY=...` | 已加入 `.gitignore`；POSIX 上通过私有文件保存，其他平台建议使用环境变量或钥匙串 |

其他命令：

```bash
python -m src.cli --key-status     # 查看来源与打码后的值（只显示 **** + 末 4 位）
python -m src.cli --clear-key      # 删除已保存的 Key
python -m src.cli --online         # 尝试在线高德（需 Key 与网络）
```

**CLI 读取优先级**（高 → 低）：环境变量 → 系统钥匙串 → `.env` 文件。
均未配置时：打印「未配置」和设置指引，并自动走离线模式。

> 安全边界：不提交 `.env` 与真实 Key；日志/输出只出现打码值；`.env.example` 里只有占位符。

---

## 3. 目录结构

```text
gentle-walk/
├── AGENTS.md                     # 项目规范（唯一事实来源）
├── README.md
├── .env.example                  # Key 与开关的示例模板（不含真实值）
├── requirements-core.txt         # 离线主链路依赖
├── requirements-optional.txt     # 在线/地图/坐标/钥匙串等可选依赖
├── pytest.ini
├── config/
│   ├── default.json              # 片区 bbox、评分权重、风险阈值、四叉树参数
│   └── beilin.json               # 碑林区专用配置（OSM 路网 + 样例 POI）
├── data/                         # 离线样例数据（标注「样例合成」）
│   ├── pois_greengrocer.csv      # 5 个社区菜店
│   ├── pois_clinic.csv           # 3 个社区医院
│   ├── obstacles.csv             # 6 个障碍点
│   ├── rest_points.csv           # 4 个休息点
│   ├── graph_nodes.csv           # 16 个路网节点
│   ├── graph_edges.csv           # 24 条路网边
│   └── osm_beilin/               # 碑林区路网（OpenStreetMap，ODbL）
├── src/
│   ├── cli.py                    # 命令行入口（argparse，永远可用）
│   ├── service.py                # 编排：数据源 → 建图 → 规划 → 风险
│   ├── config.py                 # 配置加载 + 环境变量覆盖 + bbox 四分
│   ├── keys.py                   # AMAP_KEY 四层解析、打码、钥匙串/.env 读写
│   ├── models.py                 # 数据模型与 walk_type 常量
│   ├── data_loader.py            # CSV 逐行容错加载（utf-8/utf-8-sig/gbk）
│   ├── graph_builder.py          # 节点/边 → networkx 图，障碍挂边
│   ├── scoring.py                # 等效分钟评分（台阶/坡度/过街/疲劳/休息点）
│   ├── router.py                 # networkx 最短路 + 备选方案 + 文字步骤
│   ├── risk.py                   # 风险分级与提示
│   ├── selfcheck.py              # 自检（依赖/配置/数据/Key）
│   ├── ui_streamlit.py           # 可选网页界面（未安装 Streamlit 时只打印提示）
│   ├── geo/
│   │   ├── geo_utils.py          # haversine、局部米制坐标、折线距离
│   │   └── coord_transform.py    # WGS84/GCJ02/BD09（不自写公式）
│   └── adapters/
│       ├── amap_client.py        # 超时、429 退避、哈希缓存
│       ├── amap_poi.py           # POI 关键字搜索 + 四叉树拆分
│       ├── amap_walking.py       # 步行规划 2.0，walk_type 映射
│       ├── online.py             # 在线数据集组装（失败即降级）
│       └── osm_roads.py          # OSM 路网抽取（Overpass 分块 → 项目 CSV）
└── tests/                        # 离线回归测试（数量以 pytest 输出为准）
```

---

## 4. 数据字典

所有 CSV 统一 **UTF-8** 编码（读取时依次尝试 `utf-8`、`utf-8-sig`、`gbk`）。
坐标一律 **WGS84**，`coord_system` 字段必填；缺失或转换失败的行进入「待复核」，不参与规划。
单条坏数据只跳过该行并记入 `data_issues`，不会让整体加载失败。

### 4.1 `pois_greengrocer.csv` / `pois_clinic.csv`（目的地）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | str | ✅ | 唯一编号，如 `shop_001` / `clinic_001` |
| `name` | str | ✅ | 店名/机构名 |
| `category` | str | ✅ | `greengrocer` 或 `clinic` |
| `lon` / `lat` | float | ✅ | WGS84 经纬度 |
| `coord_system` | str | ✅ | `wgs84` / `gcj02` / `bd09`（含别名） |
| `address` | str |  | 门牌地址 |
| `open_hours` | str |  | 营业时间 |
| `has_ramp` | bool/空 |  | 入口有无坡道；空 = 未知（风险提示会标注未知） |
| `source` | str | ✅ | `现场` / `街景` / `访谈` / `高德样例` / `样例合成` |
| `confidence` | str | ✅ | `high` / `medium` / `low` |
| `updated_at` | str |  | 数据更新时间 |
| `notes` | str |  | 备注（样例数据写「虚构演示数据，非真实采集」） |

### 4.2 `obstacles.csv`（障碍点）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` / `name` | str | ✅ | 编号与名称 |
| `type` | str | ✅ | `stairs` / `slope` / `crossing` / `construction` / `wet` / `no_railing` / `narrow` / `curb` |
| `lon` / `lat` / `coord_system` | — | ✅ | 同上 |
| `severity` | int | ✅ | 1–3，评分按 2 / 4 / 8 分钟计 |
| `step_count` | int |  | 台阶数；缺失按 8 级默认值估算并提示 |
| `slope_pct` | float |  | 坡度百分比；缺失按 6% 默认值估算并提示 |
| `length_m` | float |  | 影响长度（米） |
| `source` / `confidence` / `notes` | str | ✅/✅/ | 来源与可信度 |

### 4.3 `rest_points.csv`（休息点）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `name` | str | 编号与名称 |
| `type` | str | `bench` / `pavilion` / `shop_front` / `square` 等 |
| `lon` / `lat` / `coord_system` | — | 坐标 |
| `seats` | int | 座位数（可为空） |
| `source` / `confidence` / `notes` | str | 来源与可信度 |

### 4.4 `graph_nodes.csv` / `graph_edges.csv`（路网）

| 文件 | 字段 | 说明 |
| --- | --- | --- |
| nodes | `node_id, lon, lat, coord_system, source` | 步行路网节点 |
| edges | `u, v, length_m, walk_type, step_count, slope_pct, surface, source` | 无向边；`walk_type` 见下表 |

`walk_type`（来自高德 `WalkStep.walk_type`，AGENTS.md 硬性数字）：

| 值 | 含义 | 评分处理 |
| --- | --- | --- |
| 0 | 普通步道 | 只计时间 |
| 3 | 人行横道 | +2.5 分钟 |
| 9 | 扶梯 | 只计时间（提示可用） |
| 10 | 直梯 | 只计时间（提示可用） |
| **21** | **阶梯** | 0.5 + 0.9 × 台阶数（缺失按 8 级 ≈ 7.7 分钟） |
| **22** | **斜坡** | 3.0 + 0.6 × 坡度%，坡度 ≥ 8% 再 ×1.5（缺失按 6% ≈ 6.6 分钟） |

---

## 5. 路线评分规则（等效分钟）

对每条边先算分，再把整条路线的分数累加；单位都是「多花几分钟」，可与步行时间直接相加比较。

| 项 | 公式 | 参数位置 |
| --- | --- | --- |
| 步行时间 | `距离 / 0.9 m/s / 60` | `scoring.senior_speed_mps`（假设/待验证） |
| 台阶 | `0.5 + 0.9 × 台阶数`，缺失用 8 级 | `stairs_*` |
| 斜坡 | `3.0 + 0.6 × 坡度%`，≥8% 再 ×1.5，缺失用 6% | `slope_*` |
| 过街 | 每次 +2.5 | `crossing_cost` |
| 障碍 | 严重度 1/2/3 → +2 / +4 / +8（与道路类型重复时只计一次） | `obstacle_by_severity` |
| 疲劳 | 连续 >200 米无休息点，每段 +3.0；数据里完全没有休息点时不判疲劳 | `fatigue_*` |
| 休息点奖励 | 沿途每个 −1.0，最多 −5.0 | `rest_*` |
| 目的地坡道 | 有坡道 −1.5；无坡道且路线有台阶 +2.0 | `ramp_bonus` / `no_ramp_stairs_penalty` |

路线总评分 = 各边分数之和 + 疲劳 − 休息奖励 − 坡道奖励 + 无坡道惩罚，**下限 0**。
寻路本身用 `networkx.shortest_path`（权重即上面的分数），备选方案用 `nx.shortest_simple_paths`
按分数升序取前 3 条，再排序展示主路线 + 最多 2 条备选——不自研寻路算法。

风险分级（`config/default.json` 的 `risk` 段，可调）：

| 等级 | 触发条件 |
| --- | --- |
| 🔴 高 | 台阶 ≥ 11 级、或最大坡度 ≥ 8%、或过街 > 3 次、或障碍严重度 = 3 |
| 🟠 中 | 有台阶、或坡度 > 5%、或过街 2–3 次、或存在无护栏路段/疲劳段/障碍点/目的地无坡道 |
| 🟢 低 | 以上都不触发（仍提示「以现场为准」） |

---

## 6. 高德在线 adapter（默认不启用）

```bash
export AMAP_KEY=你的Key        # 或 python -m src.cli --set-key
python -m src.cli --online     # 或把 config 里 offline_demo 设为 false / 环境变量 OFFLINE_DEMO=false
```

- **POI 搜索**：`GET https://restapi.amap.com/v3/place/text`，多关键词并查
  （菜店：菜市场/蔬菜店/生鲜超市/便民菜店/瓜果蔬菜；医院：社区卫生服务站/卫生服务中心/社区医院）。
- **四叉树拆分**：单区域结果数 ≥ **180** 时四分递归；终止条件为「低于阈值」、
  「网格跨度 < **0.005 度**（约 500 米）」、「深度 > **6**」；单页 25 条，单叶子最多 8 页（≈200 条上限）。
- **请求预算**：单类查询最多 **60 次逻辑请求**（`MAX_REQUESTS`），用于限制递归查询成本；
  HTTP 重试另外受客户端重试配置限制。
- **步行规划**：`GET https://restapi.amap.com/v5/direction/walking`，解析 `WalkStep`，
  `walk_type` 21/22 直接进入台阶/坡度惩罚。
- **限流与缓存**：HTTP 429 读 `Retry-After`，否则按 1/2/4/8/16 秒退避，最多 5 次；
  业务层限流码（`10021` CUQPS 并发超限等）同样退避重试；
  相同查询（路径 + 参数，**不含 Key**）用 sha256 命名缓存，命中不再打网络。
- **坐标**：高德返回 GCJ02，入库前统一转换（使用可用的坐标转换后端）；转换库缺失时整条进入「待复核」，
  不把未纠偏坐标用于路线。

### 6.1 接口适用范围与限制

`place/text` 的矩形参数不能作为本项目片区过滤的可靠依据。四叉树查询设有请求预算，
但无法据此保证区域内 POI 的全量覆盖。需要区级限定时，使用第 11.5 节的行政区检索与边界裁剪流程。
`place/polygon` 是可进一步评估的区域检索方案，其参数与实际支持范围需按高德文档验证。

在线模式当前只替换目的地清单，步行路线仍由本地路网计算。步行规划适配器保留了
接口调用和步骤解析能力，尚未接入主规划流程。POI 超出本地路网吸附范围时无法生成路线。

---

## 7. 降级矩阵（无网络 / 无 Key / 缺依赖）

| 缺失项 | 行为 |
| --- | --- |
| 网络 | 离线演示数据，CLI 提示「离线演示模式」 |
| `AMAP_KEY` | 同上，并提示 `--set-key` |
| 429 / 超时 / 5xx | 重试后仍失败 → 降级离线，保留「下一步」 |
| `pyproj` | 仅影响投影换算，不影响主链路 |
| `geotransform` / `coordTransform` | 两者都缺时 GCJ02/BD09 数据进入「待复核」，不猜坐标；装上任一个即可纠偏 |
| `streamlit` | `--web` 打印安装指引，CLI 文字输出完整 |
| `folium` / 瓦片 | 页面降级为文字步骤 + 风险清单 |
| `keyring` | POSIX 上 `--set-key` 可改存私有 `.env`；其他平台使用环境变量 |
| 单条坏数据 | 跳过该行并记入 `data_issues`，整体不失败 |
| 空数据 / 无路线 | 输出「下一步」建议，退出码 0（参数错误才是 2） |

---

## 8. 自检与测试

```bash
python -m src.selfcheck          # 依赖、配置、数据、Key 全览；有警告也返回 0
python -m src.selfcheck --data   # 数据缺失视为失败（CI 用）
python -m src.selfcheck --strict # 警告也视为失败
python -m pytest -q              # 全部离线回归用例
```

覆盖矩阵（AGENTS.md 要求逐项对应）：正常路线、坐标偏差、无路线、数据缺字段、空数据、
无网络降级、无 Key 降级、UTF-8/UTF-8-sig/GBK 读取、Streamlit 不可用时 CLI 输出、
参数错误退出码、Key 打码不泄漏、四叉树阈值与终止条件、`walk_type` 21/22 映射。

---

## 9. 已知限制与「假设/待验证」

1. **Python 兼容性**：目标为 Python 3.9—3.12；代码采用兼容语法，完整版本运行矩阵仍待验证。
2. **可选依赖**：网页、地图及坐标转换能力由可选依赖提供，缺失时按第 7 节降级。
   完整前端与地图加载运行矩阵仍待验证；`pyproj` 用于投影换算，GCJ02/BD09 纠偏需要相应转换后端。
3. **数据读取**：为支持逐行容错、编码回退及精确问题定位，当前使用标准库 `csv`；
   相关行为由 `src/data_loader.py` 和测试覆盖，pandas 保留为可选依赖。
4. **评分为启发式**：步速、台阶与坡度惩罚系数均为初始假设，需现场调研后校准。
5. **样例数据**：默认片区的 8 个目的地、6 个障碍、4 个休息点及 16 节点/24 边路网
   均为人工编写，标注 `source=样例合成`、`confidence=low`，仅供演示。
6. **POI 覆盖范围**：关键词搜索、请求预算和区域过滤可能限制结果覆盖，不能保证检索全量；
   目的地需落在本地路网吸附范围内才能规划。
7. **在线模式**：在线 POI 与本地路网组合使用，实际覆盖和细节取决于所选数据集。
8. **路线安全**：路网吸附不能证明起终点之间的接驳可步行；未验证路段会提示现场确认。
   本工具用于演示和路线比较，路况、入口及设施信息以现场为准。

---

## 10. 数据来源声明

- POI 数据来自高德开放平台（在线模式），仅用于本项目演示，坐标已转换为 WGS84。
- **碑林区路网来自 OpenStreetMap**（© OpenStreetMap contributors，ODbL 1.0）；
  衍生 CSV 再分发时须保留该归属声明。
- **碑林区 POI 来自高德开放平台**（按行政区 adcode=610103 检索，坐标已转 WGS84），
  仅用于本项目演示，遵守高德开放平台条款。
- 离线样例数据由人工编写并标注「样例合成」，非真实采集。
- 本工具不提供医疗建议；去医院的路线仅解决「怎么走更平缓」。

---

## 11. 碑林区路网：OpenStreetMap（公开数据，不请求高德）

碑林区路网使用公开数据库，在线抓取默认关闭。路网采用
**OpenStreetMap**（© OpenStreetMap contributors，ODbL 1.0），高德只保留 POI/步行规划适配器且默认关闭。

### 11.1 数据获取方式

| 方式 | 状态 | 适用说明 |
| --- | --- | --- |
| Overpass API 分块查询 | 已提供独立适配器 | 按行政区获取道路标签与节点；需要网络，可用 `--endpoint` 指定镜像 |
| 已保存的原始 JSON | 已支持 `--raw` | 离线重建 CSV，无需再次请求地图服务 |
| 区域 PBF 文件与 pyosmium | 可评估的替代方案 | 适合数据归档与批量处理，需要额外的数据下载和抽取流程 |

镜像可达性取决于运行环境。无法连接时使用已有 CSV 或原始 JSON，继续离线演示。

### 11.2 随项目提供的碑林区数据

- 行政区：OSM relation **3226088**（`admin_level=6`，名称「碑林区」）
- bbox：`108.9049687, 34.2318173, 109.0015821, 34.2700743`（约 9.0 km × 4.2 km）
- 路网：**9,116 个节点、10,405 段、总长约 405.8 公里**（`data/osm_beilin/`）
- 台阶 **152 段**、过街 **158 段**；未知台阶数保留为未知并使用默认惩罚
- **2,241 段**标记 `pedestrian_uncertain=1`，提示无法确认人行道，需现场核实

```bash
# 联网获取路网（请求数量随片区和配置变化）
python -m src.adapters.osm_roads --district 碑林区 --out data/osm_beilin \
    --include-motor-roads --save-raw

# 已有原始 JSON 时离线重建（不打网络）
python -m src.adapters.osm_roads --raw cache/osm/osm_network_XXXX_raw.json \
    --out data/osm_beilin --include-motor-roads

# 用碑林区配置跑路线
python -m src.cli --config config/beilin.json --list
python -m src.cli --config config/beilin.json --from 108.9455,34.2611 --category clinic
```

### 11.3 标签映射与裁剪规则

| OSM 标签 | 项目字段 | 说明 |
| --- | --- | --- |
| `highway=steps` | `walk_type=21` | `step_count` 能解析就填，否则留空按默认惩罚 |
| `footway=crossing` / `highway=crossing` | `walk_type=3` | 过街风险 |
| `incline=10%` / `ramp=yes` | `walk_type=22` | 坡度；只写 `up/down` 时用保守默认 6% |
| `surface=*` | `surface` | 进入路面风险提示 |
| `access=private/no` 或 `foot=no/private/use_sidepath` | 排除 | 不进入步行路网 |
| `highway=construction/proposed` | 排除 | 在建/规划道路 |
| `highway=primary/secondary/tertiary` | 无可信人行道信息时默认排除 | 显式开启 `--include-motor-roads` 后保留未知标记，并提示现场确认 |

- **多边形裁剪**：先按 way 预筛，再按原始相邻节点裁剪；跨边界边可能保留，边界附近路线需现场确认；
- **bbox 降级裁剪**：`--no-polygon` 仅保留两个原始端点都在 bbox 内的路段，可能省略跨边界连接；
- 多边形由 relation 的 `outer` 环拼接，`subarea` 作洞处理；
- 同一对节点被多条 way 复用时只保留一段，防止重复边。

### 11.4 已知限制

1. **POI 已改为高德区级抓取**（见 11.5）；障碍点/休息点仍是样例合成（区内各 3 条、2 条）。
2. **OSM 细节标签稀疏**：碑林区 152 段台阶全部没有 `step_count`，`incline`/`handrail` 也几乎为空，
   评分只能用默认中等惩罚，风险提示会写「细节未标注，以现场为准」。
3. **机动车道取舍**：`--include-motor-roads` 是为了连通性，代价是可能沿车行道；默认关闭。
4. **数据时效**：镜像快照时间 2026-08-10（`osm3s.timestamp_osm_base`），不是实时路况。
5. **边界附近仍有越界可能**：跨边界边（一端在区内）会保留用于连通，路线在边界附近可能短距离贴边行走。
6. **许可**：ODbL 1.0，再分发/衍生数据必须保留 © OpenStreetMap contributors 归属声明。

### 11.5 POI：高德按需少量抓取（省着用，不滥用）

路网用 OSM，但 OSM 在西安的社区菜店/卫生服务站覆盖不足；为控制高德请求量，
POI 用高德**区级限定**检索，查询受预算约束；成功响应进入哈希缓存，相同查询优先使用缓存。

- **区级限定**：`place/text` + `city=610103` + `citylimit=true`（碑林区 adcode）。
  并结合本地边界过滤；矩形参数限制见第 6.1 节。
- **请求预算**：每关键词最多 3 页、单类目请求上限 12 次（`--max-pages` / `--budget`）。
- **关键词**：菜店「菜市场 / 生鲜超市 / 蔬菜店」；医院「社区卫生服务站 / 社区医院」。
- **类型过滤**：按高德 `type` 字段再筛一遍（如「购物服务;综合市场;蔬菜市场」「医疗保健服务;综合医院;卫生院」）。
- **坐标**：GCJ02 → WGS84 统一转换（`coordTransform`），失败进待复核、不混入 CSV。
- **边界裁剪**：按碑林区多边形裁剪（`data/osm_beilin/boundary.json`），区外 POI 直接丢弃。
- **连通性过滤**：`--require-connected` 会剔除「最近路网节点在孤立小岛」的 POI——
  该校验用于排除缺少主路网连接的候选目的地，校验失败时停止导入。

当前随项目提供的目的地数据：

| 类目 | 数量 | 来源 |
| --- | --- | --- |
| 社区菜店 | **111** | 高德开放平台 |
| 社区医院 | **16** | 高德开放平台 |

目的地坐标统一为 WGS84，并按碑林区边界和主连通路网筛选；来源和更新时间见 CSV 记录。
上述数据量属于提供的数据集，后续查询结果会随服务数据与配置变化。

边界判定使用约 **1 米容差**（`BOUNDARY_TOLERANCE_DEG`），用于处理紧贴边界的坐标舍入误差。
该容差是项目启发式规则，边界与设施位置仍需现场核实。

```bash
# 获取并更新 POI（成功、非空且完整时才替换 CSV）
python -m src.adapters.amap_pois --district 碑林区 \
    --ring-json data/osm_beilin/boundary.json \
    --require-connected --config config/beilin.json --write

# 只试算不写盘（默认）
python -m src.adapters.amap_pois --district 碑林区 --ring-json data/osm_beilin/boundary.json
```

> 边界文件 `data/osm_beilin/boundary.json` 由 OSM 原始数据导出（3.7 KB），
> 有了它，重新抓 POI 不需要再下载路网。缺 Key/断网时本命令报错退出，不影响离线演示。
>
> 请求超时时，可通过环境变量调整超时和重试上限；仍不可用时继续使用离线数据：
>
> ```bash
> AMAP_TIMEOUT_S=25 AMAP_MAX_RETRIES=4 python -m src.adapters.amap_pois --district 碑林区 \
>     --ring-json data/osm_beilin/boundary.json \
>     --require-connected --config config/beilin.json --write
> ```
>
> Key 写入 `.env`（已 `.gitignore`，权限 600）可用交互脚本，输入不回显、只打印打码值：
>
> ```bash
> python scripts/set_amap_key.py           # 交互输入
> python scripts/set_amap_key.py --check   # 只看当前状态
> ```
