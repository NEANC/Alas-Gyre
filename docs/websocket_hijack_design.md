# WebSocket 劫持设计说明

## 背景

Alas-Gyre 的 WebSocket 模式用于连接已经启动的 ALAS WebUI，并尝试通过 PyWebIO WebSocket 通道读取 ALAS 多配置状态、发送启动或停止命令，以及预留更新控制能力。

该设计的核心思路是：

1. 先访问 ALAS WebUI 首页，确认目标是 PyWebIO 页面。
2. 再建立 PyWebIO WebSocket 连接。
3. 模拟浏览器处理 PyWebIO 服务端消息。
4. 从页面输出中提取配置列表、运行状态、按钮回调 ID。
5. 必要时向 PyWebIO 发送 callback 事件，模拟用户点击按钮。

当前实现集中在 `alas_gyre/api/websocket_hijack.py`。

## 术语

| 术语 | 含义 |
|------|------|
| ALAS WebUI | ALAS 自带的 PyWebIO Web 页面。 |
| PyWebIO session | PyWebIO 为每个浏览器或 WebSocket 客户端创建的会话。 |
| WebSocket 劫持 | Alas-Gyre 直接连接 PyWebIO WebSocket，并模拟浏览器收发事件。 |
| 侧边栏配置 | ALAS WebUI 左侧的多配置入口，例如 `配置 A`、`配置 B`、`配置 C`。 |
| callback ID | PyWebIO 给按钮或输入控件生成的回调标识，例如 `CB-xxxx`。 |
| scheduler 按钮 | ALAS 配置页上的启动、停止按钮。 |
| header_status | ALAS 配置页显示运行状态的页面区域。 |

## 总体架构

WebSocket 劫持由 5 层组成：

1. **连接层：** 构造 HTTP 和 WebSocket 地址，检查 ALAS WebUI 是否可访问。
2. **协议层：** 解析 PyWebIO JSON 消息，回传浏览器脚本求值结果。
3. **状态层：** 累积 `output`、`output_ctl`、`pin_onchange`、`run_script` 等消息。
4. **提取层：** 从页面输出中提取配置名、运行状态、按钮回调 ID。
5. **控制层：** 通过 callback 事件模拟点击配置、启动、停止或更新按钮。

数据流如下：

```text
ALAS WebUI
  → PyWebIO WebSocket 消息
  → parse_pywebio_message()
  → PyWebIOState.apply_message()
  → extract_instance_names() / extract_status_all() / find_button_callback()
  → SingleSessionScheduler 缓存状态
  → Alas-Gyre UI 展示或提交控制命令
```

控制流如下：

```text
Alas-Gyre UI
  → post_config_action(config_name, action)
  → SingleSessionScheduler.post_action()
  → _process_one_control_command()
  → navigate_to_config()
  → find_config_action_callback()
  → send_callback_event()
  → ALAS WebUI 执行对应按钮逻辑
```

## 如何实现劫持

### 1. 检查目标页面

劫持前先请求 ALAS WebUI 首页：

```text
http://<ip>:<port>
```

页面必须包含以下任一特征：

- `pywebio_static`
- `WebIO.startWebIOClient`
- `pywebio`

如果目标不可访问，抛出 `WebUIUnavailableError`。如果目标不是 PyWebIO 页面，抛出 `NotPyWebIOError`。

### 2. 建立 WebSocket

WebSocket 地址格式为：

```text
ws://<ip>:<port>/?app=index&session=NEW
```

参数含义：

| 参数 | 说明 |
|------|------|
| `app=index` | 使用 ALAS WebUI 的 PyWebIO `index` 应用。 |
| `session=NEW` | 创建新的 PyWebIO session。 |

连接创建后设置超时，避免读取流永久阻塞。

### 3. 模拟浏览器行为

PyWebIO 服务端会发送 `run_script` 消息，要求浏览器执行 JavaScript。Alas-Gyre 需要模拟一部分浏览器能力：

| 脚本类型 | 处理方式 |
|----------|----------|
| `localStorage.getItem(key)` | 从本地 `local_storage` 字典读取并回传。 |
| `localStorage.setItem(key, value)` | 写入本地 `local_storage` 字典。 |
| `document.visibilityState` | 固定回传 `visible`。 |
| 其他非求值脚本 | 记录但不执行。 |

回传使用 PyWebIO 的 `js_yield` 事件：

```json
{
  "event": "js_yield",
  "task_id": "<task_id>",
  "data": "<result>"
}
```

### 4. 解析 PyWebIO 消息

每条 WebSocket 消息按 JSON 解析为：

```text
command
spec
task_id
raw
```

