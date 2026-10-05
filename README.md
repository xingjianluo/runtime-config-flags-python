# Runtime Config & Feature Flags（Python / FastAPI）

在**不重启服务**的前提下更新运行时配置与功能开关，并保证：

- 多来源按**稳定优先级**合并，每个取值都能解释来自哪个来源，被盖掉的低优先级冲突也有记录；
- **逐层严格提交**：任一层（即使其值被更高优先级盖掉）自身不合法、或出现未定义的
  配置项/开关，整次提交一律拒绝；多处问题一次性全部上报，敏感值绝不进诊断信息；
- 每次成功切换都进入**版本历史**，可查来源与时间、可一键**回退**；历史容量可配置，
  超容量淘汰最旧版本，当前正在使用的版本永不淘汰；
- 运行期来源损坏时进入明确可区分的**降级态（暂时没有可用配置）**，健康检查与各读取接口
  都如实报告故障来源与类别，不假装沿用旧值；下一次合法提交自动恢复；
- 刷新失败（值校验问题）**保留上一份可用配置**；并发请求只可能看到完整的旧版本或完整的
  新版本，不混合、不丢失；
- 功能开关支持启用 / 停用 / 按比例 / 按分组放量，**同一请求内多次判定结果一致**；
- 未定义开关、已废弃开关、敏感配置项均有明确处理，**任何对外输出都不泄露敏感值**。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `app/errors.py` | 可区分的错误类型（缺项/类型/越界/格式/未知项/来源/聚合校验/降级态）与 `ValidationIssue` |
| `app/schema.py` | 字段与开关规格、逐层严格校验、聚合问题上报、不可变快照 `Snapshot`（溯源/影子冲突/时间/原始层）、脱敏 |
| `app/sources.py` | 来源加载（文件/环境变量/API）、稳定合并与逐键贡献来源溯源 |
| `app/manager.py` | 线程安全的原子快照、刷新、版本历史与回退、容量淘汰、来源故障降级态 |
| `app/flags.py` | 开关判定（哈希分桶/分组）、请求内一致性、可解释依据 |
| `app/web.py` | FastAPI 入口：查询/刷新/版本/回退接口、错误到 HTTP 的映射、降级态 503 |
| `tests/` | 优先级、严格提交、版本历史与回退、来源降级恢复、并发一致性、HTTP 集成测试 |

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

> 严格策略对**每一层**独立生效：值非法必拒绝；规格之外的多余字段/开关也必拒绝
> （不再宽容丢弃），哪怕它来自低优先级层、且最终值被高优先级盖掉。详见下一节。

## 二、刷新触发方式

- **启动时**：`lifespan` 自动执行一次 `文件 + 环境变量` 刷新；失败不会静默使用默认值，
  服务进入“未初始化”态，读接口返回 `503 uninitialized`，等待一次合法的 `POST /refresh`。
- **运行时**：`POST /refresh`，请求体为 `{"values": {...}, "flags": {...}}`，
  服务在其之上叠加最新的文件与环境变量层后整体校验、原子切换。
- 刷新在锁内完成“合并 → 校验 → 构造新快照”，**只有全部成功才替换当前引用**；
  值校验失败返回 422，响应里带 `retained_version`（保留的旧版本号）与完整 `issues`。
- **来源故障**（文件损坏/解析失败）与值校验失败是两回事：来源故障进入降级态（见第六节），
  值校验失败只是拒绝本次提交、保留上一份配置。

```bash
curl -X POST localhost:8000/refresh \
  -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"s3cr3t","max_connections":300},
       "flags":{"new_checkout":{"mode":"percentage","percentage":20}}}'
```

## 二·补、提交校验与诊断口径（严格提交）

每次提交按**来源层逐层校验**，然后再校验合并结果：

1. **逐层合法性**：每一层提供的每一项都按规格独立校验。低优先级层的非法值
   **即使最终被高优先级盖掉**，这次提交仍然整体拒绝——不会因为“没生效”就放过来源自身的问题。
