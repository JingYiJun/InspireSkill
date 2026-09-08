# Python SDK（实验性入口）

同一个 `inspire-skill` 包现在提供 `from inspire import InspireClient`。本次新增同步 Python SDK，现有 CLI、依赖列表与默认安装方式保持不变，不拆分最小包或 browser extra。开发分支使用 `python -m pip install -e ./cli` 安装；这些接口尚未发布到 PyPI。

SDK 首版面向可访问平台的本机或控制节点。CPU 节点仅在网络、账号缓存和文件锁语义满足要求时条件支持；不保证 GPU 容器内部调用。它不是训练代码必须引入的运行时依赖。

## 接入已有 Python 项目

先按 CLI 的账号初始化流程准备账号与有效登录缓存，再在应用中创建 Client。构造和导入不连接平台；第一次资源操作才读取会话。账号在构造时固定，后续切换 CLI 默认账号不会改变已有 Client。

```python
from inspire import InspireClient

with InspireClient(account="my-account") as client:
    workspaces = client.workspaces.list(limit=20)
    for workspace in workspaces.items:
        print(workspace.name)

    workspace = client.workspaces.get("工作区名称")
    jobs = client.jobs.list(workspace=workspace.ref, limit=10)
    for job in jobs.items:
        print(job.name, job.status)
```

默认 `allow_browser=False`，不自动登录或启动 Chromium。缓存失效返回 `AuthenticationError`，应用可以提示用户通过 CLI 恢复认证。需要由控制节点自动登录/浏览器传输时显式指定 `allow_browser=True`；仍需按现有 CLI 安装说明准备 Chromium。SDK 各模块和 Notebook 共享服务的导入不会加载 Playwright 或 Click。

一个同步 Client 只允许在创建它的进程和线程中使用。线程 worker 或 fork 子进程必须各自创建 Client，并用 `with` 或 `close()` 释放资源。不同 Client 的 HTTP session、浏览器和关闭动作相互独立；账号磁盘缓存、刷新锁和登录冷却仍与 CLI 共用。同账号多个进程需在本地文件锁有效的文件系统上运行；未验证任意网络共享盘锁语义。

## 发现资源与确定性选择

| 服务 | 接口 | 约束 |
|---|---|---|
| `workspaces` | `list/get` | 完整名称匹配 |
| `projects` | `list/get/detail/owners` | workspace 省略时查询全局项目；detail 含预算用量 |
| `compute_groups` | `list/get` | 必须指定 workspace |
| `images` | `list/get/detail` | official/public/project/private；list 支持 keyword；跨来源同名报歧义 |
| `jobs` | `quotas` | 指定 workspace；group 可省略以枚举所有支持训练的计算组 |
| `jobs` | `list/iter/get` | 与 CLI 一样列出当前用户任务，无 owner 参数；名称 get 完整消歧 |

`list()` 返回 `Page(items, next_cursor, total)`；通过 `cursor=page.next_cursor` 继续，`total=None` 表示该查询没有可靠总数。任务列表按原始页按需读取；本地状态过滤可能扫描多页。名称解析需要扫描所有匹配任务，超过 100 页或平台返回重复/缺失页时返回 `ResolutionIncompleteError`，不会根据不完整结果挑选第一个。

其他资源目录先完整枚举再切出有界返回页。游标是 JSON `{"offset": int, "query": [...]}` 的 urlsafe base64 编码。query 保存账号、服务端、服务和筛选条件；读取时按普通相等比较验证，不使用哈希或签名。它是当前列表的偏移标记，不是服务端快照；任务并发新增/删除时可能重复或遗漏。`jobs.iter(max_items=...)` 去重已经看到的引用，但不承诺快照完整性。

使用 `.ref` 在后续操作中保持身份。引用可 `to_dict()` / `JobRef.from_dict()` 序列化；包含内部资源身份但不包含认证材料，默认 repr 隐藏 key。引用不授予权限，也不绕过服务器校验。跨账号、跨来源、跨工作区或资源类型不匹配会报错；空 workspace_id 表示未限定工作区，适用于所有引用类型。公开选择器不接受裸 ID。

```python
from inspire import ImageSelector, Quota

# 下列代码位于同一个 Client 上下文中。
image = client.images.get(
    ImageSelector(name="训练镜像:v1", source="private"),
    workspace=workspace.ref,
)
quotas = client.jobs.quotas(workspace=workspace.ref, group="计算组名称")
# 精确要求 1 GPU、20 CPU、200 GiB；不会自动选择相近规格。
quota = Quota(gpu=1, cpu=20, memory_gib=200)
```

## 资源查询方法（Phase B）

下面的接口复用 CLI 的 browser_api／数据广场函数和共享 services，不通过 Click 执行命令。返回值为 frozen dataclass；有平台身份的目录记录提供对应类型的 `.ref`。项目、镜像、数据集、模型与 usage 等视图提供 `.to_dict()`，返回 CLI JSON 的业务字段，不含 SDK 引用；嵌套列表和字典保留平台视图结构，frozen 不表示递归冻结。

