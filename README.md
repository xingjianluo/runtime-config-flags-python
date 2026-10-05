# Runtime Config & Feature Flags（Python / FastAPI）

在**不重启服务**的前提下更新运行时配置与功能开关，并保证：

- 多来源按**稳定优先级**合并，每个取值都能解释来自哪个来源；
- **逐层严格校验**：任一层自带的值/开关非法（哪怕被高优先级盖掉）整次提交即拒绝；
  未定义配置项/开关同样拒绝；多处问题一次性列出（项/分类/来源），敏感值不出现在诊断里；
- 成功提交后可查每个生效值的出处，以及本次被高优先级**挡下的低优先级冲突**；
- 刷新失败**保留上一份可用配置**；并发请求只可能看到完整的旧版本或完整的新版本，不混合、不丢失；
- 每次成功切换都进入**版本历史**，保留份数可配、自动淘汰最旧但当前版本永不淘汰，可**一键回退**；
- 运行期来源损坏（文件损坏/结构非法）进入明确可区分的**降级态**：旧值立即下线，
  健康检查与所有读取接口返回带来源和故障类别的 503；下一次合法提交后自动恢复；
- 功能开关支持启用 / 停用 / 按比例 / 按分组放量，**同一请求内多次判定结果一致**；
- 未定义开关、已废弃开关、敏感配置项均有明确处理，**任何对外输出都不泄露敏感值**。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `app/errors.py` | 可区分的错误类型（缺项/类型/越界/格式/来源/聚合/降级） |
| `app/schema.py` | 字段与开关规格、逐层严格校验管道、聚合诊断、不可变快照、脱敏 |
| `app/sources.py` | 来源加载（文件/环境变量/API）与稳定合并、来源追踪 |
| `app/manager.py` | 原子快照、三态状态机、版本历史/容量淘汰、回退与降级恢复 |
| `app/flags.py` | 开关判定（哈希分桶/分组）、请求内一致性、可解释依据 |
| `app/web.py` | FastAPI 入口：历史/回退接口、诊断、降级错误到 HTTP 的映射 |
| `tests/` | 历史回退、容量淘汰、严格提交、多问题聚合、降级恢复、优先级、并发、HTTP 集成测试 |

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

> 严格策略：规格之外的多余字段/开关属于 `unknown` 问题，整次提交拒绝，不会写入快照也不静默丢弃。

## 一·补、版本历史、容量淘汰与一键回退

- **留存**：每次**成功**切换（含回退）都生成一份不可变 `VersionEntry`，记录版本号、
  参与来源、切换时间（UTC ISO-8601），并冻结本次提交的原始来源层。校验失败的提交不留存。
- **版本号**：单调递增，只增不复用。回退不是“把指针拨回去”，而是取目标版本留存的层
  **重新走一遍普通刷新的合并与校验**，成功后生成一个新版本，并在历史里标记
  `rolled_back_from=<原版本号>`；并发读依旧只可能看到完整的某一版。
- **回退失败**：目标版本不存在（`rollback_rejected` / `VersionNotFoundError`）或
  其留存层重新校验不过（类别与普通刷新完全一致）时，拒绝切换并**停在当前版本**，
  服务不会进入半死状态。
- **容量规则**：保留份数由构造参数 `history_limit`（默认 10，最小 1）控制；
  超过上限后按时间**淘汰最旧**记录，但**当前正在用的那份永不被淘汰**
  （即便它因为回退而不是最大版本号）。
- **查询/操作**：

| 方法/路径 | 说明 |
| --- | --- |
| `GET /versions` | 当前版本、状态、容量与历史元数据清单（不含配置值） |
| `POST /versions/{version}/rollback` | 一键回退；成功返回新版本号与 `rolled_back_from`，失败 422 |

## 一·补二、提交校验与诊断口径

校验在 `commit_snapshot` 中**按层独立**进行，而不是只看合并后的最终值：

1. 每一层的每个字段/开关都按规格校验。低优先级层自己非法时，**即使同名键最终会被
   高优先级覆盖**，整次提交仍然拒绝——不会因为“反正用不到”就放行带病来源；
2. 任何层出现规格未定义的字段或开关，记为 `unknown` 问题并拒绝（不再静默忽略）；
3. 一次提交内所有问题**一次性收集**、按 `(section, key, source, category)` 稳定排序后上报，
   每条都带：`section`（values/flags）、`key`、`category`、`source`、`message`；