其中：

- `pin_onchange` 用于收集输入项和回调 ID。
- `run_script` 用于处理浏览器脚本求值。
- `output` 用于收集页面输出。
- `output_ctl` 用于处理页面区域替换、追加、清空。
- `set_session_id` 如果存在，用于记录 session ID。

当前实测 ALAS WebUI 不稳定返回可复用的 `set_session_id`，因此不能依赖 session 链接复用配置页。

## 如何处理多配置

### 配置列表来源

多配置优先从 ALAS 左侧侧边栏输出中提取。页面 scope 通常包含：

```text
pywebio-scope-alas-instance-
```

提取流程：

1. 收集首页初始输出。
2. 找到 scope 包含 `pywebio-scope-alas-instance-` 的输出。
3. 递归遍历输出中的 `buttons`。
4. 读取按钮 `label` 作为配置名。
5. 去重后写入配置缓存。

示例：

```text
alas-instance-0 → 配置 A
alas-instance-1 → 配置 B
alas-instance-2 → 配置 C
```

### 配置缓存

`SingleSessionScheduler` 维护以下缓存：

| 字段 | 说明 |
|------|------|
| `configs` | 配置名列表。 |
| `statuses` | 每个配置的运行状态。 |
| `tasks` | 每个配置的任务说明，目前通常为空。 |
| `buttons` | 每个配置的启动、停止按钮回调缓存。 |
| `last_seen_at` | 最近一次成功读取配置状态的时间。 |
| `scan_errors` | 每个配置的扫描错误。 |
| `control_errors` | 每个配置的控制错误。 |

### 扫描多配置

当前实现使用单个 PyWebIO session 依次扫描每个配置：

```text
建立 WS
  → bootstrap 读取侧边栏配置
  → for config_name in configs
      → 点击侧边栏配置
      → 收集 header_status 和 scheduler_btn
      → 更新状态和按钮缓存
```

也就是说，多配置状态不是从侧边栏直接得到的，而是依次进入每个配置页读取详情。

### 多配置的设计边界

当前 ALAS WebUI 侧边栏不可靠提供运行状态。因此，如果不点击配置页，只能获取配置名，不能可靠确定每个配置是否运行。

实测结论：

| 方式 | 是否能获取配置列表 | 是否能获取运行状态 | 是否安全 |
|------|------------------|------------------|----------|
| 读取首页侧边栏 | 是 | 否 | 是 |
| 点击侧边栏进入配置页 | 是 | 是 | 存在 ALAS GUI 崩溃风险 |
| 通过 URL 参数进入配置页 | 否 | 否 | 是，但无效 |
| 复用配置页链接 | 否 | 否 | 无可复用链接 |

## 如何对参数处理

### 连接参数

连接参数至少包含：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ip` | `127.0.0.1` | ALAS WebUI 地址。 |
| `port` | `22267` | ALAS WebUI 端口。 |
| `current_config` | 空 | 期望进入的配置名，仅用于 localStorage 模拟。 |

地址构造规则：

```text
HTTP: http://<ip>:<port>
WS:   ws://<ip>:<port>/?app=index&session=NEW
```

参数处理要求：

1. `ip` 和 `port` 必须转成字符串并去除首尾空白。
2. 空值使用默认值。
3. 不把配置名直接拼入 URL，除非 ALAS WebUI 明确支持 URL 路由。
4. 控制命令只接受白名单动作：`start`、`stop`。
5. 更新命令只接受白名单动作：`check`、`apply`、`cancel`。

### localStorage 参数

PyWebIO 页面会读取浏览器 localStorage。Alas-Gyre 用字典模拟 localStorage。

当前使用：

```python
local_storage = {
    "aside": current_config or None,
}
```

设计意图是让 ALAS WebUI 初始进入指定配置页。但实测 ALAS 会把 `aside` 重置为 `Home`，不能依赖它直接进入配置页。

因此，`localStorage["aside"]` 只能作为兼容参数，不应作为状态获取的可靠入口。

### 回调参数

PyWebIO 按钮 callback 事件格式如下：

```json
{
  "event": "callback",
  "task_id": "<callback_id>",
  "data": "<button_value>"
}
```

注意：这里的 `task_id` 实际上传入的是按钮 `callback_id`。这是 PyWebIO callback 通道的用法，不是普通消息里的 session `task_id`。

按钮值通常来自按钮组中的索引，例如：

```text
按钮组：[启动, 停止]
点击第 0 个按钮 → data = 0
```

### 参数校验和降级

参数设计应遵循以下规则：

1. 连接参数错误时，返回连接失败，不进入控制流程。
2. 配置名不存在时，返回 `button_callback_not_found:<config>`。
3. 按钮不存在时，返回 `config_action_callback_not_found:<action>`。
4. 页面状态证据不足时，标记 `target_scope_not_found`。
5. WebSocket 传输失败时，清空按钮缓存，保留最后一次业务状态。

## 如何确定当前配置的运行状态

### 状态来源

当前运行状态主要来自 2 个页面区域：

1. `header_status`
2. `scheduler_btn`

优先级如下：

```text
header_status 文本
  → scheduler_btn 按钮语义
  → 默认 error 或保持旧状态