2. **未知项拒绝**：任一层出现规格中未定义的配置项或开关（`kind=unknown`），
   整次提交拒绝，不再像旧版本那样悄悄丢弃。
3. **多问题一次性上报**：一次提交的所有问题收集到 `issues` 数组里一次性返回，
   每条包含：
   - `key`：出问题的配置项/开关名；
   - `kind`：问题类别——`type` / `range` / `format` / `missing` / `unknown`；
   - `source`：问题值来自哪一层（`file` / `env` / `api`）；
   - `message`：人类可读描述；`sensitive=true` 标记敏感项。
4. **敏感值脱敏**：敏感项（如 `db_password`）出问题时，诊断信息只显示
   `***REDACTED***`，真实值不会出现在任何错误文本或 HTTP 响应里。
5. **可解释的成功结果**：刷新成功后，`GET /config` 返回：
   - `field_origins` / `flag_origins`：每个生效值/开关最终来自哪一层
     （未提供的可选项与默认开关记为 `defaults`）；
   - `shadows`：本次合并中被更高优先级挡下的低优先级冲突，每条带
     `kind`（field/flag）、`source`（被挡来源）、`winning_source`（生效来源）。

被拒绝的响应示例（一次两处问题）：

```json
{
  "ok": false,
  "error_type": "refresh_rejected",
  "detail_error_type": "ConfigValidationError",
  "retained_version": 3,
  "issues": [
    {"key": "max_connections", "kind": "range", "source": "api",
     "message": "字段 'max_connections' 取值 0 小于下限 1", "sensitive": false},
    {"key": "db_password", "kind": "type", "source": "file",
     "message": "字段 'db_password' 需要 string，实际为 int: ***REDACTED***",
     "sensitive": true}
  ]
}
```

## 二·补二、版本历史与一键回退

每**成功**切换一次配置（普通刷新或回退）就产生一个不可变版本并入历史；被拒绝的提交不入历史。

- **查询**：`GET /versions` 返回最近若干份可用版本（最新在前），每份含版本号、
  来源列表、UTC 切换时间、切换原因（`refresh` / `rollback`）、回退源版本
  `origin_version`，以及 `current` 标记。
- **回退**：`POST /versions/{version}/rollback` 取出该版本提交时的**原始来源层**，
  按与普通刷新**完全相同**的规则重新合并、逐层校验、构造快照：
  - 成功则生成一个**新版本号**（版本号只增不复用），快照中 `reason=rollback`、
    `origin_version` 指向被回退版本；被回退版本本身仍保留在历史中；
  - 目标版本不存在 -> `404 VersionNotFound`；重新校验失败 -> `422 rollback_rejected`
    并在 `issues` 中列出全部问题，服务**停在当前版本**，不会切成半死状态。
- **容量规则**：保留份数由构造参数 `history_limit`（HTTP 服务可用环境变量
  `CONFIG_HISTORY_LIMIT`，默认 10，最小 1）控制；超过上限时淘汰**最旧**的版本；
  **当前正在使用的版本即使最旧也不会被淘汰**（此时旧的非当前版本先被淘汰）。
- 历史接口只返回元数据，不含任何配置值，敏感信息无从泄露。