| CLI | SDK 方法 | 返回值与范围 |
|---|---|---|
| account current | `account_info.current()` | AccountInfo：固定账号 alias、username、base_url、user_id、user_name；CLI current 的 name 对应 alias |
| account check | `account_info.check()` | AccountCheck：ok、user、session_created_at、issues；共享占位主机和凭据校验，使用 `get_current_user(refresh=True)` 验证会话；不返回密码 |
| account context | `account_info.context(limit=None)` | AccountContext；实时枚举项目、工作区、计算组及警告；None 不裁剪，指定 limit 时每类分别裁剪 |
| account permissions | `account_info.permissions(workspace=None)` | tuple[Permission, ...]；None 或 `"all"` 遍历全部工作区，单个工作区可用名称或 WorkspaceRef |
| account api-key list | `api_keys.list(limit=20, cursor=None)` | Page[APIKeyInfo]；只有名称、创建时间和 APIKeyRef |
| account api-key export 的密钥读取 | `api_keys.get(name_or_ref)` / `api_keys.plaintext(name_or_ref)` | 元数据 APIKeyInfo / 显式返回明文 str；名称完整匹配，同名须选 Ref |
| account api-key create / delete | `api_keys.create(name)` / `api_keys.delete(name_or_ref)` | APIKeyInfo；写入使用 single_send，无额外确认、预查重或写后重列循环 |
| project list / detail / owners | `projects.list(workspace=None, limit=20, cursor=None)` / `projects.detail(name_or_ref, workspace=None)` / `projects.owners()` | Page[ProjectInfo] / ProjectDetail / tuple[ProjectOwner, ...]；全局列表使用 list_all_projects，详情预算用量不可读时与 CLI 一样保留其余详情 |
| project 名称选择 | `projects.get(name_or_ref, workspace=None)` | ProjectInfo；可传工作区筛选，也可直接查询全局项目 |
| image list / detail | `images.list(workspace, source=None, keyword=None, limit=20, cursor=None)` / `images.detail(name_or_ref, workspace)` | Page[Image] / ImageDetail；None 或 all 按 official/public/project/private 顺序读取、按 ID 保留首条；keyword 为名称子串 |
| dataset list / show | `datasets.list(keyword=None, tag=None, limit=20, cursor=None)` / `datasets.get(name)` | Page[DatasetInfo] / DatasetDetail；tag 可为名称或名称序列；详情含 DatasetVersion 和可用的版本引用 |
| dataset tags / applications | `datasets.tags()` / `datasets.applications(name=None, to_approve=False, keyword=None, limit=20, cursor=None)` | tuple[DatasetTag, ...] / Page[DatasetApplication]；指定 name 时复用 CLI 的单数据集申请详情查询，keyword 与 CLI 一样仅用于未指定 name 的列表 |
| dataset validate | `datasets.validate(specs, workspace)` | tuple[DatasetValidation, ...]；接受 `"name:version"` 或 DatasetMount，复用解析及重复检测；平台拒绝以 mountable=False、reason 原文返回，格式错误抛 ValidationError |
| model list / 名称选择 | `models.list(workspace, project=None, keyword=None, limit=20, cursor=None)` / `models.get(name_or_ref, workspace, project=None)` | Page[ModelInfo] / ModelInfo；当前用户模型；list 支持 workspace="all"，其余模型方法指定单工作区 |
| model status | `models.status(name_or_ref, workspace, project=None)` | ModelStatus；与 CLI JSON 一致地组合详情、版本记录、vLLM、pending_serving、servings 和 other_versions_in_use；servings 保留 CLI 的 20 条窗口及截断字段 |
| model versions / deploy-config | `models.versions(name_or_ref, workspace, project=None)` / `models.deploy_config(name_or_ref, workspace, project=None, version=None)` | tuple[ModelVersion, ...] / ModelDeployConfig；version 缺省时从目录版本推断 |
| resources availability | `resources.availability(workspace, group=None, include_cpu=False)` | tuple[ResourceAvailability, ...]；共享 CLI 排序和公开字段，group 为名称子串 |
| resources policy | `resources.policy(workspace, workload=None)` | tuple[WorkloadSchedulePolicy, ...]；包含平台原始调度声明、回收条件和时间限制 |
| resources usage | `resources.usage(workspace, project=None, user=None, task=None, group=None, mine=False, details=False, limit=None)` | ResourceUsage；scope、filters、compute_groups、items 及裁剪元数据与 CLI 相同；三种 scope 为 project-user / task / mine，None 不裁剪 |
| resources node-events | `resources.node_events(nodes, since=None, type=None, reason=None, limit=None, from_component=None)` | EventResult；nodes 为名称或名称序列，读取 CLI 同样的最新 1,000 条扫描窗口；items 保留事件原始字段；since 为 datetime 或 Unix 秒；limit 取过滤后最新 N 条 |

API key 创建接口没有返回 ID 或创建时间，因此 `create()` 的成功结果只有 name，`ref=None`、`created_at=""`。之后可显式调用 `get(name)` 取得引用。SDK 不伪造 ID，也不让额外查询改变单次写入的结果。`plaintext()` 是唯一返回密钥值的接口；文件导出和子进程环境注入由调用方自行处理。