```

### header_status 识别

`header_status` 通常包含运行状态文本，例如：

| ALAS 文本 | Alas-Gyre 状态 |
|-----------|----------------|
| `运行中`、`running`、`run` | `running` |
| `空闲`、`闲置`、`inactive`、`stopped` | `idle` |
| `错误`、`发生错误`、`warning` | `error` |
| `更新中`、`updating` | `update` |
| `未连接`、`disconnected` | `disconnected` |

### scheduler_btn 反推

如果没有读到 `header_status`，可根据按钮含义反推状态：

| 页面按钮 | 推断状态 | 说明 |
|----------|----------|------|
| `停止`、`中止`、`stop` | `running` | 页面提供停止按钮，说明当前配置正在运行。 |
| `启动`、`啟動`、`実行`、`start` | `idle` | 页面提供启动按钮，说明当前配置未运行。 |

### 状态证据检查

更新配置状态前需要确认页面中确实包含状态证据。有效证据包括：

- `header_status` scope 中出现状态关键词。
- `scheduler_btn` scope 中出现启动或停止按钮关键词。

如果没有证据，不更新状态，而是写入扫描错误：

```text
target_scope_not_found
```

### 当前配置与多配置的关系

ALAS WebUI 的配置页状态只代表当前打开的配置。因此，在多配置场景下：

1. 必须先进入某个配置页。
2. 再读取该配置页的 `header_status` 或 `scheduler_btn`。
3. 读取结果只能绑定到本次进入的 `config_name`。

不能把某个配置页的状态套用到其他配置。

### 已知限制

实测 ALAS WebUI 存在以下限制：

1. 首页侧边栏不提供可靠运行状态。
2. URL query 不能指定配置页。
3. 进入配置页后的浏览器链接不能复用。
4. `session=NEW` 下点击侧边栏配置可能触发 ALAS GUI 内部异常，例如 `state_switch` 缺失。

因此，当前设计在「状态完整性」和「安全性」之间存在取舍：

- 要完整状态，需要点击配置页。
- 要避免触发 ALAS GUI 脆弱路径，就不能点击配置页。

## 如何控制 ALAS 对应配置开与关

### 控制入口

上层调用：

```text
post_config_action(config, config_name, action)
```

其中：

| 参数 | 说明 |
|------|------|
| `config` | WebSocket 连接配置。 |
| `config_name` | 目标 ALAS 配置名。 |
| `action` | `start` 或 `stop`。 |

### 控制队列

`SingleSessionScheduler.post_action()` 不立即执行控制，而是把控制命令放入队列。

队列规则：

1. 同一配置只保留最后一次未执行命令。
2. 新命令会清理该配置旧的 `control_errors`。
3. 扫描循环会在合适时机处理一个控制命令。

这样可以避免用户连续点击导致大量重复控制。

### 启动或停止流程

控制流程如下：

```text
取出控制命令
  → navigate_to_config(config_name)
  → 收集 scheduler_btn
  → find_config_action_callback(action)
  → send_callback_event(callback_id, value)
  → 更新该配置状态
```

`find_config_action_callback()` 会按动作查找按钮：

| 动作 | 匹配按钮 |
|------|----------|
| `start` | `启动`、`啟動`、`実行`、`start` |
| `stop` | `停止`、`中止`、`stop` |

### 控制失败处理

控制失败时：

1. 不继续重试同一命令。
2. 写入 `control_errors[config_name]`。
3. 记录错误类型和错误内容。
4. 保留原有状态缓存。

常见失败包括：

| 错误 | 说明 |
|------|------|
| `button_callback_not_found:<config>` | 未找到目标配置按钮。 |
| `config_action_callback_not_found:<action>` | 未找到启动或停止按钮。 |
| WebSocket 超时 | 页面未输出目标 scope。 |
| WebSocket 断开 | ALAS WebUI 或网络异常。 |
| ALAS GUI 内部异常 | 点击配置页触发 ALAS 自身错误。 |

### 控制设计边界

控制启动或停止必须进入配置页，因为启动或停止按钮只存在于配置页详情区域。

如果禁止点击侧边栏配置，则无法可靠控制指定配置开关。此时应降级为：

- 禁用 WebSocket 模式的启动、停止按钮。
- 或仅在当前 session 已自然出现 `scheduler_btn` 时允许控制。
- 或要求使用 Overlay Runtime 模式完成控制。

## ALAS 更新控制如何传递

### 设计目标

ALAS 更新控制与配置启动、停止类似，本质也是点击 PyWebIO 页面按钮。

预期动作包括：

| 动作 | 匹配按钮 |
|------|----------|
| `check` | `检查更新`、`check update` |
| `apply` | `进行更新`、`update now`、`apply update` |
| `cancel` | `取消更新`、`cancel update` |

### 预期流程

完整更新流程应为：

```text
进入更新器页面
  → 收集更新器按钮
  → 根据 action 查找按钮 callback
  → send_callback_event(callback_id, value)
  → 收集更新结果或更新中状态
  → 回写状态缓存
