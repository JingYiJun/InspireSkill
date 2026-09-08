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
| Client pid/thread/deadline/session/http/browser | Transport.check/scope/close | 跨线程/fork 拒绝；嵌套 deadline 使用较早者 |

认证路径继续调用既有 auth.get_web_session，未重写底层登录。浏览器登录的固定内部超时无法提供硬取消；公开文档明确这一限制。状态与同步检查见 SDK 隔离测试，账号锁与 guard 的进程级合同继续由现有测试覆盖；新 SDK 多进程真实刷新尚未联调。

## 3. 调用方声明的传输策略与写路径重放审计

`operation` 的 `Transport.scope(timeout=...)` 默认 READ。实际写入调用显式进入 `single_send(operation_id, create=True)`（创建）或 `single_send()`（其他变更），create/mutation 由参数声明。已删除 `_READ_ACTIONS` 和 `operation_policy()`；URL、Action 和 HTTP 动词均不参与策略推断。

| 入口 | READ | single_send |
|---|---|---|
| 缓存加载 / 显式允许登录 | 发送前认证 | 失败保持原类型，不包装为已发送不确定 |
| requests 异常 | 三次总尝试；allow_browser=True 才能换通道 | 已发送失败，不重放 |
| HTTP 401/3xx | allow_browser=True 才允许一次刷新，否则 AuthenticationError | 不刷新、不重发 |
| HTTP 429/5xx | 三次总尝试，共享 deadline 和退避预算 | 不重试 |
| v2 transient envelope | 仅按共享 `_is_transient_v2_error_code` 判定是否重试 | JSON 原样交给 browser_api；解包失败由 block 映射为不确定 |
| v1 / 任意 JSON 形状 | 原样返回，无形状门控 | 原样返回 |
| JSON 解析失败 | TransportError，requests 异常仍遵循上面的请求异常策略 | 不确定 |
| 一般 HTTP 4xx | ValidationError，保留状态和约 500 字符正文 | 已发送失败映射为不确定 |
| 创建返回缺少 ID | 不适用 | SubmissionUncertainError，不追加详情查询 |
| 同一 block 第二次 request | 不适用 | RuntimeError，禁止第二次发送 |

发送后的创建失败为 `SubmissionUncertainError(operation_id)`，其他变更失败为 `MutationUncertainError`。operation_id 是任意非空本地诊断字符串，默认 uuid4().hex，不是服务器幂等键。普通 ValueError 和平台错误映射保留 `str(error)`，并继续搜索原因链中的 SDK 错误。

## 4. 业务抽取与测试替身清单

| 原调用点 | 共享实现 | 兼容方式 |
|---|---|---|
| cli.utils.job_submit | services.job_submission | CLI wrapper 保留 resolve_image_url 及原 build_training_job_plan 签名；SDK 传已解析 URL |
| cli.utils.quota_resolver | services.quotas | 纯类型/三元组解析/payload re-export；CLI 缓存和名称解析不迁移 |
| cli.utils.quota_cache | services.compute_groups | 计算组能力纯函数 re-export |
| job_commands 的终态集合 | services.job_status | CLI wait/logs 使用同一共享集合；SDK 同一词表 |
| browser_api 聚合导出 | 延迟 __getattr__ + TYPE_CHECKING | 原公共名字保留；测试仍 patch 实际 CLI 调用模块 |

Phase E 新增共享模块及兼容落点：

| 原调用点 | 共享实现 | 兼容方式 |
|---|---|---|
| serving_commands 创建镜像／模型／资源价格／域名解析 | services.serving_submission | CLI 保留私有别名及 Click 参数适配；SDK 公开核心调用，模型候选由各自名称选择器消歧 |
| Serving 生命周期等待 | services.serving_status | 独立 RUNNING 目标及 FAILED / ERROR / STOPPED / DELETED 终态，不套用 Job 成功词表 |
| serving public_output / access | services.serving_output / serving_access | CLI 兼容导出；SDK 返回相同公开视图与结构化调用信息 |
| serving 实例／版本／扩缩容历史视图 | services.serving_instances / serving_views | CLI 原名字兼容；SDK 使用公开名称，实例标签和 pod 映射一致 |
| serving events / logs | services.serving_events / serving_logs | 共享事件合并与 pod 日志读取；CLI 保留格式与展示预算 |
| serving api-metrics | services.serving_api_metrics | 核心使用 ValueError；CLI wrapper 保留 click.BadParameter；共享别名、窗口与摘要 |
| tensorboard_commands | services.tensorboards | 共享组与关联任务候选、创建后查找及 await_status；SDK single_send 后显式确认身份 |
| tensorboard_data | services.tensorboard_data | 共享标量集合、按 step 排序的摘要和尾部点集 |
| image_commands | services.image_writes | 可见性映射共享；平台等待继续使用 browser_api.images.wait_for_image_ready |
| model_commands | services.model_writes | 共享创建 ID 提取、所有版本引用、pending 检查和 in-use 文案；force 仅跳过 CLI 同款预检 |
| id_resolver / quota_resolver 纯名称检查 | services.identifiers / services.quotas | 抽取无 Click 的纯检查；CLI 公开函数兼容，禁止 SDK 经 CLI 依赖加载 Click |

