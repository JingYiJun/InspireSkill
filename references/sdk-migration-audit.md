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

## 3. 写路径重放审计

| 入口 | 发送前后判定 | SDK 合同/测试 |
|---|---|---|
| 缓存加载/显式允许登录 | 创建发送前 | 失败为认证错误，不包装为已发送不确定 |
| requests RequestException / ReadTimeout | 已进入一次发送 | CREATE → SubmissionUncertainError；无换通道 |
| HTTP 401/3xx | 已发送，未证明未执行 | CREATE 不刷新、不重发 |
| HTTP 429/5xx | 已发送 | CREATE 不重试；READ 共用三次预算 |
| JSON 无法解析/非 envelope | 服务端可能已执行 | CREATE 不确定 |
| envelope 暂时错误 | 已发送 | READ 在同一预算内退避；CREATE 不确定 |
| 明确参数/权限拒绝 | 已有拒绝证据 | 净化 ValidationError/AuthenticationError，无重放 |
| create 返回缺少 ID | 可能成功但不可确认 | 不确定；不追加详情查询 |
| browser 请求认证失败 | 已进入浏览器通道 | CREATE 不重建重放 |
| 未知 Action / GET | 无已审计幂等合同 | 默认 MUTATION，单次发送；不从 HTTP 方法猜测 |

OperationPolicy 只允许显式已审计的只读 Action 自动重试。operation_id 为本地诊断 UUID，不是服务器幂等键。所有日志/错误去除服务端正文；业务程序主动打印日志不属于 SDK 自动诊断。

## 4. 业务抽取与测试替身清单

| 原调用点 | 共享实现 | 兼容方式 |
|---|---|---|
| cli.utils.job_submit | services.job_submission | CLI wrapper 保留 resolve_image_url 及原 build_training_job_plan 签名；SDK 传已解析 URL |
| cli.utils.quota_resolver | services.quotas | 纯类型/三元组解析/payload re-export；CLI 缓存和名称解析不迁移 |
| cli.utils.quota_cache | services.compute_groups | 计算组能力纯函数 re-export |
| job_commands 的终态集合 | services.job_status | CLI wait/logs 使用同一共享集合；SDK 同一词表 |
| browser_api 聚合导出 | 延迟 __getattr__ + TYPE_CHECKING | 原公共名字保留；测试仍 patch 实际 CLI 调用模块 |

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

## 日志协议探索

真实只读探针：同一已完成任务、一个实例、固定 24h 窗口，请求 5/10 条（SDK 内取 limit+1 以判断截断），返回 total 均为 10，较小样本是较大样本前缀。未读取或输出原始日志正文到审计文档；没有证明全局最近 N 条。多实例排序/重启实例保留、相同时间戳续读仍未建立平台合同，所以首版只发布 limit 样本，不发布 tail/follow/cursor。

## 发布前剩余门禁

真实受控创建→查询→日志→终态→清理尚未执行；CPU/GPU 环境不从本机测试外推；浏览器登录硬取消、多进程 SDK 真实刷新、动态列表快照语义均不作为已完成能力。安装、类型与回归检查见实现交付记录。

## 本次检查结果

- Ruff：通过。
- mypy：220 个源码文件通过；外部 wheel 消费示例另行通过。
- pytest：2944 passed，6 skipped；其中 SDK 合同测试 32 项。
- uv build：wheel 和源码包构建成功，wheel 包含 SDK 与 py.typed。
- 干净环境安装原依赖 wheel：SDK 惰性导入和 CLI --help 均通过。
- pyproject 的 project 元数据、依赖与 CLI 入口与基线相同。
- git diff --check：通过。