```

### 当前实现状态

当前 `post_update_action()` 明确返回：

```text
update_action_out_of_scope
```

这表示更新控制暂未纳入当前 WebSocket 劫持生产路径。

### 更新控制的安全边界

更新控制比启动、停止风险更高，原因是：

1. 更新器页面需要额外导航。
2. 更新过程中 ALAS WebUI 状态变化更复杂。
3. 更新可能导致 WebUI 重启或 session 断开。
4. 如果同时存在用户操作，容易出现控制竞态。

因此，更新控制应满足以下条件后再启用：

1. 能稳定进入更新器页面。
2. 能识别更新器页面的当前状态。
3. 能处理 WebSocket 断开和 WebUI 重启。
4. 能向 UI 返回「检查中」「可更新」「更新中」「已完成」「失败」等状态。
5. 能避免在 ALAS GUI 已经出现内部异常时继续发送 callback。

## 已知风险与根因定位

### `state_switch` 异常

局域网实例验证发现：通过 `session=NEW` 建立非浏览器 PyWebIO session 后，发送侧边栏配置按钮 callback，可能触发 ALAS GUI 内部异常：

```text
AttributeError: 'AlasGUI' object has no attribute 'state_switch'
```

这说明 ALAS GUI 对非浏览器 WS 客户端的某些点击路径不安全。

### `object has no attribute 'alas'`

该异常通常是状态机或 session 已经异常后的连锁问题。它可能发生在 ALAS GUI 继续执行后续页面初始化、任务日志或概览逻辑时。

### `SessionClosedException`

该异常通常说明 PyWebIO session 已关闭，但后台任务仍在请求浏览器事件，例如 `document.visibilityState`。常见触发场景包括：

- 用户关闭或刷新 ALAS WebUI 页面。
- WebSocket 劫持 session 被关闭。
- ALAS WebUI 内部 task handler 未及时停止。

### `An internal error occurred in the application`

这是 PyWebIO 对内部异常的通用包装，不是独立根因。应继续查看后续堆栈，例如 `state_switch` 或 `alas` 缺失。

## 推荐的完整设计

### 设计一：完整控制模式

适用于需要完整状态和控制能力的场景。

特点：

- 创建一个 PyWebIO session。
- 读取侧边栏配置列表。
- 依次点击配置页。
- 读取 `header_status` 和 `scheduler_btn`。
- 支持启动、停止控制。

优点：

- 功能完整。
- 可以获取多配置运行状态。
- 可以控制指定配置。

缺点：

- 依赖 ALAS GUI 内部页面结构。
- 需要发送侧边栏 callback。
- 在部分 ALAS 实例上可能触发 `state_switch` 异常。

适用条件：

- ALAS WebUI 对 `session=NEW` 点击配置页稳定。
- 用户接受 WebSocket 模式具有实验性。
- 有完整异常熔断和错误提示。

### 设计二：安全只读模式

适用于优先避免 ALAS GUI 崩溃的场景。

特点：

- 只连接首页。
- 只读取侧边栏配置列表。
- 不点击侧边栏配置按钮。
- 不读取运行状态。
- 不提供启动、停止和更新控制。

优点：

- 不触发配置页点击路径。
- 对 ALAS GUI 干扰最小。
- 适合连接测试和配置发现。

缺点：

- 无法获取运行状态。
- 无法控制配置开关。
- 功能明显弱于 Overlay Runtime 模式。

适用条件：

- 用户只需要验证 ALAS WebUI 可连接。
- 用户手动在 ALAS WebUI 中操作配置。
- ALAS 侧边栏不提供运行状态。

### 设计三：混合降级模式

这是推荐的长期设计。

特点：

1. 默认先使用安全只读模式。
2. 如果用户明确启用「完整控制」，才允许点击配置页。
3. 一旦检测到 ALAS GUI 内部异常，立即熔断并回退到只读模式。
4. UI 明确提示当前能力边界。

状态机如下：

```text
只读模式
  → 用户启用完整控制
  → 完整控制模式
  → 检测到 ALAS GUI 内部异常
  → 熔断
  → 回退只读模式