新增 `test_sdk_servings.py`、`test_sdk_tensorboards.py`、`test_sdk_image_model_writes.py`；覆盖 dry-run 与创建参数等价、每个写方法的单次分派、原始平台错误链、创建缺 ID／确认失败、状态等待、实例事件日志、API 端点及流量摘要、镜像就绪轮询。`test_sdk.py::test_imports_are_lazy` 扩展到全部 Phase E 共享服务。

扫描口径：下表枚举当前测试源码中引用 job_submit、quota_resolver、quota_cache、job_commands、image_resolver 或 browser_api 聚合模块的文件，分类直接 import 与 patch/monkeypatch 语句。它是静态落点审计，不把命中次数当成测试覆盖率；未迁移的 CLI wrapper 保持原替身落点。SDK 新测试直接 patch SDK 实际消费的底层 API，额外验证 CLI/SDK payload 等价。

| 测试文件 | 相关模块 | 替身形式 |
|---|---|---|
| `test_account_commands.py` | browser_api | monkeypatch, import |
| `test_account_context_output.py` | browser_api | monkeypatch |
| `test_account_permissions_command.py` | browser_api | monkeypatch, import |
| `test_api_key_export_formats.py` | browser_api | monkeypatch, import |
| `test_batch_query_commands.py` | job_commands, browser_api | monkeypatch, import |
| `test_bridge_exec.py` | browser_api | monkeypatch |
| `test_browser_api_availability.py` | browser_api | monkeypatch, import |
| `test_browser_api_batch_query.py` | browser_api | monkeypatch, import |
| `test_browser_api_hpc_instance_events.py` | browser_api | monkeypatch, import |
| `test_browser_api_hpc_jobs.py` | browser_api | monkeypatch, import |
| `test_browser_api_jobs.py` | browser_api | monkeypatch, import |
| `test_browser_api_metrics.py` | browser_api | monkeypatch, import |
| `test_browser_api_models.py` | browser_api | monkeypatch, import |
| `test_browser_api_notebook_save_cancel.py` | browser_api | monkeypatch, import |
| `test_browser_api_project_and_notebook_selectors.py` | browser_api | monkeypatch, import |
| `test_browser_api_ray_jobs.py` | browser_api | monkeypatch, import |
| `test_browser_api_ray_logs.py` | browser_api | monkeypatch, import |
| `test_browser_api_servings.py` | browser_api | monkeypatch, import |
| `test_browser_api_tensorboards.py` | browser_api | monkeypatch, import |
| `test_browser_api_workspaces.py` | browser_api | monkeypatch, import |
| `test_cli_commands.py` | job_submit, quota_resolver, job_commands, browser_api | monkeypatch, import |
| `test_compact_local_lists.py` | browser_api | monkeypatch |
| `test_create_workload_options.py` | job_submit, quota_resolver, browser_api | monkeypatch, import |
| `test_dataset_commands.py` | browser_api | monkeypatch, import |
| `test_debug_logging.py` | browser_api | monkeypatch |
| `test_dry_run_and_batch.py` | job_submit, quota_resolver, browser_api | monkeypatch, import |
| `test_event_collection_boundaries.py` | browser_api | monkeypatch, import |
| `test_global_account.py` | browser_api | monkeypatch, import |
| `test_hpc_commands.py` | quota_resolver, browser_api | monkeypatch, import |
| `test_hpc_logs_and_instance_events.py` | browser_api | monkeypatch, import |
| `test_image_commands.py` | browser_api | monkeypatch, import |
| `test_job_create_output.py` | job_submit, quota_resolver | monkeypatch, import |
| `test_job_logs_output_budget.py` | browser_api | monkeypatch |
| `test_job_logs_web_follow.py` | browser_api | monkeypatch |
| `test_job_project_name_only.py` | job_submit, browser_api | monkeypatch, import |
| `test_job_shell.py` | job_commands, browser_api | monkeypatch, import |
| `test_model_commands.py` | browser_api | monkeypatch, import |
| `test_model_delete_command.py` | browser_api | monkeypatch, import |
| `test_name_pick_commands.py` | job_commands | monkeypatch, import |
| `test_node_events.py` | browser_api | monkeypatch, import |
| `test_notebook_account_isolation.py` | browser_api | monkeypatch |
| `test_notebook_cancel_save_image_command.py` | quota_resolver, browser_api | monkeypatch, import |
| `test_notebook_commands.py` | browser_api | monkeypatch, import |
| `test_notebook_create_flow.py` | quota_resolver, browser_api | monkeypatch, import |
| `test_notebook_gpu_model.py` | browser_api | monkeypatch |
| `test_notebook_install_deps.py` | browser_api | monkeypatch |
| `test_notebook_jupyter.py` | browser_api | monkeypatch, import |
| `test_notebook_jupyter_terminal.py` | browser_api | monkeypatch, import |
| `test_notebook_metrics.py` | browser_api | monkeypatch, import |
| `test_notebook_realtime_metrics.py` | browser_api | monkeypatch, import |
| `test_notebook_rtunnel_flow.py` | browser_api | monkeypatch, import |
| `test_notebook_rtunnel_helpers.py` | browser_api | monkeypatch, import |
| `test_notebook_rtunnel_probe.py` | browser_api | monkeypatch, import |
| `test_notebook_rtunnel_verify.py` | browser_api | import |
| `test_notebook_save_image_command.py` | browser_api | monkeypatch, import |
| `test_notebook_save_size_estimate.py` | browser_api | monkeypatch, import |
| `test_notebook_shell_transport.py` | browser_api | monkeypatch |
| `test_notebook_ssh_redesign.py` | browser_api | monkeypatch, import |
| `test_notebook_transport_policy.py` | browser_api | monkeypatch |
| `test_notebook_url.py` | browser_api | monkeypatch, import |
| `test_notebook_wait.py` | browser_api | patch, import |
| `test_project_list_commands.py` | browser_api | monkeypatch, import |
| `test_project_selection.py` | browser_api | monkeypatch, import |
| `test_quota_cache.py` | quota_resolver, quota_cache, browser_api | monkeypatch, import |
| `test_quota_resolver.py` | quota_resolver, browser_api | monkeypatch, import |
| `test_ray_commands.py` | quota_resolver, browser_api | monkeypatch, import |
| `test_ray_instances.py` | browser_api | monkeypatch, import |
| `test_ray_logs_command.py` | browser_api | monkeypatch, import |
| `test_ray_output_boundaries.py` | browser_api | monkeypatch |
| `test_ray_scaling_command.py` | browser_api | monkeypatch, import |
| `test_resource_index_refresh.py` | quota_cache, browser_api | monkeypatch, import |
| `test_resource_metrics_variants.py` | job_commands, browser_api | monkeypatch, patch, import |
| `test_resources_node_specs.py` | browser_api | monkeypatch, import |
| `test_resources_policy.py` | browser_api | monkeypatch, import |
| `test_resources_usage.py` | browser_api | monkeypatch, import |
| `test_sdk.py` | job_submit, browser_api | monkeypatch, patch, import |
| `test_serving_api_access.py` | browser_api | monkeypatch, import |
| `test_serving_api_metrics.py` | browser_api | monkeypatch, import |
| `test_serving_commands.py` | quota_resolver, browser_api | monkeypatch, import |
| `test_serving_logs_and_scale_history.py` | browser_api | monkeypatch, import |
| `test_task_priority.py` | browser_api | monkeypatch, import |
| `test_task_priority_wiring.py` | job_submit, quota_resolver, browser_api | monkeypatch, import |
| `test_tensorboard_commands.py` | browser_api | monkeypatch, import |
| `test_transient_api_errors.py` | browser_api | monkeypatch, import |
| `test_web_config_resolution.py` | browser_api | monkeypatch, import |
| `test_workload_node_placement.py` | job_commands | import |
| `test_workload_quota_and_resources.py` | browser_api | monkeypatch, import |
| `test_workload_selector_contract.py` | job_commands | monkeypatch, import |