4. `category` 取值：`missing` / `type` / `range` / `format` / `unknown`；
   单问题时 `detail_error_type` 为具体异常名（`ConfigRangeError` 等），
   多问题时为 `ConfigValidationError`；
5. **敏感诊断脱敏**：敏感字段（如 `db_password`）非法时，诊断只说明约束
   （需要的类型/范围/格式），绝不回显真实值。

成功之后还可解释两件事：

- **取值出处**：`GET /config` 的 `field_origins` 给出每个生效值来自哪一层（缺省项为 `defaults`）；
- **被挡冲突**：本次提交中“合法但被更高优先级同名键覆盖”的取值列入 `shadowed`
  （含 key、被挡来源 `source`、获胜来源 `winning_source`、section），
  `POST /refresh` 的响应与 `GET /config` 都会返回。

422 响应示例（多问题）：

```json
{
  "ok": false,
  "error_type": "refresh_rejected",
  "detail_error_type": "ConfigValidationError",
  "retained_version": 3,
  "issues": [
    {"section": "values", "key": "max_connections", "category": "range",
     "source": "api", "message": "字段 'max_connections' 取值 0 小于下限 1"},
    {"section": "values", "key": "db_password", "category": "type",
     "source": "api", "message": "敏感配置项 'db_password' 非法：需要 string，实际为 int（真实值不展示）"}
  ]
}
```

## 二、刷新触发方式

- **启动时**：`lifespan` 自动执行一次 `文件 + 环境变量` 刷新；若来源本身损坏/不可读，
  服务不崩溃，进入“未初始化（uninitialized）”态，读接口返回 503，等待合法提交。
- **运行时**：`POST /refresh`，请求体为 `{"values": {...}, "flags": {...}}`，
  服务在其之上叠加最新的文件与环境变量层后逐层校验、原子切换。
- 刷新在锁内完成“加载 → 逐层校验 → 构造新快照”，**只有全部成功才替换当前引用**；
  语义非法则整份拒绝并返回 422（带 `retained_version` 与 `issues`）；
  来源读取故障则进入降级态（见下节）。

```bash
curl -X POST localhost:8000/refresh \
  -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"s3cr3t","max_connections":300},
       "flags":{"new_checkout":{"mode":"percentage","percentage":20}}}'
```

## 二·补、来源故障下的降级态

管理器是显式三态状态机：

| 状态 | 含义 | 读接口行为 |
| --- | --- | --- |
| `uninitialized` | 从未成功加载过（含启动时来源就坏） | 503 `uninitialized` |
| `active` | 有一份生效版本 | 200 |
| `degraded` | 曾健康，但运行期重新读取时来源故障 | 503 `config_unavailable` |

- **触发**：运行期刷新时来源不可读 / JSON 解析失败 / 顶层或 `values`、`flags` 段
  结构非法（`ConfigSourceError`，例如文件被写成损坏内容或顶层变成数组）。
- **不假装没事**：进入 `degraded` 时旧快照**立即下线**，读路径不会继续返回旧值；
  也不会抛出笼统 500。`GET /health`、`/config`、`/flags`、开关判定统一返回
  503 `config_unavailable`，并带 `state=degraded`、`source`（如 `file`）、
  `kind=source`、`detail`（具体解析错误/路径）、`failed_at`。
- **语义非法不降级**：API 推送内容类型/范围/格式非法属于提交被拒（422），
  当前版本保留、状态仍为 `active`，两类故障口径互不混淆。
- **持续与恢复**：来源没修好前，每次刷新都停在 `degraded`；一旦来源恢复且
  下一次提交合法配置成功，状态自动回到 `active`，版本号继续单调递增；
  降级期间版本历史不丢失。

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
| `unknown`（issue 分类） | 出现未定义配置项或开关 | 422 |
| `ConfigValidationError` | 一次提交聚合多处问题（逐条见 `issues`） | 422 |
| `ConfigSourceError` / `source` | 配置文件不可读或 JSON 解析/结构失败 | 503（运行期降级） |
| `refresh_rejected` | `/refresh` 语义校验整体被拒，`detail_error_type` 给出类别 | 422 |
| `rollback_rejected` | 回退目标不存在或重新校验失败，停在当前版本 | 422 |
| `unknown_flag` | 访问未定义开关 | 404 |
| `uninitialized` | 尚无任何可用配置 | 503 |
| `config_unavailable` | 来源故障降级：暂时没有可信配置（带 source/kind/detail） | 503 |

