# LifeSmart Home Assistant Custom Component

LifeSmart 智能家居平台的 Home Assistant 自定义集成组件，通过 LifeSmart 云端 API 和 WebSocket 实时同步设备状态。

## 功能特性

- **开关控制** — 支持多种 LifeSmart 开关设备（SL_SW/SL_SF/SL_OL 等系列）
- **灯光控制** — 支持 RGB/RGBW 彩灯及普通灯开关
- **空调/暖气控制** — 支持空调（V_AIR_P）和地暖（SL_CP_DN），含模式切换、温度设定、风速调节
- **窗帘控制** — 支持 Dooya 窗帘电机，可开合及定位
- **传感器** — 支持温湿度、光照、电池、燃气、空气质量等传感器
- **二值传感器** — 支持门磁、人体感应、烟雾报警等
- **红外遥控** — 通过 `send_keys` / `send_ackeys` 服务发送红外指令
- **实时状态** — 通过 WebSocket 长连接实时接收设备状态变更

## 支持的设备类型

| 平台 | 设备类型 | 说明 |
|------|---------|------|
| switch | SL_SW/SL_SF/SL_OL 系列 | 智能开关 |
| light | MSL_IRCTL/OD_WE_IRCTL/SL_SPOT | 红外遥控灯/Spot 灯 |
| light | SL_OL_W/SL_SW_IF 系列 | 灯开关 |
| climate | V_AIR_P | 空调 |
| climate | SL_CP_DN | 地暖 |
| cover | SL_DOOYA | 窗帘 |
| sensor | SL_SC_WA/SL_SC_CH/SL_SC_CP/ELIQ_EM | 燃气/电力传感器 |
| sensor | SL_SC_THL/SL_SC_BE/SL_SC_CQ | 环境传感器 |
| sensor | SL_SC_MHW/SL_SC_BM/SL_SC_G/SL_SC_BG | 温湿度/光照传感器 |
| binary_sensor | SL_SC_G/SL_SC_BG | 门磁 |
| binary_sensor | SL_SC_MHW/SL_SC_BM/SL_SC_CM | 人体感应 |
| binary_sensor | SL_P_A | 烟雾报警 |

## 安装

将 `lifesmart` 文件夹复制到 Home Assistant 的 `custom_components/` 目录下：

```bash
mkdir -p custom_components/lifesmart
cp -r lifesmart/* custom_components/lifesmart/
```

重启 Home Assistant。

## 2025+/2026 适配说明

针对 Home Assistant 2025.12 / 2026.12 的适配改造，设备类型与配置方式保持不变：

- **websocket-client 回调签名修复**：适配 websocket-client ≥ 1.x 的 `on_close(ws, code, msg)` 签名，修复断线后线程崩溃导致的重连风暴
- **依赖声明**：`websocket-client` 已写入 `manifest.json` requirements，不再依赖其他组件顺带安装
- **实时状态重构**：WebSocket `io` 推送改为按 `(devtype, agt, me, idx)` 分发到实体对象（`async_write_ha_state`），不再直写状态机，实体不存在时只记 debug 日志，不再 NoneType 崩溃
- **新增 `CONFIG_SCHEMA` 校验**：`appkey`/`apptoken`/`username`/`password` 为必填项
- **认证与设备拉取容错**：登录 / do_auth / EpGetAll 失败会明确报错并停止集成，不再抛 TypeError
- **HA 停止事件**：监听 `EVENT_HOMEASSISTANT_STOP` 关闭 WebSocket 线程
- **实体增强**：新增 `unique_id` 和设备注册表条目（`DeviceInfo`），实体 ID 命名格式保持不变，现有自动化引用不受影响
- **传感器修复**：`device_class`/单位不再使用字符串 `"None"`；OT 类传感器（SL_SC_*）只创建 V/Z/P3/P4 有效通道；P3/P4 单位迁移为 `UnitOfRatio.PARTS_PER_MILLION` / `UnitOfDensity`（2026+）或旧常量（≤2025）
- **灯光修复**：补全 `ColorMode.HS` 声明；未设颜色时开灯不再崩溃（回退为电源开启）；红外遥控列表存在时才声明 EFFECT 特性
- **版本规范（26.10.31.1 起）**：采用 CalVer + 当日序号（`年.月.日.序号`），当日多次发布时序号递增
- **设备注册表接入**：平台加载从旧式 `discovery.async_load_platform` 迁移为标准 `async_forward_entry_setups`，设备与实体正确注册到配置条目和设备注册表——「设置 → 设备与服务」中 LifeSmart 卡片可见设备，可按设备分组、整组分配区域、查看设备诊断；实体 ID 与 unique_id 保持不变，现有自动化引用不受影响

## 配置

支持两种配置方式：**WebUI 添加集成**（设置 → 设备与服务 → 添加集成 → 搜 lifesmart），或在 `configuration.yaml` 中添加（首次启动自动导入为配置条目，之后 YAML 修改会在重启时同步到该条目）。

### 在线模式（mode: online，云端 API）

```yaml
lifesmart:
  mode: online            # 可省略，默认 online
  appkey: YOUR_APPKEY
  apptoken: YOUR_APPTOKEN
  username: YOUR_LIFESMART_ACCOUNT
  password: YOUR_LIFESMART_PASSWORD
  exclude:
    - DEVICE_ME_TO_EXCLUDE
```