## 日志与共享服务

Phase A 将 CLI 已有的日志窗口解析、fetch 和 tail/head/limit 选择提取到 `services/job_logs.py`。SDK 默认不截断字符，CLI 保持默认字符预算及输出净化。两者共享底层拉取和排序选择，但不把有限平台样本宣称为全局最后 N 条或无损游标。SDK 新增轮询 follow，终态停止并为日志保留最后一轮读取。

事件过滤、实例选择和合并查询在 `services/job_events.py`；指标选择和时间解析在 `services/metrics.py`，样本提取仍共用 browser_api.metrics；数据集语法和解析在 `services/datasets.py`，CLI 保留 re-export，原来 patch CLI 内部 validator 的测试已改为 patch 共享服务。状态集合集中到 `services/job_status.py`，CLI list --active / wait / logs 均从这里读取。

## 发布前剩余门禁

真实受控创建→查询→日志→终态→清理尚未执行；CPU/GPU 环境不从本机测试外推；浏览器登录硬取消、多进程 SDK 真实刷新、动态列表快照语义均不作为已完成能力。安装、类型与回归检查见实现交付记录。

## 检查记录

首版基线曾通过 2944 项测试（6 skipped）。Phase A 的完整命令及结果以本次交付总结 `phase_a_summary.md` 为准；测试包括 caller-declared single_send、transient envelope、cursor 相等验证、日志窗口和 CLI 选择等价、完整创建 payload、批量状态、事件筛选、结构化实例和指标样本。真实平台受控创建闭环仍留在阶段 F。
