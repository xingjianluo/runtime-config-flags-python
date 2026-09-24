# Runtime Config & Feature Flags（Python / FastAPI）

在**不重启服务**的前提下更新运行时配置与功能开关，并保证：

- 多来源按**稳定优先级**合并，每个取值都能解释来自哪个来源；
- 缺项 / 类型不符 / 越界 / 格式非法的配置被**整体拒绝**，错误类型可区分，绝不静默沿用默认值；
- 刷新失败**保留上一份可用配置**；并发请求只可能看到完整的旧版本或完整的新版本，不混合、不丢失；
- 功能开关支持启用 / 停用 / 按比例 / 按分组放量，**同一请求内多次判定结果一致**；
- 未定义开关、已废弃开关、敏感配置项均有明确处理，**任何对外输出都不泄露敏感值**。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `app/errors.py` | 可区分的错误类型（缺项/类型/越界/格式/来源） |
| `app/schema.py` | 字段与开关规格、校验、不可变快照 `Snapshot`、脱敏 |
| `app/sources.py` | 来源加载（文件/环境变量/API）与稳定合并、来源追踪 |
| `app/manager.py` | 线程安全的原子快照、刷新与失败回退 |
| `app/flags.py` | 开关判定（哈希分桶/分组）、请求内一致性、可解释依据 |
| `app/web.py` | FastAPI 入口与错误到 HTTP 的映射 |
| `tests/` | 优先级、非法拒绝、回退、并发一致性、HTTP 集成测试 |

## 一、配置来源与优先级

优先级**从低到高**，高优先级整体覆盖低优先级的同名键：

| 优先级 | 来源 | 名称 | 说明 |
| --- | --- | --- | --- |
| 0 | 规格内默认值 | `defaults` | 仅对**非必填**字段生效；必填字段无默认 |
| 10 | JSON 配置文件 | `file` | 路径由环境变量 `CONFIG_FILE` 指定，文件缺失视为空层 |
| 20 | 环境变量 | `env` | `APP_VALUES_<KEY>`、`APP_FLAGS_<NAME>`，值按 JSON 解析 |
| 30 | 运行时推送 | `api` | `POST /refresh` 请求体，优先级最高 |

- 合并是确定性的：先按优先级排序，同优先级按入参顺序后者覆盖前者。
- 每个键的最终出处记录在 `Snapshot.field_origins`，`GET /config` 一并返回，做到“取值可解释”。
- 文件结构：`{"values": {...}, "flags": {...}}`，示例见 `config.example.json`。
- 环境变量示例：`APP_VALUES_MAX_CONNECTIONS=300`、
  `APP_FLAGS_NEW_CHECKOUT='{"mode":"percentage","percentage":10}'`。

字段规格（见 `default_spec()`）：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `app_name` | string | 必填，正则 `^[a-z][a-z0-9_-]{2,31}$` |
| `db_password` | string | 必填，**敏感** |
| `log_level` | string | 可选，默认 `info`，枚举 debug/info/warning/error |
| `max_connections` | int | 可选，默认 100，范围 1..10000 |
| `sample_rate` | float | 可选，默认 1.0，范围 0.0..1.0 |
| `enable_cache` | bool | 可选，默认 true（`true` 字符串经 JSON 解析） |
| `timeout_ms` | int | 可选，默认 5000，范围 1..60000 |

> 严格策略针对“已知字段”：值非法必拒绝；规格之外的多余字段不会写入快照。

## 二、刷新触发方式

- **启动时**：`lifespan` 自动执行一次 `文件 + 环境变量` 刷新；失败不会静默使用默认值，
  服务进入“未初始化”态，读接口返回 `503 uninitialized`，等待一次合法的 `POST /refresh`。
- **运行时**：`POST /refresh`，请求体为 `{"values": {...}, "flags": {...}}`，
  服务在其之上叠加最新的文件与环境变量层后整体校验、原子切换。
- 刷新在锁内完成“合并 → 校验 → 构造新快照”，**只有全部成功才替换当前引用**；
  任一项非法则整份拒绝并返回 422，响应里带 `retained_version`（保留的旧版本号）。

```bash
curl -X POST localhost:8000/refresh \
  -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"s3cr3t","max_connections":300},
       "flags":{"new_checkout":{"mode":"percentage","percentage":20}}}'
```

## 三、功能开关放量语义

开关可写为 `true/false`、`"on"/"off"`，或对象：