目录分页使用公共 Page 和游标。数据集与模型目录按平台页完整枚举后切片；跨页重复身份会去重；总数仍有剩余却返回空页，或达到 100 页上限仍未完成枚举时抛 ResolutionIncompleteError。单数据集 applications 复用 CLI 的有界详情扫描，total=None，不承诺扫描窗口之外的完整申请历史。镜像 list 与 CLI 一样可返回成功来源的数据；名称 get/detail 仍要求完整候选集，避免漏掉其他来源的同名对象。

resources availability、policy、usage 与 CLI 一样只接受单工作区；模型 list 和账号 permissions 支持工作区 fan-out。usage 的 project/user/task/group 都是 CLI 的子串过滤，mine 与 group/user/task/details 不可组合。

CLI-only：账号本地管理 `account add/use/rename/remove/list`、`api-key export` 的文件格式／权限／stdout 和 `api-key run` 的子进程及环境处理、`init/update/uninstall/cache`、YAML batch、SSH/shell/exec/scp/连接安装与代理入口，以及指标绘图和终端渲染。image/model 注册、更新和删除仍属于后续写操作阶段，尚未加入这些资源门面。

## 规划、提交和等待

```python
from inspire import InspireClient, JobCreateSpec, Quota, SubmissionUncertainError

with InspireClient(account="my-account") as client:
    spec = JobCreateSpec(
        name="sdk-training-example",
        workspace="工作区名称",
        project="项目名称",
        group="计算组名称",
        quota=Quota(gpu=1, cpu=20, memory_gib=200),
        image="训练镜像:v1",
        command="python /workspace/train.py",
        nodes=1,
        shm_gib=64,
        max_time_hours=1,
    )
    plan = client.jobs.plan(spec)
    print(plan.summary)  # 不含 command 或环境变量值，不预留资源。
    try:
        handle = client.jobs.create(spec)
    except SubmissionUncertainError as exc:
        print("提交结果待核查，诊断号：", exc.operation_id)
        raise  # 查询候选任务并核查；不要在这里自动再次 create。
    result = client.jobs.wait(handle.ref, timeout=3600, raise_on_failure=True)
    print(result.status)
```

`plan()` 执行实时只读资源解析，检查项目、镜像、训练计算组能力、精确规格和优先级，复用 CLI 的训练 payload 构建服务。`create()` 会重新规划，避免把预检当作预约；它只发送一次创建请求，并直接返回 `JobHandle`，不以提交后的详情查询决定创建是否成功。

`operation_id` 可由调用方传入任意非空字符串，默认 `uuid4().hex`，仅用于关联诊断，不是平台幂等键，未写入平台。它不支持自动按诊断号找到任务。创建后响应丢失、JSON 损坏、会话失效、暂时错误或缺少 ID 均可能返回 `SubmissionUncertainError`。发送后的失败均视为写入不确定；发送前的参数、认证和资源解析错误保持相应错误类型。读请求最多三次总尝试；写请求不进行认证重放、浏览器换通道重放或暂时错误重试。

`wait()` 首次解析后固定 JobRef，使用共享状态词表；UNKNOWN 不视为成功。超时仅结束等待，不停止或删除远端任务。默认返回任何终态，`raise_on_failure=True` 在失败/取消时抛出 `JobFailedError`。`jobs.stop(ref)` 解析引用后直接停止，不额外读取详情；`jobs.delete(ref)` 解析引用后直接删除，不做终态预检，由平台决定是否允许。

## 完整创建字段

`JobCreateSpec` 与 CLI `job create` 的创建选项对应，SDK 和 CLI 共用 `services.job_submission.build_training_job_plan`。数据集共用 `services.datasets.resolve_dataset_info` 校验并解析挂载路径。

| 字段 | 类型 / 默认值 | 含义 |
|---|---|---|
| name / command | 必填 str | 名称与启动命令；command 不进入 repr 或摘要 |
| workspace / project / group | 名称或对应 Ref，必填 | 作用域与计算组 |
| quota | Quota 或 QuotaRef，必填 | 精确资源规格 |
| image | 名称、registry URL、ImageRef 或 ImageSelector，必填 | 镜像选择 |
| framework | str，`"pytorch"` | 平台框架标签 |
| priority | int 或 None | 按工作区与项目策略解析 |
| auto_fault_tolerance | bool 或 None | None 使用账号配置 |
| fault_tolerance_max_retry | int 或 None | None 使用账号配置 |
| fault_tolerance_retry_interval_sec | int 或 None | 故障恢复间隔，单位秒 |
| datasets | list[str 或 DatasetMount]，空列表 | `"name:version"` 或 `DatasetMount(name, version)` |
| envs | dict[str, str]，空字典 | 注入每个实例；摘要只展示数量 |
| description | str 或 None | 平台描述 |
| keep_after_success_hours / keep_after_failure_hours | float 或 None | 成功 / 失败后保留容器小时数 |
| public_path_readonly | bool 或 None | 项目 public 路径只读挂载 |
| enable_notification | bool 或 None | None 使用账号配置 |
| max_time_hours | float 或 None | 最长运行小时数 |
| nodes | int，1 | 实例数 |
| exclude_nodes / specified_nodes | list[str]，空列表 | 排除 / 指定节点 |
| shm_gib | int 或 None | 每实例共享内存，None 使用账号配置 |

`JobPlan` 暴露 name、workspace、project、group、image、quota、priority、nodes、datasets、envs_count、description、max_time（提交的毫秒字符串）和 shm（GiB）。`summary` 使用这些解析结果，不包含 command 或环境变量值。