```bash
curl -s localhost:8000/versions | python -m json.tool
curl -s -X POST localhost:8000/versions/2/rollback
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
| `ConfigValidationError` / `validation` | 提交被聚合拒绝，`issues` 含全部问题（主入口错误） | 422 |
| `ConfigMissingError` / `missing` | 必填项所有来源都缺失（issue 中 `kind=missing`） | 422 |
| `ConfigTypeError` / `type` | 类型不匹配（布尔不会被当作整数） | 422 |
| `ConfigRangeError` / `range` | 数值越界（min/max、percentage 0..100） | 422 |
| `ConfigFormatError` / `format` | 枚举/正则/结构非法、分组重复等 | 422 |
| `ConfigUnknownKeyError` / `unknown` | 来源提供了未定义的配置项/开关 | 422 |
| `ConfigSourceError` / `source` | 配置文件不可读或 JSON 解析失败（来源侧故障） | 400/503* |
| `ConfigUnavailableError` / `unavailable` | 降级态：来源故障导致当前没有可用配置，带 `fault_source`/`fault_error_type` | 503 |
| `refresh_rejected` | `/refresh` 值校验被拒，`detail_error_type=ConfigValidationError` | 422 |
| `rollback_rejected` | 回退被拒（不存在为 404，校验不过为 422），保留当前版本 | 404/422 |
| `unknown_flag` | 访问未定义开关 | 404 |
| `uninitialized` | 尚无任何可用配置（启动时即失败，从未成功过） | 503 |

\* 直接加载来源时抛 `ConfigSourceError`；经由 `POST /refresh` 触发的来源故障会被转成
503 `unavailable` 降级态。`uninitialized`（从未加载成功）与 `unavailable`
（曾正常运行、因来源故障暂时无配置）是两个可区分状态。

刷新失败响应示例：

```json
{
  "ok": false,
  "error_type": "refresh_rejected",
  "detail_error_type": "ConfigValidationError",
  "retained_version": 3,
  "issues": [
    {"key": "max_connections", "kind": "range", "source": "api",
     "message": "字段 'max_connections' 取值 9999999 大于上限 10000",
     "sensitive": false}
  ]
}
```

## 五、来源故障下的降级态

运行期重新读取来源时，若**来源本身**损坏（如配置文件被写坏、JSON 解析失败、结构不对），
服务**不会起不来，也不会假装无事继续沿用旧值**，而是进入显式的
`degraded / unavailable`（暂时没有可用配置）状态：

- 当前快照引用被撤下（`manager.current()` 抛 `ConfigUnavailableError`），
  `manager.is_degraded()` 为真，故障信息可通过 `manager.fault()` 获取，
  内含 `fault_source`（哪个来源）与 `fault_error_type`（哪类问题）；
- `GET /health` 返回 `503`，`status=degraded`，带 `fault_source` /
  `fault_error_type` / `reason`；
- `GET /config`、`GET /flags`、`POST /flags/{name}/check` 在降级态统一返回
  `503 {"error_type": "unavailable", ...}`，而不是笼统的 500 内部错误；
- 版本历史不受影响：旧版本仍在历史中，故障恢复后可直接回退；
- **自动恢复**：下一次提交合法配置成功（文件修好后普通刷新，或直接推送一层合法配置）
  即原子恢复为正常状态，`/health` 回到 `200 status=ok`。

注意区分两类运行期失败：

| 情形 | 表现 | 当前配置 |
| --- | --- | --- |
| 值校验不通过（类型/范围/格式/未知项/缺项） | 422 拒绝提交 | **保留**上一份，不降级 |
| 来源故障（文件损坏/不可解析） | 503 降级态 | **撤下**当前配置，等待恢复 |

文件**不存在**按空层处理，不属于来源故障（随后若缺必填项，按 422 校验拒绝处理）。

## 六、敏感配置保护

- 标记 `sensitive=True` 的字段（如 `db_password`）在 `GET /config` 与 `/flags` 等
  所有对外输出中统一替换为 `***REDACTED***`；
- 原始值仅存在于进程内不可变快照中供内部使用，不会出现在任何响应文本里；
- 错误信息只包含字段名与约束，不回显敏感值（聚合 `issues` 中同样脱敏）。

## 七、HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查；正常 `200 status=ok`，降级态 `503 status=degraded` 并带故障来源/类别 |
| `GET /config` | 版本、脱敏配置、来源列表、每字段/开关出处 `origins`、被挡冲突 `shadows` |
| `POST /refresh` | 运行时推送并原子切换；值校验失败 422 保留旧版，来源故障 503 进入降级态 |
| `GET /versions` | 最近可用版本列表（版本号、来源、时间、原因、当前标记）与历史容量 |
| `POST /versions/{v}/rollback` | 一键回退到指定版本（重新校验，失败停留当前版本） |
| `GET /flags` | 开关清单（模式、放量参数、废弃标记） |
| `POST /flags/{name}/check` | 开关判定，body：`{"user_id": "...", "groups": [...]}` |

## 八、并发一致性模型

- 当前配置是不可变对象 `Snapshot`，读路径**无锁**只取一次引用，
  因此读到的要么是完整旧版、要么是完整新版，不可能读到字段跨版本的“半成品”。
- 刷新/回退在单把锁内构造新快照，成功后一次性替换引用；值校验失败不动旧引用；
  来源故障则显式撤下引用进入降级态。历史读写也在同一把锁保护下。
- `tests/test_concurrency.py` 用 8 读线程 + 2 个合法刷新线程 + 1 个持续非法刷新线程
  压测（每轮上万次读取），断言字段组合始终自洽、键不丢失、非法刷新后旧快照仍有效。

## 九、本地验证

```bash
# 安装依赖（dev 组含 pytest / httpx）
uv sync

