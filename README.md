# runtime-config-flags-python

FastAPI 运行时配置与功能开关服务：**不重启服务**即可更新配置；所有入口读到的配置
是一份完整、不可变、可追溯的版本；非法配置一律拒绝，绝不静默回退默认值。

## 1. 配置来源与优先级

优先级从低到高，**逐字段覆盖**（高优先级来源只覆盖它显式提供的字段）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 0 | `default` | 代码内置基线 `dynconfig/config.py:DEFAULTS` |
| 10 | `file` | JSON 文件，路径由 `APP_CONFIG_FILE` 指定，默认 `config/default.json` |
| 20 | `env` | 环境变量，前缀 `APP_`，双下划线 `__` 表示层级 |
| 30 | `api` | 管理接口 `POST /admin/refresh` 提交的即时覆盖层 |

- 环境变量映射：`APP_HTTP__PORT=9090` → `http.port = 9090`。
  环境变量值恒为字符串，按字段类型做受控字面量转换
  （int/float/bool/枚举；bool 接受 `true/false/1/0/yes/no/on/off`）。
- 文件与 API 使用 JSON 原生类型；字符串传给 int/float/bool 直接判类型错误。
- 每个字段在 `/config` 的 `sources` 中都能查到最终值**来自哪一层**，可解释。
- 文件不存在视为该层为空（不提供覆盖）；文件存在但损坏 / JSON 非法 / 顶层不是对象，
  直接报 `source_error`，不回退默认。

字段定义见 `dynconfig/config.py:SCHEMA`，支持类型：`int / float / bool / str / enum`，
可声明 `required / default / min / max / choices / pattern / sensitive`。

## 2. 刷新触发方式

- `POST /admin/refresh`，**无请求体**：重新读取文件 + 环境变量并重载；
- `POST /admin/refresh`，**带 JSON 请求体**：在重新读取的完整来源栈之上叠加
  最高优先级 API 覆盖（不是只使用请求体，避免必填项缺失）。API 覆盖是临时的，
  下次无负载重载即恢复为文件/环境变量的值。

成功返回 `{status, old_version, version}`；失败返回 `422`：

```json
{
  "error": {
    "code": "refresh_failed",
    "message": "刷新失败，继续使用版本 3：配置解析被拒绝，共 2 处错误",
    "reasons": [
      {"code": "out_of_range", "message": "...", "field": "http.port",
       "source": "api", "kept_version": 3}
    ]
  },
  "kept_version": 3
}
```

刷新失败时**线上快照保持不变**（版本号、值均不变），响应里逐条给出可区分原因。

## 3. 功能开关与放量语义

开关在 Schema 中定义，可被各来源的 `flags` 段覆盖
（仅允许 `state / rollout / groups` 三个属性，未知开关/属性一律拒绝）。

判定顺序（`dynconfig/flags.py`）：

1. `state = "on"` → 恒启用，`reason="on"`；`"off"` → 恒停用，`reason="off"`；
2. `state = "conditional"` 时：
   - **分组优先**：请求分组与规则 `groups` 有交集 → 启用，`reason="group:<name>"`；
   - **比例放量**：对 `SHA256(flag_key + user_id) % 100` 取稳定桶，
     桶号 `< rollout` 则启用，`reason="rollout:<bucket>/<pct>"`。
     同一用户对同一开关跨请求结果稳定；不同开关独立取桶；
   - 无 `user_id` 的匿名请求无法稳定分桶 → 不放量，`reason="anon"`。

请求一致性：`FlagContext` 即请求作用域，首次判定时**钉住当前快照版本并缓存结果**，
请求处理途中即使发生刷新，同一上下文内的多次判定也恒为同一版本、同一结果。

- 未定义开关：`undefined_flag` → HTTP `404`，拒绝猜测默认值；
- 已废弃开关（`deprecated=true`）：`deprecated_flag` → HTTP `410`，
  强制调用方移除引用，不返回启用/停用；
- 批量判定 `POST /flags/check`：每个开关独立给出结果或错误，整体状态码
  `200`（全部成功）/ `207`（部分失败），并在响应中回显判定所依据的 `version`。

## 4. 错误分类与敏感配置

| code | 含义 |
|---|---|
| `missing_field` | 必填项在所有来源中均缺失 |
| `type_error` | 值类型与字段声明不符（含 bool 冒充 int） |
| `out_of_range` | 数值超出 min/max，或 rollout 超出 0..100 |
| `format_error` | 枚举值非法、正则不匹配、未知配置项/开关属性 |
| `source_error` | 来源文件不可读 / JSON 非法 / 顶层类型错 |
| `refresh_failed` | 刷新整体被拒，`reasons` 聚合上述全部原因 |
| `undefined_flag` | 访问未定义开关（404） |
| `deprecated_flag` | 访问已废弃开关（410） |

- 解析采用**聚合报错**：一次返回全部问题，每条含 `field/source/message`；
- 低优先级来源中的非法值**不会被高优先级合法值掩盖**，照样拒绝；
- 标记 `sensitive: true` 的字段（如 `db.password`）在任何对外输出
  （`GET /config`、错误响应、刷新回显）中一律显示 `***redacted***`，
  但仍正常参与校验与来源追溯。

## 5. 一致性模型（并发）

- `ConfigSnapshot` 为不可变对象；读路径只取一次快照引用，不持锁，
  因此请求要么看到**完整旧版本**，要么看到**完整新版本**，不可能字段跨版本混合；
- 刷新路径持互斥锁串行执行：先重建并完整校验候选快照，通过后才做唯一一次引用替换；
  校验失败完全不触碰当前快照；
- 并发刷新不会丢失成功更新或损坏状态（版本号严格等于成功刷新次数 + 1）。

## 6. 本地验证

```bash
# 安装依赖（含 pytest）
uv sync

# 运行全部单测（用 -s 可查看"触发输入/判定依据"日志）
uv run pytest -s

# 启动服务
uv run fastapi dev main.py
# 或 uv run uvicorn main:app --reload
```

手工验证：

```bash
curl -s localhost:8000/config                       # 查看脱敏配置与来源
curl -s -H 'X-User-Id: u1' localhost:8000/flags/new_checkout
curl -s -X POST localhost:8000/admin/refresh \
     -H 'Content-Type: application/json' \
     -d '{"http.port": 9090, "flags":{"new_checkout":{"rollout":80}}}'
curl -s -X POST localhost:8000/admin/refresh \
     -H 'Content-Type: application/json' -d '{"http.port": 99999}'   # 422 回退
```

环境变量实验：

```bash
APP_HTTP__PORT=9090 APP_LOG__LEVEL=debug uv run fastapi dev main.py
```

## 7. 代码结构

```
dynconfig/
  errors.py    # 错误分类（原因码 + field/source 溯源）
  schema.py    # 字段/开关规格
  sources.py   # default/file/env/api 四层来源加载
  parser.py    # 合并、严格校验、聚合报错、构建不可变快照
  manager.py   # 原子切换、失败回退、刷新串行化
  flags.py     # 开关判定、稳定分桶、请求上下文
  service.py   # 请求级判定服务（版本钉住 + 缓存）
  api.py       # FastAPI 路由（脱敏、错误码映射）
  config.py    # 应用 Schema 与管理器装配
config/default.json
tests/         # 优先级/拒绝/开关/回退/并发/HTTP 共 25 个用例
```