刷新失败响应示例（单问题）：

```json
{
  "ok": false,
  "error_type": "refresh_rejected",
  "detail_error_type": "ConfigRangeError",
  "reason": "字段 'max_connections' 取值 9999999 大于上限 10000",
  "retained_version": 3,
  "issues": []
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
| `GET /health` | 健康检查与当前状态（active 200；uninitialized/degraded 503 并说明来源） |
| `GET /config` | 版本、脱敏配置、每字段出处、本次提交被挡冲突 `shadowed` |
| `POST /refresh` | 运行时推送并原子切换；422 带全部 `issues`，来源故障 503 降级 |
| `GET /versions` | 版本历史清单（版本/来源/时间/回退标记）、当前版本与容量 |
| `POST /versions/{v}/rollback` | 一键回退；按普通刷新规则重新校验，失败停在当前版本 |
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

# 运行全部测试；-s 可查看“场景抬头 + 触发输入 => 判定依据”的逐条日志
uv run pytest -s

# 只看本次新增的三类场景
uv run pytest -s tests/test_version_history_rollback.py   # 场景一：历史/容量/回退
uv run pytest -s tests/test_strict_commit.py              # 场景二：严格校验/多问题/脱敏/被挡冲突
uv run pytest -s tests/test_degraded_state.py             # 场景三：来源损坏降级与恢复
uv run pytest -s tests/test_http_versioning_degraded.py   # 三类能力的 HTTP 端到端

# 启动服务（可选指定配置文件）
CONFIG_FILE=config.example.json uv run fastapi dev main.py

# 1) 查看脱敏配置、每字段出处与本次被挡冲突
curl -s localhost:8000/config | python -m json.tool
# 2) 推送一次运行时更新（无需重启）
curl -s -X POST localhost:8000/refresh -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"shop-api","db_password":"s3cr3t","max_connections":300}}'
# 3) 查看历史版本并一键回退到 v1
curl -s localhost:8000/versions | python -m json.tool
curl -s -X POST localhost:8000/versions/1/rollback | python -m json.tool
# 4) 多问题提交：一次性看到每处问题的项/分类/来源，敏感值不回显
curl -s -X POST localhost:8000/refresh -H 'Content-Type: application/json' \
  -d '{"values":{"app_name":"Bad Name","db_password":123,"max_connections":0}}'
# 5) 开关判定（按比例，稳定分桶）
curl -s -X POST localhost:8000/flags/new_checkout/check \
  -H 'Content-Type: application/json' -d '{"user_id":"user-42"}'
# 6) 模拟运行期来源损坏：把配置文件改成非法内容后刷新，
#    观察 /health 返回 503 config_unavailable（含 source/kind/detail），
#    修复文件后再刷新即自动恢复 active。
```

### 自动化测试覆盖口径

| 测试文件 | 覆盖场景 |
| --- | --- |
| `test_version_history_rollback.py` | 每次切换留存、来源/时间可查、`history_limit` 容量淘汰最旧、当前版本受保护、回退生成新版本、回退失败停在当前、失败提交不入历史 |
| `test_strict_commit.py` | 跨来源非法值（即使被盖掉）拒绝、未知字段/开关拒绝、多问题一次性上报、敏感诊断不带真实值、`shadowed` 被挡冲突、每值出处 |
| `test_degraded_state.py` | 文件损坏/结构非法进入降级、旧值立即下线、读取异常可区分、API 语义非法不降级、损坏持续期间不恢复、合法提交自动恢复、版本单调、启动损坏为未初始化、历史跨降级保留 |
| `test_http_versioning_degraded.py` | `/versions` 与回退接口、降级时 health/config/flags/check 全 503 且带诊断、422 多问题、shadowed 上报、修复后恢复 |
| 既有用例 | 优先级合并、值校验、刷新失败回退、开关放量/一致性/脱敏、8 读线程并发一致性等，全部保持不回归 |

`pytest -s` 时每个用例前会打印 `===== 场景：... | 用例：test_xxx =====` 抬头，
用例内部再打印 `[输入] ... => [依据] ...`，日志即可直接看出覆盖了哪些场景与各用例情况。
