# Python SDK 全量接入规划（codex/python-sdk）

基线：`6fedb72 feat(sdk): add experimental InspireClient python sdk`（初版，Codex 产出，Claude 审核）。
飞书设计文档第 1–10 节的方向不变；本文件是把"GPU Job 首版"扩展为"CLI 已接入的所有平台命令"的实施规划与审核记录。

## 0. 初版审核结论（2026-09-07）

保留：
- Client 自有 Transport / HTTP session / 浏览器实例，close 只关自己；跨线程、fork 拒绝。
- 写请求单次发送，发送后不做认证重放、浏览器换通道、暂时错误重试；失败映射为 `*UncertainError`。
- `browser_api` 聚合导出惰性化；`import inspire` / 导入 SDK 不加载 Playwright / Click。
- 业务逻辑下沉到 `inspire/services/`（job_submission、quotas、compute_groups、job_status），CLI 与 SDK 共用，测试替身落点不变。
- 名称精确匹配 + `AmbiguousResourceError(candidates)`；类型化 ref；`Page(items, next_cursor, total)`。

必须修改（用户明确要求：避免额外门控，减少无意义的 sha256）：

| # | 位置 | 问题 | 处理 |
|---|---|---|---|
| 1 | `sdk/resources.py::cursor_offset` | 用 sha256 指纹绑定 cursor 与查询 | 去掉 hashlib；cursor 直接编码 `{offset, query}`，读取时按相等比较 |
| 2 | `sdk/transport.py::_READ_ACTIONS` / `operation_policy` | 只读重试靠 Action 白名单；未列出的 Action 一律当 MUTATION 且错误包装成 Uncertain。扩展到全部工作负载后每个新 Action 都要登记，漏登记会静默降级 | 删除白名单。策略由调用方声明：`operation` 默认 READ；写方法只把真正的发送调用包进 `transport.single_send(...)`；不从 URL/HTTP 方法推断 |
| 3 | `sdk/transport.py::request` 的 envelope 形状检查 + 无条件 `_v2_result` | v1 端点（`/api/v1/...`）或非标准返回会被误判"Invalid platform envelope"，读操作重试三次后报 TransportError | 删除形状检查；只检测 v2 envelope 的 transient 错误码用于读重试，其余原样返回给 browser_api 函数解析 |
| 4 | `sdk/resources.py::operation` | 所有 ValueError 一律改写成固定文案，平台/业务错误信息丢失 | 保留原始 message（`str(error)`）；只做类型映射 |
| 5 | `sdk/client.py` | base URL 手工校验（username/query/fragment）；`_validate_session` 要求 `login_username == config.username` | 删除，与 CLI 同一套 Config/WebSession 语义 |
| 6 | `sdk/jobs.py` | `owner` 参数只接受 self；`window` 只接受 `1h/24h`；`instances` 必须在已发现列表内；`operation_id` 必须是 UUID；`delete` 前强制终态；"short intermediate page" 报错 | 去 `owner`；window 复用 CLI `30m/2h/1d` 解析；instances 直接透传；operation_id 任意字符串；delete 与 CLI 一致不预检；去 short-page 判定（保留去重和 100 页上限） |
| 7 | `JobCreateSpec` | 缺 CLI `job create` 的 framework/auto_fault_tolerance/…/dataset/env/keep_after_*/public_path_readonly/enable_notification/exclude_nodes/specified_nodes | 补齐，复用 `build_training_job_plan` 与 `resolve_dataset_info` |
| 8 | `Jobs` | 缺 status(batch)/metrics/command/events 过滤/logs tail-head | 按 CLI 语义补齐（见 §2） |

## 1. 范围：CLI 命令 → SDK

原则：凡是"对平台的读/写操作"都进入 SDK；纯本机/交互/安装类命令不进入，并在 `references/sdk.md` 明确列出。

进入 SDK（按 client 属性）：