## Jobs 方法与 CLI 对应

| CLI | `client.jobs` | 返回值 / 行为 |
|---|---|---|
| job list | `list(workspace=..., status=None, keyword=None, limit=20, cursor=None)` | Page[Job]；status 对规范状态和平台原始状态均做大小写不敏感比较 |
| job list 分页 | `iter(workspace=..., status=None, keyword=None, max_items=None)` | 任务迭代器，按引用去重 |
| job status | `get(ref, workspace=...)` / `status(names_or_refs, workspace=None)` | 单个 Job / 按输入顺序返回 tuple[Job, ...]；名称需要 workspace |
| job create --dry-run | `plan(spec)` | JobPlan |
| job create | `create(spec, operation_id=None)` | JobHandle |
| job stop / delete | `stop(ref)` / `delete(ref)` | 单次写入；名称选择时传 workspace |
| job wait | `wait(ref, timeout=3600, poll_interval=10, raise_on_failure=False)` | 使用共享终态词表等待 |
| job command | `command(ref)` | 原始命令字符串 |
| job instances | `instances(ref)` / `instance_names(ref)` | tuple[JobInstance, ...] / 名称元组；结构化行有 name/status/node/role/rank/raw |
| job events | `events(ref, type=None, reason=None, instance=None, workload_level=False, limit=100)` | EventResult，items 为事件字典 |
| job events --follow | `follow_events(ref, interval=5, **filters)` | 生成器，每次返回新增事件批次，到终态停止 |
| job logs | `logs(ref, instances="all", window=None, start=None, end=None, tail=None, head=None, limit=100, max_chars=None)` | LogResult，包含 text/items/instances/start/end/truncated/total |
| job logs --follow | `follow_logs(ref, interval=2, **filters)` | 平台日志批次生成器，终态后再读取一轮 |
| job metrics | `metrics(ref, metric="core", window="1h", start=None, end=None, interval=None, group=None)` | tuple[MetricGroup, ...]，含结构化样本；默认采样间隔 1m |
| job quota | `quotas(workspace=..., group=None, include_empty=False, limit=20, cursor=None)` | Page[QuotaOption]；group 字符串是大小写不敏感的组名子串，Ref 精确匹配；空组行的 quota 为 None |

需要解析名称的单任务方法均支持 `workspace=...`；传 JobRef 时可省略。`status` 中任何一项失败会抛出相应 SDK 错误，不打印 CLI 的逐项错误行。

## Notebooks 方法与 CLI 对应（Phase C）

`client.notebooks` 覆盖 notebook 的平台查询、创建、启停、删除、事件、运行周期、指标、配额和镜像保存。名称操作必须提供 workspace；拿到 `NotebookRef` 后可省略 workspace。`NotebookRef` 与其他引用一样支持 `to_dict()` / `from_dict()`，账号、来源和工作区校验沿用 Client 的公共合同。

| CLI | `client.notebooks` | 返回值 / 行为 |
|---|---|---|
| notebook list | `list(workspace, status=None, keyword=None, limit=20, cursor=None)` | Page[Notebook]；仅查询当前账号的 notebook；status 大小写不敏感并传入平台过滤器 |
| notebook list 分页 | `iter(workspace, status=None, keyword=None, max_items=None)` | Notebook 迭代器，按 Ref 去重 |
| notebook status | `get(name_or_ref, workspace=None)` / `status(names, workspace=None)` | Notebook / 按输入顺序返回 tuple[Notebook, ...]；名称完整匹配，同名抛 AmbiguousResourceError |
| notebook create 的只读解析 | `plan(spec)` | NotebookPlan：已解析的作用域、项目、计算组、镜像、quota、优先级、共享内存、自动停止和数据集；`create_kwargs` 是传给 create_notebook 的参数，不包含 session；不创建或预留资源 |
| notebook create | `create(spec, operation_id=None)` | NotebookHandle(name, ref, operation_id)；复用 CLI 的参数构造，单次发送，不隐式等待 |
| notebook create/start --wait | `wait(ref, timeout=600, poll_interval=5, target="RUNNING", raise_on_failure=False)` | Notebook；target 也可为 STOPPED；达到目标或 FAILED / ERROR / STOPPED / DELETED 终态后返回 |
| notebook start / stop / delete | `start(ref)` / `stop(ref)` / `delete(ref)` | 每个操作单次写入；与 CLI 相同，不增加详情或终态预检 |
| notebook events | `events(ref, keyword=None, limit=100)` | EventResult；使用 CLI 共享关键词过滤，返回过滤后最近 limit 条，附 truncated |
| notebook events --follow | `follow_events(ref, interval=5, **filters)` | 持续生成新增 EventResult；与 notebook CLI 一样，终态后继续轮询，由调用方中断或关闭生成器 |
| notebook lifecycle | `lifecycle(ref, limit=None)` | tuple[dict, ...]；调用 ListRunIndex，按运行周期 index 排序；指定 limit 时取最近周期；None 返回全部，时间字符串保持平台格式 |
| notebook metrics | `metrics(ref, metric="core", window="1h", start=None, end=None, interval=None, group=None)` | 返回 tuple[MetricGroup, ...]；共享 metrics 解析，默认间隔 1m，group 可覆盖详情中的计算组 |
| notebook metrics --now | `realtime_metrics(ref, *, workspace=None)` | tuple[NotebookResourceSnapshot, ...]：resource、used、total、available、usage_rate、unit；调用 GetRealtimeNotebookMetric |
| notebook quota | `quotas(workspace, group=None, include_empty=False, limit=20, cursor=None)` | Page[QuotaOption]；组名子串过滤或 ComputeGroupRef 精确选择，仅保留 notebook 计算组；`to_dict()` 含 CLI quota 的优先级限制和 points_per_hour；空组的 quota 为 None |
| notebook save-image --dry-run | `estimate_image_size(ref)` | NotebookImageSizeEstimate：size_bytes、notebook_running；仅估算，不提交保存 |
| notebook save-image | `save_image(ref, name, version=None, description=None, visibility=None, flatten=False)` | ImageSaveHandle；version 默认 v1，description 默认空；包含镜像 Ref（可能为 None）、notebook Ref、估算大小、flatten 和 warning |
| notebook cancel-save-image | `cancel_save_image(ref)` | bool；False 表示没有进行中的保存，单次发送 |
| notebook save-image --wait | `wait_image_ready(image_handle_or_ref, timeout=600, poll_interval=5)` | 复用 CLI 的镜像等待核心，返回平台 CustomImageInfo；接受 READY / SUCCESS / SUCCEEDED 等已有成功词表 |

