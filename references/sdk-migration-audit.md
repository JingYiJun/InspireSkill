# SDK 迁移审计（v7.1.8 基线）

本清单对应 `codex/python-sdk`。用户确认保留原包依赖与默认安装方式；Rust 重写不在本次范围。SDK 为实验性入口，真实创建闭环仍是发布门禁。

## 1. 运行环境矩阵

| 环境 | 支持范围 | 证据与限制 |
|---|---|---|
| 本机/控制节点 | 首发目标 | 本机真实工作区、本人任务列表、实例和日志读取成功；无真实写操作 |
| CPU 节点 | 条件支持 | 需要平台可达、可用账号缓存、本地锁；未在 CPU 节点实测 |
| GPU 训练容器 | 不保证 | 未测试容器网络、认证供应和浏览器；建议训练进程不依赖控制 SDK |
| wheel 安装 | 原依赖保留 | `pyproject.toml` 只增加 py.typed 包数据；不新增 extras |
| 导入/过期缓存 | 默认禁浏览器 | `test_imports_are_lazy` 与 `test_browser_disabled_on_expiry`；构造不联网 |

## 2. 可变状态与所有权

| 现有位置/状态 | SDK 读写与清理路径 | 处理 |
|---|---|---|
| session.__init__ 的 `_BROWSER_API_FORCE_BROWSER` | SDK 不进入旧 `request_json` | Transport.request 操作局部 browser 标记 |
| session.__init__ 的 `_unproven_rebuild` / lock | SDK 不进入旧请求刷新分支 | Transport._refresh 使用账号锁、锁后重读 |
| browser_api.core `_cached_base_url` / key | `_get_base_url` 优先 active_transport | 固定 Client.base_url；CLI 默认路径保留 |
| session.requests `_pooled_by_thread` / lock | SDK 只调用 build_requests_session/_configure | Transport._http 自有；close 仅关闭自身 |
| session.browser_client TLS/WeakSet/locks | SDK 直接持有 `_BrowserRequestClient` | Transport._browser 自有；不调用全局关闭 |
| session 的 stale-handle ContextVar | SDK 不调用旧重放装饰器 | 新 runtime.active_transport 在 scope finally 恢复 |
| accounts 的当前账号 ContextVar | scope 进入/退出 account_scope | Client 固定账号；磁盘默认切换不改变 Client |
| 账号 session 文件/refresh lock | WebSession.load/save + exclusive_session_refresh | 继续复用账号原子写和锁；锁等待有界 |
| login_guard 文件 | guarded_credential_submission | 保留现有冷却；新增 retry_at 属性，无新登录绕路 |
| Client pid/thread/deadline/session/http/browser | Transport.check/scope/close；SDK facade 仅持有 Client 引用 | 跨线程/fork 拒绝；嵌套 deadline 使用较早者 |

认证路径在账号刷新锁内依次重读更新缓存、SSO Cookie 续期、受登录 guard 保护的 requests CAS 登录；仅 allow_browser=True 时回退既有 auth.get_web_session。验证码要求直接暴露为 AuthenticationError，冷却保留 retry_at。浏览器登录的固定内部超时无法提供硬取消；公开文档明确这一限制。状态与同步检查见 SDK 隔离测试，账号锁与 guard 的进程级合同继续由现有测试覆盖；新 SDK 多进程真实刷新尚未联调。

## 3. 调用方声明的传输策略与写路径重放审计

`operation` 的 `Transport.scope(timeout=...)` 默认 READ。实际写入调用显式进入 `single_send(operation_id, create=True)`（创建）或 `single_send()`（其他变更），create/mutation 由参数声明。已删除 `_READ_ACTIONS` 和 `operation_policy()`；URL、Action 和 HTTP 动词均不参与策略推断。

| 入口 | READ | single_send |
|---|---|---|
| 缓存加载 / 无浏览器续期 | 发送前认证；Chromium 仅限 allow_browser=True | 从未成功或空闲满 60 秒时，先以 READ GetUserDetail 探测；失败不发送写入、不包装为已发送不确定 |
| requests 异常 | 三次总尝试；allow_browser=True 才能换通道 | 已发送失败，不重放 |
| HTTP 401/3xx | 不受 allow_browser 限制，先无浏览器续期并重试一次；再次失效抛 AuthenticationError | 发送前探测按 READ 续期；实际写入发出后不刷新、不重发，创建为 SubmissionUncertainError，其他变更为 MutationUncertainError |
| HTTP 429/5xx | 三次总尝试，共享 deadline 和退避预算 | 不重试 |
| v2 transient envelope | 仅按共享 `_is_transient_v2_error_code` 判定是否重试 | JSON 原样交给 browser_api；解包失败由 block 映射为不确定 |
| v1 / 任意 JSON 形状 | 原样返回，无形状门控 | 原样返回 |
| JSON 解析失败 | TransportError，requests 异常仍遵循上面的请求异常策略 | 不确定 |
| 一般 HTTP 4xx | ValidationError，保留状态和约 500 字符正文 | 已发送失败映射为不确定 |
| 创建返回缺少 ID | 不适用 | Job/HPC/Ray/Serving 为 SubmissionUncertainError；Notebook/TensorBoard 使用共享只读确认，失败不重提；API key 成功返回 ref=None |
| 同一 block 第二次 request | 不适用 | RuntimeError，禁止第二次发送 |

Notebook 保存镜像与可见性更新是两个明确的 single_send；模型删除的引用／pending 预检位于写入前，force 只跳过该预检。