# 运行全部测试；-v 看场景用例名，-s 查看“触发输入 => 判定依据”的场景日志
uv run pytest -v -s

# 只看新增能力的用例
uv run pytest -v -s tests/test_version_history.py tests/test_strict_commit.py \
  tests/test_source_failure.py tests/test_http_degraded.py tests/test_http_versions.py

# 启动服务（可选指定配置文件与历史容量上限，默认 CONFIG_HISTORY_LIMIT=10）
CONFIG_FILE=config.example.json CONFIG_HISTORY_LIMIT=5 uv run fastapi dev main.py

# 1) 查看脱敏配置、逐值出处与被挡冲突 shadows
curl -s localhost:8000/config | python -m json.tool
# 2) 推送一次运行时更新（无需重启）
curl -s -X POST localhost:8000/refresh -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"s3cr3t","max_connections":300}}'
# 3) 开关判定（按比例，稳定分桶）
curl -s -X POST localhost:8000/flags/new_checkout/check \
  -H 'Content-Type: application/json' -d '{"user_id":"user-42"}'
# 4) 非法刷新（越界），观察 422、retained_version 与一次性聚合的 issues
curl -s -X POST localhost:8000/refresh -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"x","max_connections":0}}'
# 5) 版本历史与一键回退
curl -s localhost:8000/versions | python -m json.tool
curl -s -X POST localhost:8000/versions/1/rollback
# 6) 模拟来源损坏：把配置文件写坏后刷新，观察 503 status=degraded 与 fault_source
echo '{ broken' >> /tmp/cfg-corrupt.json
#    修复文件后再次 POST /refresh，健康检查自动恢复 200 status=ok
```

## 十、新增行为对应的测试文件

| 文件 | 覆盖重点 |
| --- | --- |
| `tests/test_version_history.py` | 逐版本入档、失败不入档、容量淘汰、当前版本保护、回退成功生成新版本、回退不存在/重新校验失败停留当前版本 |
| `tests/test_strict_commit.py` | 被盖掉的低层非法值仍拒绝、未知配置项/开关拒绝、多问题一次性上报、敏感值不进诊断、生效出处与 shadows |
| `tests/test_source_failure.py` | 文件损坏进降级态、不沿用旧值、降级态判定报错、文件缺失不算故障、值校验失败不降级、合法提交自动恢复 |
| `tests/test_http_degraded.py` | 降级态 `/health`、`/config`、`/flags`、`/flags/check` 的 503 响应与故障来源，恢复后回归 200 |
| `tests/test_http_versions.py` | `/versions` 列表与 `/versions/{v}/rollback` 接口、未知版本 404 |
| 原有 6 个测试文件 | 优先级合并、校验基元、刷新失败保留旧版、并发一致性、开关语义/脱敏、HTTP 主流程（无回归） |