`Notebook` 暴露 `name/ref/status/raw_status/workspace/project/image/compute_group/created_by`，以及 CLI status JSON 的 resource、node、priority、priority_level、shared_memory_gib、uptime_seconds、auto_stop_in_seconds、datasets、created_at、updated_at。`quota` 是 resource 的字典副本；`to_dict()` 不包含 Ref。状态保留 notebook 自身词表，例如 STOPPED 不映射成 Job 的 CANCELLED。批量 status 中任何一项失败会抛出错误。

`NotebookCreateSpec` 的平台选项如下；项目和计算组完整名称解析，镜像字符串沿用 notebook CLI 的目录顺序和名称 / URL 匹配，类型化镜像引用与 ImageSelector 可明确指定身份或来源。

| 字段 | 类型 / 默认值 | 平台语义 |
|---|---|---|
| name | str，必填 | notebook 名称；空字符串沿用 CLI 自动命名 |
| workspace / project / group | str 或对应 Ref，必填 | 工作区、项目、计算组 |
| quota | Quota / QuotaRef / `"gpu,cpu,mem"`，必填 | 精确资源规格，内存为 GiB |
| image | str / ImageRef / ImageSelector，必填 | notebook 镜像目录选择 |
| shm_gib | int / None | None 使用配置 shm_size，未配置时 32 GiB；至少 1 GiB |
| auto_stop | bool，False | 请求平台空闲自动停止 |
| auto_stop_after | int / None | 运行分钟数，至少 2；隐含 auto_stop=True，并拆成 stop_hour / stop_minute |
| datasets | list[str 或 DatasetMount]，空列表 | `"name:version"` 或 DatasetMount；通过共享数据集核心校验与解析挂载路径 |
| enable_notification | bool / None | 状态变化通知；None 保留平台默认 |
| public_path_readonly / project_path_readonly | bool / None | public / 项目成员路径只读开关；None 保留平台默认 |
| priority | int / None | 与 CLI 相同的工作区策略、项目优先级上限及 quota 限制 |
| node | str / None | 传入 CLI --node 对应的平台节点字段 |

```python
from inspire import NotebookCreateSpec, Quota

spec = NotebookCreateSpec(
    name="sdk-notebook",
    workspace="工作区名称",
    project="项目名称",
    group="完整计算组名称",
    quota=Quota(gpu=0, cpu=8, memory_gib=32),
    image="镜像名称",
    shm_gib=16,
    auto_stop_after=60,
)
print(client.notebooks.plan(spec).summary)
handle = client.notebooks.create(spec, operation_id="my-notebook-operation")
notebook = client.notebooks.wait(handle.ref, raise_on_failure=True)
print(notebook.name, notebook.status)
# 生命周期变更由应用显式调用；create 和 wait 不执行 post-start 命令。
client.notebooks.stop(handle.ref)
client.notebooks.wait(handle.ref, target="STOPPED", raise_on_failure=True)
```

创建保留 CLI 的同名预检；预检读取失败不会阻止提交。若创建响应未带 ID，则复用 CLI 的创建后名称查询；仍无法确认时抛 SubmissionUncertainError，保留 operation_id，不重发创建。wait 的目标已达到时直接返回；遇到其他终态且 `raise_on_failure=True` 时抛 SDK 的 `NotebookFailedError(InspireError)`，其 `.notebook` 为完整 `Notebook` 快照，可读取状态、引用与详情字段；CLI/browser_api 保留服务层异常类型。等待超时抛 WaitTimeoutError。

保存镜像先尽力估算大小：估算失败不阻止保存，明确返回 notebook 未运行时按 CLI 报错。保存请求不接受 visibility；SDK 在确认镜像 ID 后另用一个 single_send 更新可见性。镜像 ID 尚不可查时返回 `ref=None`；可见性更新失败按 CLI 行为保留已保存结果，并通过 warning 返回原错误，应用可稍后查询镜像并处理。`wait_image_ready` 需要已确认的镜像引用；镜像构建失败保留平台错误文本并抛 ValidationError。它不会重复保存。