发送后的创建失败为 `SubmissionUncertainError(operation_id)`，其他变更失败为 `MutationUncertainError`。operation_id 是任意非空本地诊断字符串，默认 uuid4().hex，不是服务器幂等键。普通 ValueError 和平台错误映射保留 `str(error)`，并继续搜索原因链中的 SDK 错误。

## 4. 共享服务、替身落点与 AST 门禁

共享实现位于 `cli/inspire/services/`，下表列出全部业务模块（不含包初始化文件）。SDK 和 CLI 直接消费公开服务符号，不通过 Click 运行命令。

| 能力 | 服务模块 |
|---|---|
| 账号与目录 | `account_check`, `account_context`, `projects`, `images`, `dataset_catalog`, `datasets`, `models` |
| 资源与公共工具 | `collections`, `compute_groups`, `identifiers`, `image_resolution`, `quotas`, `raw_ids`, `resource_availability`, `resource_usage`, `task_priority`, `text`, `workload_quota`, `metrics` |
| Job | `job_events`, `job_logs`, `job_status`, `job_submission` |
| Notebook | `notebook_output`, `notebook_status`, `notebooks` |
| HPC | `hpc_events`, `hpc_instances`, `hpc_logs`, `hpc_output`, `hpc_status`, `hpc_submission` |
| Ray | `ray_events`, `ray_instances`, `ray_logs`, `ray_output`, `ray_scaling`, `ray_status`, `ray_submission` |
| Serving | `serving_access`, `serving_api_metrics`, `serving_events`, `serving_instances`, `serving_logs`, `serving_output`, `serving_status`, `serving_submission`, `serving_views` |
| TensorBoard | `tensorboards`, `tensorboard_data` |
| 镜像／模型写入 | `image_writes`, `model_writes` |

**Patch-target 策略：** 测试替身 patch 实际调用方读取的名字。CLI 的旧 wrapper／模块导入别名是既有测试兼容面；仅为此需要保留私有旧别名，不能把它们重新作为 SDK 依赖。SDK 测试 patch SDK 引用的 browser_api 模块或共享服务公开函数；已经绑定的 callable 必须 patch 其消费者，不能假定修改聚合导出就会替换绑定。HPC/Ray 的 list binding 经 lambda 读取各自 API 模块，Serving 的列表调用读取聚合 browser_api；分页测试按这个落点替换。

CLI 从 services 导入时使用公开符号，可以 `as _legacy_name` 兼容旧测试；SDK 从 services 的导入和模块属性访问都不得引用私有名字。纯服务不导入 CLI、Click、Rich 或 Playwright。SDK 的业务复用由 payload／公开视图等价测试验证，不以静态命中数充当覆盖率。

- `test_sdk.py::test_services_and_sdk_do_not_import_cli_or_ui_dependencies` 递归 AST 扫描 sdk/services，解析绝对与相对导入，拒绝 CLI/UI 依赖；检查 SDK 私有服务导入和解析别名后的私有服务属性访问。
- `test_sdk.py::test_imports_are_lazy` 在独立解释器中导入全部 SDK 模块及共享服务，验证没有加载 Click、Rich、Playwright，并保留公开导出兼容性。
- `test_sdk_signatures.py::test_all_facade_signatures` 从 InspireClient 实例属性发现全部门面，使用 inspect.signature 检查首参数、关键字边界、分页默认值、提交／注册命名与批量 status 的 Sequence／tuple 注解。内部辅助方法使用私有名，新增门面自动进入检查。
- `test_sdk*.py` 覆盖账号／来源／线程隔离、超时和只读重试、单次写入、不确定结果、共享创建载荷、目录分页、名称消歧、状态等待和日志／事件／指标。HPC 拒绝 page_size>50 的回归与 HPC/Ray/Serving 扩大第一页的测试使用假平台数据。

这些检查均可离线运行，不证明真实平台写操作或环境可用性。真实验证由 reviewer 在 sdk.md 的专用记录节填写。

## 日志与共享服务

CLI 与 SDK 的日志窗口解析、fetch 和 tail/head/limit 选择共用 `services/job_logs.py`。SDK 默认不截断字符，CLI 保持默认字符预算及输出净化。两者共享底层拉取和排序选择，但不把有限平台样本宣称为全局最后 N 条或无损游标。SDK 新增轮询 follow，终态停止并为日志保留最后一轮读取。

事件过滤、实例选择和合并查询在 `services/job_events.py`；指标选择和时间解析在 `services/metrics.py`，样本提取仍共用 browser_api.metrics；数据集语法和解析在 `services/datasets.py`，CLI 保留 re-export，原来 patch CLI 内部 validator 的测试已改为 patch 共享服务。状态集合集中到 `services/job_status.py`，CLI list --active / wait / logs 均从这里读取。

## 发布前剩余门禁

真实受控创建→查询→日志→终态→清理尚未执行；CPU/GPU 环境不从本机测试外推；浏览器登录硬取消、多进程 SDK 真实刷新、动态列表快照语义均不作为已完成能力。安装、类型与回归检查见实现交付记录。

## 检查记录

离线验收命令：在 `cli/` 执行 `uv run ruff check inspire tests`、`uv run mypy`、`uv run mypy --check-untyped-defs inspire/sdk`、`uv run pytest -q`、`uv build`；仓库根执行 `git diff --check`。真实平台记录只在 [SDK 指南](sdk.md#真实平台验证记录) 中由 reviewer 填写，不从离线测试推断。