| mode | 语义 |
| --- | --- |
| `on` | 对所有请求启用 |
| `off` | 对所有请求停用（未配置时的默认模式） |
| `percentage` | 依据 `sha256(flag + ":" + user_id)` 哈希到 0..99 分桶，`bucket < percentage` 放量；缺 `user_id` 时**不放量**（保证可复现而非随机） |
| `allow_groups` | 请求分组与允许分组有交集即放量，`groups` 必须为非空、无重复的字符串列表 |

- **请求内一致性**：判定基于 `ContextVar` 请求级缓存，同一请求对同一开关的多次判定恒等，
  即使后续调用传入了不同的 `user_id/groups`。HTTP 每次请求通过 `request_scope` 建立并清理缓存。
- **未定义开关**：判定抛 `UnknownFlagError`，HTTP 返回 `404 unknown_flag`；
  配置中出现未定义开关则整份配置被拒绝（`ConfigFormatError`）。
- **已废弃开关**（如 `legacy_reports`）：仍可判定，结果带 `deprecated=true` 与
  `replacement`，并打印 WARNING 日志提示迁移。
- 每次判定都返回可解释的 `reason`（分桶值、阈值、命中分组等）并写 INFO 日志。

## 四、错误分类

| 异常 / error_type | 含义 | HTTP |
| --- | --- | --- |
| `ConfigMissingError` / `missing` | 必填项所有来源都缺失 | 422 |
| `ConfigTypeError` / `type` | 类型不匹配（布尔不会被当作整数） | 422 |
| `ConfigRangeError` / `range` | 数值越界（min/max、percentage 0..100） | 422 |
| `ConfigFormatError` / `format` | 枚举/正则/结构非法、未知开关、分组重复等 | 422 |
| `ConfigSourceError` / `source` | 配置文件不可读或 JSON 解析失败 | 400 |
| `refresh_rejected` | `/refresh` 整体被拒，`detail_error_type` 给出具体类别 | 422 |
| `unknown_flag` | 访问未定义开关 | 404 |
| `uninitialized` | 尚无任何可用配置 | 503 |

刷新失败响应示例：

```json
{
  "ok": false,
  "error_type": "refresh_rejected",
  "detail_error_type": "ConfigRangeError",
  "reason": "字段 'max_connections' 取值 9999999 大于上限 10000",
  "retained_version": 3
}
```

## 五、敏感配置保护

- 标记 `sensitive=True` 的字段（如 `db_password`）在 `GET /config` 与 `/flags` 等
  所有对外输出中统一替换为 `***REDACTED***`；
- 原始值仅存在于进程内不可变快照中供内部使用，不会出现在任何响应文本里；
- 错误信息只包含字段名与约束，不回显敏感值。

## 六、HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查与当前版本（未初始化时 503） |
| `GET /config` | 版本、脱敏配置、来源列表与每字段出处 |
| `POST /refresh` | 运行时推送并原子切换（失败回退） |
| `GET /flags` | 开关清单（模式、放量参数、废弃标记） |
| `POST /flags/{name}/check` | 开关判定，body：`{"user_id": "...", "groups": [...]}` |

## 七、并发一致性模型

- 当前配置是不可变对象 `Snapshot`，读路径**无锁**只取一次引用，
  因此读到的要么是完整旧版、要么是完整新版，不可能读到字段跨版本的“半成品”。
- 刷新在单把锁内构造新快照，成功后一次性替换引用；失败不动旧引用。
- `tests/test_concurrency.py` 用 8 读线程 + 2 个合法刷新线程 + 1 个持续非法刷新线程
  压测（每轮上万次读取），断言字段组合始终自洽、键不丢失、非法刷新后旧快照仍有效。

## 八、本地验证

```bash
# 安装依赖（dev 组含 pytest / httpx）
uv sync

# 运行全部测试；-s 可查看“触发输入 => 判定依据”的日志打印
uv run pytest -s

# 启动服务（可选指定配置文件）
CONFIG_FILE=config.example.json uv run fastapi dev main.py

# 1) 查看脱敏配置与来源
curl -s localhost:8000/config | python -m json.tool
# 2) 推送一次运行时更新（无需重启）
curl -s -X POST localhost:8000/refresh -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"s3cr3t","max_connections":300}}'
# 3) 开关判定（按比例，稳定分桶）
curl -s -X POST localhost:8000/flags/new_checkout/check \
  -H 'Content-Type: application/json' -d '{"user_id":"user-42"}'
# 4) 非法刷新（越界），观察 422 与 retained_version
curl -s -X POST localhost:8000/refresh -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"x","max_connections":0}}'
```