Notebook CLI-only：`ssh`、`shell`、`exec`、`scp`、`ssh-config`、`ssh-proxy`、`connection *`、`install-deps`、`proxy-url`、`batch`、`--post-start/--post-start-script`，以及 `metrics --plot/--open/--sparkline`。SDK 不承担本地连接配置、命令执行、文件同步、依赖安装或绘图；批处理由调用方循环或编排。

## HPC / Ray 方法与 CLI 对应（Phase D）

`client.hpc` 与 `client.ray` 覆盖两个工作负载的平台操作。名称选择需要 workspace，完整名称大小写不敏感，同名资源抛 `AmbiguousResourceError`。获得 `HPCJobRef` / `RayJobRef` 后可省略 workspace，引用支持 `to_dict()` / `from_dict()`，沿用账号、来源、类型和工作区校验。

| CLI | SDK 方法 | 返回值 / 行为 |
|---|---|---|
| hpc / ray list | `list(workspace, status=None, keyword=None, limit=20, cursor=None)` | `Page[HPCJob]` / `Page[RayJob]`；当前账号任务，状态与关键词本地过滤，完整扫描最多 100 页 |
| hpc / ray list 分页 | `iter(workspace, status=None, keyword=None, max_items=None)` | 按 Ref 去重的迭代器 |
| hpc / ray status | `get(ref, workspace=None)` / `status(names, workspace=None)` | 单个任务 / 按输入顺序返回任务元组；任何一项失败抛出对应错误 |
| hpc / ray create --dry-run | `plan(spec)` | `HPCJobPlan` / `RayJobPlan`；create_kwargs 是实际创建载荷，payload / to_dict() 是 CLI dry-run 对应的业务视图，summary 不含命令；不预留资源 |
| hpc / ray create | `create(spec, operation_id=None)` | `HPCJobHandle` / `RayJobHandle`，包含 name、ref、operation_id；重新规划后单次发送 |
| hpc / ray stop、delete | `stop(ref)` / `delete(ref)` | 每个操作单次发送，不增加终态预检 |
| ray start | `client.ray.start(ref)` | 单次启动，后续状态由调用方查询 |
| 等待任务完成 | `wait(ref, timeout=3600, poll_interval=10, raise_on_failure=False)` | 返回终态任务；超时抛 WaitTimeoutError，不停止远端任务 |
| hpc events | `client.hpc.events(ref, reason=None, instance=None, workload_level=False, limit=100)` | EventResult；默认合并控制器和实例事件，折叠重复次数，附实例标签；HPC 没有 type 过滤 |
| ray events | `client.ray.events(ref, type=None, reason=None, instance=None, workload_level=False, limit=100)` | EventResult；共享 CLI 最近事件窗口；type 精确匹配，reason 子串匹配 |
| hpc / ray events --follow | `follow_events(ref, interval=5, **filters)` | 轮询并生成新增 EventResult；与 CLI 相同，终态后仍继续，由调用方关闭生成器 |
| hpc / ray instances | `instances(ref)` | `tuple[HPCInstanceView, ...]` / `tuple[RayInstanceView, ...]`；HPC 含 handle/pod/role/label，Ray 含 handle/role/kind/label；标签与 CLI 的 Role / Type / Rank 一致 |
| hpc / ray logs | `logs(ref, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | LogResult，默认 100 条；包含 text/items/instances/start/end/truncated/total |
| hpc / ray metrics | `metrics(ref, metric="core", window="1h", start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]`，共享指标解析，默认间隔 1m；Ray 从 head/worker 详情递归解析计算组，group 可覆盖 |
| hpc / ray quota | `quotas(workspace, group=None, include_empty=False, limit=20, cursor=None)` | Page[QuotaOption]；对应工作负载计算组，组名子串过滤或 ComputeGroupRef 精确选择 |
| ray scaling | `client.ray.scaling(ref, group=None, limit=None)` | 按时间排序的公开历史字典元组；time/event/group/replicas_before/replicas_after；limit 取最近 N 项，None 返回平台本次完整结果 |

两种计划均提供解析后的类型字段：`project: Resource[ProjectRef]`、`group: Resource[ComputeGroupRef]`、`quota: Quota`、`image: str`、`priority: int`，以及 `name`、`workspace`。HPC 的 `image` 为解析后的镜像仓库 URL，Ray 的 `image` 为解析后的 mirror ID。`summary` 包含项目、计算组、配额、镜像和优先级。

- `HPCJobPlan` 另含 `instance_count: int`、`number_of_tasks: int`、`cpus_per_task: int`、`memory_per_cpu: int` 和 `datasets: tuple[DatasetMount, ...]`，表示解析后的 Slurm 布局与数据集挂载。
- `RayJobPlan` 另含 `workers: tuple[dict[str, Any], ...]`（解析后的 worker 配置）及 `shm_gib: int | None`。`create_kwargs`、`payload` 和 `to_dict()` 保持原有语义。


所有单任务方法均支持 `workspace=...`。events / logs 的 instance 接受单个标签或标签序列；workload_level 与 instance 互斥。`HPCJob` / `RayJob` 提供 name、ref、status、raw_status、project、created_at、finished_at 和 raw；`to_dict()` 返回 CLI status 的业务视图。

状态词表分别位于 `services/hpc_status.py` / `services/ray_status.py`，CLI status 和列表过滤也使用它们。STOPPED 保留平台原义；SUCCEEDED、FAILED、STOPPED、CANCELLED、DELETED、ERROR 是等待终态，UNKNOWN 和未识别状态继续等待。`raise_on_failure=True` 在非 SUCCEEDED 终态抛 `HPCJobFailedError` / `RayJobFailedError`，`.job` 携带最终快照。

创建载荷分别共享 `services/hpc_submission.py` 和 `services/ray_submission.py`。以下字段保留 CLI 默认值与验证：

| 创建规格 | 字段 | 类型 / 默认值 |
|---|---|---|
| 两者 | name | 必填 str |
| 两者 | workspace / project / group | 必填名称或对应 Ref；创建时使用完整组名 |
| 两者 | quota | 必填 Quota / QuotaRef / `"gpu,cpu,mem"`，内存为 GiB |
| 两者 | image | 必填名称 / URL / ImageRef / ImageSelector；HPC 解析为镜像 URL，Ray 解析为平台镜像 ID |
| 两者 | image_type | SOURCE_PUBLIC / SOURCE_PRIVATE / SOURCE_OFFICIAL；HPC 默认 SOURCE_PRIVATE，Ray 默认 SOURCE_PUBLIC |
| 两者 | priority | int / None，按工作区与项目策略解析 |
| 两者 | public_path_readonly | bool / None；None 不发送，False 显式发送可写 |
| HPCJobCreateSpec | entrypoint | 必填 str，Slurm 脚本正文；与 CLI 一样拒绝完整 shebang / SBATCH 脚本 |
| HPCJobCreateSpec | instance_count / number_of_tasks | int，均默认 1 |
| HPCJobCreateSpec | cpus_per_task / memory_per_cpu | int / None；按节点规格和每节点任务数推导，内存为每 CPU GiB；共享 Slurm 布局验证 |
| HPCJobCreateSpec | enable_hyper_threading | bool，False |
| HPCJobCreateSpec | max_time_hours / keep_after_finish_hours | 正小时数 / None；前者转换为 Slurm 时间字段，后者转换为容器保留秒数 |
| HPCJobCreateSpec | datasets | list[str 或 DatasetMount]，默认空；共享数据集解析与挂载验证 |
| HPCJobCreateSpec | description / enable_notification | str / None；bool，False |
| RayJobCreateSpec | command | 必填 str，Ray driver 启动命令 |
| RayJobCreateSpec | description / shm_gib | str，空字符串；正整数 GiB / None |
| RayJobCreateSpec | workers | list[str]，默认空，但规划 / 创建要求至少一组；使用 CLI 的 --worker 语法 |

Ray worker 格式为 `name=decode;image=镜像名;group=完整组名;quota=0,8,32;min=1;max=4;image-type=SOURCE_PRIVATE;shm-size=8`。前六项必填，image-type 默认 SOURCE_PUBLIC，shm-size 可省略；min/max 至少为 1 且 max 不小于 min，错误文本与 CLI 解析核心一致。

```python
from inspire import HPCJobCreateSpec, RayJobCreateSpec, Quota