### 离线模式（mode: offline，局域网直连智慧中心）

```yaml
lifesmart:
  host: 192.168.1.100     # 智慧中心（Smart Station）局域网 IP，按实际填写
  mode: offline
  fallback: false         # 站点不可达时是否回退云端，默认不启用
  local_username: admin                 # 站点本地管理员名（中心管理账号）
  local_password: YOUR_CENTER_PASSWORD  # 中心管理密码（App 里设置的那个）
  # fallback: true 时还需填写 appkey/apptoken 和云端账号 username/password
  exclude:
    - DEVICE_ME_TO_EXCLUDE
```

### 配置项

| 参数 | 必填 | 说明 |
|------|------|------|
| `host` | offline 必填 | 智慧中心局域网 IP（TCP 8888） |
| `mode` | 否 | `online`（云端 API，默认）/ `offline`（局域网直连） |
| `fallback` | 否 | offline 模式下站点不可达时回退云端，默认 `false` |
| `username` | online 必填 | 云端 LifeSmart 账号（如手机号）；仅在线模式或启用 fallback 时需要，offline 纯本地模式可不填 |
| `password` | online 必填 | 云端 LifeSmart 密码；同上 |
| `local_username` | offline 必填 | 站点本地管理员名（默认 admin） |
| `local_password` | offline 必填 | 中心管理密码（App 里设置的那个） |
| `appkey`/`apptoken` | online 必填 | 开发者凭据；offline 仅启用 fallback 时需要 |
| `exclude` | 否 | 需要排除的设备 ME 标识列表 |

### 模式说明

- **offline（局域网）**：设备列表、实时状态推送、开关/灯/窗帘/空调控制全部经站点本地完成（TCP 8888），断外网可用，毫秒级响应；红外遥控通过站端场景（`RunA`）本地触发
- **online（云端）**：原云端 REST + WebSocket 通道，红外遥控列表查询（light 的 effect_list）仅此模式支持
- **fallback**：offline 模式下站点连接失败自动登录云端（`username`/`password`）并接管控制与状态推送，站点恢复后自动切回本地
- **数值一致性**：本地模式与云端的最终状态值一致——温湿度等通道云端 `v = val/10`，本地同样按 `val/10` 还原；电池百分比在本地协议中只有原始电量值，按云端实测样本（val 127→0%、144→25%、145→50%）拟合 `v = clamp(25×(val−143), 0, 100)` 还原，与云端显示一致

## 图标

集成在 HA 前端的图标分两层：

- **实体/服务图标（当前版本即生效）**：仓库根目录的 `icons.json` 为各平台实体提供默认图标（开关 `mdi:power`、灯光 `mdi:lightbulb`、空调 `mdi:air-conditioner`、窗帘 `mdi:curtains`、传感器 `mdi:eye`/`mdi:motion-sensor`），并为 `lifesmart.send_keys`/`send_ackeys` 服务提供图标。实体未显式设置图标或 device_class 时使用这些默认值。
- **集成磁贴图标（Settings → 设备与服务 中的 LifeSmart 标志）**：
  - HA **2026.3+**：直接读取集成目录下的 `brand/` 文件夹（本仓库已内置 `icon.png`/`logo.png` 及 dark/@2x 共 8 个文件），升级后自动生效，无需任何配置
  - HA **< 2026.3**：磁贴图标只能来自 brands CDN（`brands.home-assistant.io`）。若需在旧版本显示，可向 [home-assistant/brands](https://github.com/home-assistant/brands) 仓库的 `custom_integrations/lifesmart/` 提交 PR（`brand/` 内素材取自 LifeSmart 官方应用图标，可直接使用）

## 服务

集成注册了以下服务用于红外遥控：

### `lifesmart.send_keys`

发送红外按键指令。

| 字段 | 说明 |
|------|------|
| `agt` | 网关 AGT |
| `me` | 设备 ME |
| `ai` | 红外设备 AI |
| `category` | 类别 |
| `brand` | 品牌 |
| `keys` | 按键列表 |

### `lifesmart.send_ackeys`

发送空调红外按键指令（含温度、模式、风速等参数）。

| 字段 | 说明 |
|------|------|
| `agt` | 网关 AGT |
| `me` | 设备 ME |
| `ai` | 红外设备 AI |
| `category` | 类别 |
| `brand` | 品牌 |
| `keys` | 按键列表 |
| `power` | 电源状态 |
| `mode` | 运行模式 |
| `temp` | 目标温度 |
| `wind` | 风速 |
| `swing` | 摆风 |

## 技术实现

- 通过 LifeSmart REST API 进行设备认证和控制
- 通过 WebSocket（`wss://api.ilifesmart.com:8443/wsapp/`）实时接收设备状态推送
- 使用 MD5 签名机制进行 API 鉴权
- 内置 WebSocket 断线自动重连机制

## 版本

当前版本：**26.10.31.1**（版本规范：CalVer + 当日序号 `年.月.日.序号`；品牌素材为 LifeSmart 官方图标）

## 作者

@skyzhishui