| CLI 组 | SDK 属性 | 方法（与 CLI 子命令一一对应） |
|---|---|---|
| account | `client.account_info` | `current()`、`check()`、`context()`、`permissions(workspace)`；`client.api_keys`: `list()`、`create(name)`、`delete(name)`、`get(name)`（对应 export 的值获取，文件/环境导出由调用方处理） |
| project | `client.projects` | `list(workspace)`、`get(name, workspace)`、`detail(name, workspace)`、`owners(workspace)` |
| image | `client.images` | `list(workspace, source, keyword)`、`get`、`detail`、`register(...)`、`wait_ready`、`delete`、`set_visibility` |
| dataset | `client.datasets` | `list(keyword, tag)`、`get(name)`、`tags()`、`applications(name, to_approve, keyword)`、`validate(specs, workspace)` |
| model | `client.models` | `list(workspace, project, keyword)`、`get/status`、`versions`、`deploy_config`、`register(...)`、`delete(...)` |
| resources | `client.resources` | `availability(workspace, group, include_cpu)`、`policy(workspace, workload)`、`usage(...)`、`node_events(nodes, ...)` |
| job | `client.jobs` | `list/iter/get/status(names)/plan/create/delete/stop/wait/events/instances/logs/metrics/command/quotas` |
| notebook | `client.notebooks` | `list/get/status/plan/create/delete/start/stop/wait/events/lifecycle/metrics/quotas/save_image/cancel_save_image` |
| hpc | `client.hpc` | `list/get/status/plan/create/delete/stop/events/instances/logs/metrics/quotas` |
| ray | `client.ray` | `list/get/status/plan/create/delete/start/stop/events/instances/logs/metrics/quotas/scaling` |
| serving | `client.servings` | `list/get/status/plan/create/delete/start/stop/scale/rollback/versions/scale_history/configs/api/api_metrics/events/instances/logs/metrics/quotas` |
| tensorboard | `client.tensorboards` | `list/get/status/create/delete/start/stop/tags/scalars` |

不进入 SDK（CLI-only，文档说明）：`init`、`update`、`uninstall`、`cache *`、`account add/use/rename/remove/list`、`* batch`（YAML 驱动，SDK 用户自行循环）、`notebook ssh/shell/exec/scp/ssh-config/ssh-proxy/connection */install-deps/proxy-url`、`job/hpc/ray/serving shell`、`metrics --plot/--open/--sparkline`（SDK 返回结构化样本）。`--follow` 以生成器 `follow_events()/follow_logs()` 形式提供。

## 2. 公共合同（各工作负载一致）

- 选择器：`str`（精确名称，大小写不敏感）或对应 `*Ref`；名称歧义抛 `AmbiguousResourceError(candidates)`。
- `list(...)` 返回 `Page`；`iter(...)` 按 cursor 迭代；`limit` 默认 20。
- `plan(spec)` = CLI `--dry-run`：只读解析并返回 payload 摘要；`create(spec)` 重新解析后单次发送，返回 `*Handle(name, ref, operation_id)`。
- `wait(ref, timeout, poll_interval, raise_on_failure)` 使用各工作负载自己的终态词表（`services/<workload>_status.py`）。
- `logs(ref, instance, window|start/end, tail|head|limit)`：与 CLI 相同的语义与相同的底层调用；SDK 不做 CLI 之外的承诺。
- `events(ref, type, reason, instance, workload_level, limit)`；`follow_events(ref, interval)` 生成器。
- `metrics(ref, metric, window|start/end, interval, group)` 返回 `MetricGroup` 序列。
- 写操作错误：发送前失败→原始错误类型；发送后失败→`SubmissionUncertainError`（create）/`MutationUncertainError`（其它）。
- 错误信息保留平台/业务原文。

## 3. 阶段与门禁

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| A | §0 重构 + job 组补全 | 全部测试通过；job 组每个 CLI 子命令有 SDK 对应；CLI/SDK payload 等价测试 |
| B | account/project/image/dataset/model/resources 只读 | 同上 |
| C | notebook 组 | 同上 + 与 `notebook create` payload 等价 |
| D | hpc + ray | 同上 |
| E | serving + tensorboard + image/model/api-key 写操作 | 同上 |
| F | follow 生成器、文档、真实平台手动测试、飞书文档更新 | 只读全覆盖 + 受控创建/清理闭环 |

每阶段门禁（在 `cli/` 下）：`uv run ruff check inspire tests`、`uv run mypy`、`uv run pytest -q`、`uv build`、`git diff --check`。
每阶段结束由 Claude 审阅 diff，必要时回退 Codex 修改，直到双方满意后提交一个 commit。

## 4. 手动测试计划（真实平台，阶段 F）

只读：workspaces/projects/images/datasets/models/resources/account 全部 list/get；job/notebook/hpc/ray/serving/tensorboard 的 list/get/status/events/instances/logs/metrics 对既有资源各跑一遍，并与 `inspire ... --json` 输出交叉核对。
受控写：`kchen-sdk-smoke-*` 命名；CPU notebook create→wait→stop→start→delete；CPU job create→wait→logs→delete；tensorboard create→delete；hpc 最小 spec create→stop→delete。结束后确认无残留。