spec = HPCJobCreateSpec(
    name="sdk-hpc", entrypoint="srun python preprocess.py",
    workspace="工作区名称", project="项目名称", group="完整计算组名称",
    quota=Quota(0, 8, 32), image="镜像名称", max_time_hours=2,
)
print(client.hpc.plan(spec).summary)
handle = client.hpc.create(spec, operation_id="pipeline-stage-1")
finished = client.hpc.wait(handle.ref, raise_on_failure=True)
print(client.hpc.logs(handle.ref, tail=50).text)

ray_spec = RayJobCreateSpec(
    name="sdk-ray", command="python driver.py",
    workspace="工作区名称", project="项目名称", group="完整计算组名称",
    quota="0,8,32", image="镜像名称",
    workers=["name=decode;image=镜像名称;group=完整计算组名称;quota=0,8,32;min=1;max=4"],
)
print(client.ray.plan(ray_spec).summary)
```

创建响应丢失、平台报错或未返回 ID 时，沿用单次发送合同抛 SubmissionUncertainError；不重新提交或隐式查询确认。其他写操作发送后失败抛 MutationUncertainError，异常链保留底层平台错误。operation_id 是诊断关联值，不是平台幂等键。

HPC 默认日志窗口取实例创建 / 结束时间，Ray 取任务详情中的时间；显式 window 使用当前时间，start/end 接受成对 datetime。两个工作负载均将窗口限制为最近 30 天。tail/head 互斥，日志按共享排序与标签核心选择。HPC 平台接口截断尾部且忽略页码，因此 tail 或默认查询在 total 大于返回记录数时会扩大一次请求；head 不扩大。Ray 普通查询只在本次返回样本中选择 tail/head。SDK 不承诺全局最后 N 条或无损续读，不附加 CLI 的默认文本字符预算；记录未覆盖 total 时 truncated=True。

HPC / Ray 的 shell、YAML batch 和 `metrics --plot/--open/--sparkline` 保留为 CLI 功能。真实平台创建及清理闭环仍属于 Phase F；Phase D 使用隔离账号配置与模拟平台请求验收。

## 日志、事件与指标

```python
logs = client.jobs.logs(handle.ref, window="30m", instances="all", tail=50)
print(logs.text)
events = client.jobs.events(handle.ref, type="Warning", reason="sched", limit=20)
metrics = client.jobs.metrics(handle.ref, metric="gpu,cpu", window="2h")
for update in client.jobs.follow_logs(handle.ref, interval=2):
    print(update.text)