```

熔断关键词包括：

- `An internal error occurred in the application`
- `AttributeError`
- `object has no attribute 'state_switch'`
- `object has no attribute 'alas'`
- `SessionClosedException`

优点：

- 默认安全。
- 保留高级能力。
- 出错后不会反复触发 ALAS 崩溃。

缺点：

- UI 和状态机更复杂。
- 需要清晰说明 WebSocket 模式的实验性。

### 设计四：短会话扫描模式

特点：

- 每次扫描创建一个短 WebSocket session。
- 扫描完成后立即关闭。
- 控制命令也使用一次性 session。

优点：

- 不长期占用 PyWebIO session。
- 减少与用户浏览器操作重叠的时间窗口。

缺点：

- 如果仍然点击侧边栏配置，仍可能触发 `state_switch`。
- 连接开销更高。
- 状态刷新不如常驻 session 实时。

结论：

短会话只能降低干扰频率，不能解决「点击配置页 callback 会触发 ALAS GUI 内部异常」这个根因。

### 设计五：ALAS 侧 API 模式

这是根治方案。

由 ALAS 或配套插件提供稳定 API，例如：

```text
GET /api/configs
GET /api/configs/<name>/status
POST /api/configs/<name>/start
POST /api/configs/<name>/stop
POST /api/update/check
POST /api/update/apply
```

优点：

- 不依赖 PyWebIO 页面结构。
- 不需要模拟点击。
- 多配置状态和控制语义清晰。
- 适合长期维护。

缺点：

- 需要修改 ALAS 或增加插件。
- 超出单纯 Alas-Gyre WebSocket 劫持范围。

## 最终建议

当前 WebSocket 劫持应明确分为 2 个能力等级：

| 能力等级 | 默认建议 | 说明 |
|----------|----------|------|
| 连接测试和配置发现 | 默认启用 | 安全，只读取首页和侧边栏。 |
| 多配置状态和控制 | 实验性启用 | 需要点击 ALAS 配置页，可能触发 ALAS GUI 内部异常。 |

推荐落地策略：

1. WebSocket 模式默认只做连接测试和配置发现。
2. 启动、停止、更新控制保留给 Overlay Runtime 模式。
3. 若必须保留 WebSocket 完整控制，应增加显式开关和风险提示。
4. 检测到 ALAS GUI 内部异常时立即停止发送 callback。
5. 文档中明确说明：ALAS WebUI 当前不提供 URL 可寻址配置页，也不提供安全的多配置运行状态接口。

## 实现检查清单

### 多配置

- [ ] 从 `alas-instance-` scope 提取配置名。
- [ ] 配置名去重并保持页面顺序。
- [ ] 每个配置维护独立状态、错误和按钮缓存。
- [ ] 不把一个配置页的状态套用到其他配置。

### 参数

- [ ] `ip`、`port` 使用默认值并转为字符串。
- [ ] `action` 使用白名单。
- [ ] `current_config` 不作为可靠路由入口。
- [ ] callback `data` 使用 PyWebIO 按钮值。
- [ ] localStorage 只作为浏览器模拟，不作为状态来源。

### 状态

- [ ] 优先使用 `header_status`。
- [ ] 其次使用 `scheduler_btn` 反推。
- [ ] 无证据时不更新状态。
- [ ] 状态统一归一化为 `running`、`idle`、`error`、`update`、`disconnected`。

### 控制

- [ ] 控制命令进入队列。
- [ ] 同一配置只保留最后一次命令。
- [ ] 控制前重新收集按钮，避免使用过期 callback。
- [ ] 控制失败写入 `control_errors`。
- [ ] ALAS GUI 内部异常时停止继续发送 callback。

### 更新

- [ ] 更新控制保持关闭，直到更新页面导航和状态识别稳定。
- [ ] 启用前必须定义更新状态机。
- [ ] 启用前必须处理 WebUI 重启和 WebSocket 断开。

### 安全

- [ ] 默认不在安全模式下点击侧边栏配置。
- [ ] 完整控制模式必须显式启用。
- [ ] 识别 `state_switch`、`alas`、`SessionClosedException` 等异常。
- [ ] 异常后熔断并回退到只读能力。