```

日志的 window 与 CLI 共用解析器，接受 `30m`、`2h`、`1d` 等正整数窗口。显式 window 以当前时间为终点；默认 None 使用任务创建 / 完成时间并前后各留 10 分钟，缺少创建时间时回看 24 小时。也可传 datetime start/end 指定绝对窗口。

`instances="all"` 发现实例；显式列表直接用于平台调用，不先校验它是否在发现结果中。tail/head 互斥，与 CLI 共用日志拉取与排序选择逻辑：请求条数为 `max(limit, tail, head)`，按时间排序后取头部或尾部；省略 tail/head 时取 limit 条尾部记录。平台返回有限样本，这不额外保证全局最后 N 条或无损续读。`max_chars=None` 默认不做字符截断；指定时裁剪格式化文本并设置 truncated。items 保留按条数选择的结构化记录。

事件默认合并任务级与实例级事件；type 精确匹配 Normal/Warning（大小写不敏感），reason 做子串匹配。instance 接受单个标签或标签列表，支持 `rank=0`、`0` 和角色名称；workload_level 与 instance 互斥。limit 选择过滤后的最近事件。follow 按事件内容或日志标识去重，是轮询观察接口，不是平台持久订阅或无损游标。Jobs 的事件 follow 到终态停止；Notebooks 和 CLI 的事件 follow 保留持续轮询行为。

指标与 CLI 共用参数解析和平台样本提取：metric 支持 core/all、逗号分隔别名和原始指标名；start/end 支持 CLI 时间字符串，SDK 也接受 datetime。start 优先于 window。group 可覆盖从详情推断的计算组。

CLI-only：job batch 的 YAML 驱动、job shell，以及日志 `--path/--remote-log-path/--notebook/--source` 的 SSH 文件路径。SDK follow 仅支持平台来源日志。指标 `--plot/--open/--sparkline` 是 CLI 输出功能，SDK 返回样本，由应用绘图。

## 错误与时间预算

SDK 错误（包括 `NotebookFailedError`）均从 `InspireError` 派生。配置、认证、冷却、参数错误、未找到、歧义、不完整枚举、传输失败、写入不确定、等待超时分别可捕获。`AmbiguousResourceError.candidates` 返回可选择引用；`AuthenticationCooldownError.retry_at` 是允许再次评估认证的 Unix 时间，不是鼓励盲目重试错误密码。冷却沿用 CLI 的账号级 guard。

`timeout` 控制单次请求预算，`operation_timeout` 控制普通操作的协作式总预算，`wait(timeout=...)` 设置整个等待预算；嵌套调用使用更早截止时间。请求、退避和刷新锁等待共享剩余预算。同步网络库的底层调用和已有浏览器登录流程不能被强制抢占，显式允许浏览器时登录可能超出总预算；这些参数不是硬实时取消保证。需要硬隔离的编排器应使用独立进程，并在超时后核查任何可能已发送的写请求。

传输策略由调用方声明：普通 `operation` 进入 `Transport.scope(timeout=...)`，按 READ 处理。READ 对 requests 异常、HTTP 429/5xx、共享 `_is_transient_v2_error_code` 判定的 v2 暂时错误最多尝试三次，退避与请求共用截止时间。HTTP 401/3xx 仅在 allow_browser=True 时允许一次会话刷新，否则抛 AuthenticationError；requests 层失败也只有显式允许浏览器才可换通道。

真正写入的 browser_api 调用必须包在 `transport.single_send(operation_id, create=True)` 或 `transport.single_send()` 中。一个 block 最多允许一次 request，第二次调用抛 RuntimeError；create 参数显式区分创建与其他变更。发送后不刷新、不重试、不换通道，错误分别映射为 SubmissionUncertainError / MutationUncertainError。Transport 不按 URL、Action 或 HTTP 动词猜测幂等性，也不校验信封形状或自动解包；解析后的 JSON 原样交给 browser_api。READ 的一般 HTTP 4xx 返回包含状态码和最多约 500 字符正文的 ValidationError，业务错误保留原消息。

## 当前验证边界

回归覆盖 CLI/SDK payload 等价、跨账号与线程隔离、单次创建、envelope 重试、缺失确认、分页、同名消歧、镜像来源、日志窗口、状态等待与显式清理。真实平台只做工作区、任务和日志读取，未提交收费任务或执行 stop/delete。GPU 容器、CPU 节点及网络共享盘内调用尚未实测。SDK 为实验性新增入口，发布前仍需要专门的真实创建闭环验收。

维护者参见 [迁移审计与测试清单](sdk-migration-audit.md)。
