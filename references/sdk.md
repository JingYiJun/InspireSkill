# Python SDK（实验性）

## 接入

同一个 `inspire-skill` 包提供同步入口 `from inspire import InspireClient`，覆盖全部平台侧 CLI 命令组。SDK 直接复用 browser_api、数据广场客户端及共享 services；安装依赖和 CLI 默认行为不变。源码安装可在 `cli/` 运行 `uv pip install -e .`，应用项目可用 `uv add /path/to/InspireSkill/cli`。

```python
from inspire import InspireClient

with InspireClient(account="my-account") as client:
    workspace = client.workspaces.get("工作区名称")
    for job in client.jobs.iter(workspace.ref, max_items=40):
        print(job.name, job.status)
```

SDK 面向能访问平台的本机或控制节点。CPU 节点需满足网络、账号缓存和文件锁条件；GPU 训练容器及任意网络共享盘锁语义尚未验证。

## 账号与会话

本地账号管理可直接使用 `from inspire import Accounts`，无需先构造 Client；`InspireClient.accounts` 指向同一个类。所有账号状态仍保存在 `~/.inspire/accounts/<alias>/`，没有仓库级配置层。

| API | 行为 |
|---|---|
| `Accounts.list()` | 返回排序后的账号别名元组 |
| `Accounts.current()` | 读取磁盘默认账号，忽略临时账号作用域；未设置时为 `None` |
| `Accounts.exists(name)` | 检查账号是否存在 |
| `Accounts.config_path(name)` | 返回账号配置的 `Path` |
| `Accounts.add(name, *, username, password, base_url="https://qz.sii.edu.cn", proxy=None, use=False, overwrite=False)` | 与 CLI 共用配置渲染；首个账号自动成为默认账号，其余仅在 `use=True` 时切换 |
| `Accounts.use(name)` | 切换磁盘默认账号 |
| `Accounts.rename(old, new)` | 重命名账号并迁移 Notebook 目标缓存 |
| `Accounts.remove(name)` | **立即删除账号及其缓存，不做确认** |

账号创建不打印、不提示、不联网、不启动浏览器；环境归一化仅检查本地 Chromium 文件是否存在。重复名称默认抛 `ValidationError`；`overwrite=True` 会替换整个账号目录，包括缓存。账号层错误转换为 `ValidationError` 并保留原消息。

```python
from inspire import Accounts, InspireClient

Accounts.add("research", username="login-name", password="password", use=False)
with InspireClient(account="research") as client:
    identity = client.login()
    result = client.init()
    print(result.config_path, result.changed, result.warnings)

# 也可以直接提供凭据；构造只准备本地配置，不联网登录。
with InspireClient(username="login-name", password="password", account="research") as client:
    identity = client.login()

# 可读性别名，行为相同：
client = InspireClient.from_credentials("login-name", "password", account="research")
client.close()
```

完整构造签名为 `InspireClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60)`。不传凭据时使用现有账号，省略 account 使用当前账号。username/password 必须成对传入；提供凭据且省略 account 时，以 username 作为本地别名（须符合账号名称规则，邮箱等应显式提供合法 account）。缺失账号会创建；已有账号仅更新显式提供且不同的 auth/api/proxy 字段，保留其他配置。`proxy=None` 保留原代理，空字符串清空四个代理字段。构造客户端始终不改变默认账号指针，包括创建首个账号时。

`client.login(force=False) -> AccountInfo` 立即建立并验证会话：缓存未过期时复用它并查询当前用户，缺失或过期时调用现有 Transport 续期流程；`force=True` 主动进入续期流程。平台会话约每 15 分钟失效，即使持续请求也不会延长。续期在账号刷新锁内先重读更新的磁盘缓存，再尝试 SSO Cookie 续期，最后执行受账号登录 guard 保护的 `login_without_browser`（requests CAS 凭据登录）。只有 `allow_browser=True` 才允许 Chromium 登录回退及浏览器请求通道。验证码要求保留原提示并抛 `AuthenticationError`，不会转入浏览器重试；冷却通过 `AuthenticationCooldownError.retry_at` 暴露。

`client.init(force=False) -> InitResult` 先登录，要求会话含真实可访问 workspace ID，然后按需原子写入账号配置，补齐平台地址、缺失的凭据及缓存登录身份。默认 `force=False` 仅合并凭据、平台地址及缓存登录身份，保留所有其他键值（包括未知节和旧表），仅在解析后的字典变化时写回；`force=True` 从账号模板和发现值重新构建，像 `inspire init --force` 一样丢弃旧表，并舍弃自定义节。返回 `config_path: Path`、按解析后字典比较的 `changed: bool` 和 `warnings: tuple[str, ...]`。它不写入仓库配置。**交互提示、Playwright 安装和 ssh-keygen 仍只由 CLI 提供**；SDK init 不执行这些步骤。CLI 的非交互 init 仍要求已有配置时显式指定 `--force`，其原有刷新语义保留。

导入 SDK 不加载 Click、Rich 或 Playwright。构造时固定账号、平台来源和配置，后续切换默认账号不影响已有 Client。一个 Client 只在创建它的进程和线程中使用；线程 worker 或 fork 子进程各自创建 Client。使用 `with` 或 `close()` 释放连接。磁盘会话、刷新锁和登录冷却与 CLI 共用。`account_info` 查询平台信息；`api_keys.plaintext(ref)` 显式返回密钥值，其他密钥视图只包含元数据。

## 资源引用与分页

集合接口为 `list(workspace, *, ...filters, limit=20, cursor=None)`、`iter(workspace, *, ...filters, max_items=None)` 和 `quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)`；无工作区的目录保留其首个选择参数或无参数。以工作区为主要操作对象的方法（包括 `resources.availability/policy/usage`、`account_info.permissions` 和 `servings.configs`）将 `workspace` 作为首参数，接受位置或关键字传入，其余参数仅接受关键字；`account_info.permissions(workspace=None)` 的工作区可省略。其他方法中的工作区筛选参数仅接受关键字。单资源操作第一参数统一叫 `ref`，其余参数只接受关键字；批量 `status(refs, *, workspace=None)` 接受名称或类型化引用的序列，按输入顺序返回元组，空序列返回空元组。

训练 Jobs 和 HPC 的 `status()` 在解析引用后通过平台批量 Action 查询，按工作区分组，每 20 个引用一次请求（重复 ID 在分块前去重）。Ray、Serving、Notebook 仍逐个引用读取。两种路径返回的资源对象与 `get()` 完全相同，包括 `raw` 和 `view`；输入顺序和重复引用均保留。名称选择仍需先进行名称解析。

`Page` 提供 `items`、`next_cursor`、`total`，通过相同过滤条件与 `cursor=page.next_cursor` 继续。Jobs、Notebook、HPC、Ray、Serving 的列表采用服务端分页，按需取页直到收集到 `limit` 项或目录结束；游标记录平台行偏移，并绑定账号、门面及查询条件，不是平台快照。迭代器沿游标继续并对身份去重，`max_items` 控制产出数。Notebook、HPC、Ray、Serving 列表未应用本地过滤时，`total` 使用平台报告的总数；应用本地状态或关键词过滤时为 `None`，平台未提供可靠总数时也为 `None`。其他目录（包括 TensorBoard）仍可能先有界枚举再做本地分页。

HPC、Ray、Serving 的请求页大小固定为 50、20、20，Notebook 为 100；不会按总数扩大请求。平台列表接口只服务 `page_num × page_size ≤ 5000` 的请求（更深的页返回 `InvalidParameter`），因此任何列表或迭代最多能到达第 5000 行；SDK 在发出这类请求之前就抛 `ResolutionIncompleteError`，提示用 `status`/`keyword` 收窄或用 `max_items` 截止。缺页、重复页或单次扫描超过 100 页时同样抛 `ResolutionIncompleteError`。Serving、Notebook、TensorBoard 名称解析先发送 `keyword` 再精确匹配；当前 HPC/Ray ListJobs 合同不支持关键词过滤，名称解析最多扫描 100 页，无法确认唯一性时抛 `ResolutionIncompleteError`，可使用已有类型化引用直接查询。

名称按完整名称消歧，多个候选抛 `AmbiguousResourceError`，候选引用在 `.candidates`。名称查询工作负载、计算组、镜像和模型时显式给 workspace；已有类型化 Ref 可省略。Ref 校验类型、账号、来源以及显式工作区；`.to_dict()` / `XRef.from_dict()` 可用于保存和恢复，不能跨账号套用。项目目录默认是全局范围；模型 list 和账号 permissions 支持 `workspace="all"`，resources 查询只接受单工作区。

```python
workspace = client.workspaces.get("工作区名称")
page = client.jobs.list(workspace.ref, limit=20)
if page.next_cursor:
    next_page = client.jobs.list(workspace.ref, cursor=page.next_cursor)
statuses = client.jobs.status([job.ref for job in page.items])
```

资源结果通常为 frozen dataclass，嵌套字典并非递归冻结；`.to_dict()` 返回 CLI JSON 业务字段。镜像 `ImageSelector(name, source)` 可指定 official/public/project/private，跨来源同名会报歧义。镜像 list 可返回成功来源的目录，名称 get/detail 要求完整候选集。数据集 get 接受 code 或 DatasetRef，validate 接受 `"name:version"` 或 DatasetMount；applications 的单数据集扫描有界，不保证窗口之外的申请历史。

### 缓存

每个 `InspireClient` 有独立的进程内目录缓存，默认 TTL 为 60 秒，通过 `catalog_ttl` 设置（单位：秒；`0` 禁用）。缓存覆盖工作区路由、项目目录、计算组、各来源的镜像目录、配额价格与优先级、工作区公平调度标记和当前用户；按账号、平台地址及工作区／来源／调度类型等范围隔离。任务列表、资源详情、日志、事件、指标和实时用量不缓存。名称消歧仍检查全部候选，不缓存失败或不完整的目录结果。

```python
with InspireClient(catalog_ttl=60) as client:
    # 重复的名称解析和 plan() 在 TTL 内复用目录。
    print(client.cache.stats())  # {"hits": ..., "misses": ..., "entries": ...}
    client.cache.clear()         # 清空目录；命中／未命中计数保留
```

镜像注册、删除、可见性修改和 Notebook 保存镜像会清理受影响的镜像目录；会话续期不清理目录。指定 `ImageSelector(source=...)` 只读取该来源，`ImageRef` 直接读取详情，registry URL 不枚举镜像目录。长时间运行且必须立即看到外部目录变更的进程，应设置 `catalog_ttl=0`，或在需要最新目录时先调用 `client.cache.clear()`。

## 各门面方法表

以下签名省略类型注解；`*` 后参数必须以关键字传入。CLI 栏表示对应平台能力，名称解析、迭代及等待可以组合一个 CLI 子命令的能力；不会启动 CLI 子进程。

### workspaces

| SDK 方法 | CLI 子命令 |
|---|---|
| `workspaces.get(ref)` | `config context / account context（名称选择）` |
| `workspaces.list(*, limit=20, cursor=None)` | `config context / account context` |

### projects

| SDK 方法 | CLI 子命令 |
|---|---|
| `projects.detail(ref, *, workspace=None)` | `project detail` |
| `projects.get(ref, *, workspace=None)` | `project list（名称选择）` |
| `projects.list(workspace=None, *, limit=20, cursor=None)` | `project list` |
| `projects.owners()` | `project owners` |

### compute_groups

| SDK 方法 | CLI 子命令 |
|---|---|
| `compute_groups.get(ref, *, workspace=None)` | `account context（计算组目录）（名称选择）` |
| `compute_groups.list(workspace, *, limit=20, cursor=None)` | `account context（计算组目录）` |

### images

| SDK 方法 | CLI 子命令 |
|---|---|
| `images.delete(ref, *, workspace=None)` | `image delete` |
| `images.detail(ref, *, workspace=None)` | `image detail` |
| `images.get(ref, *, workspace=None)` | `image list（名称选择）` |
| `images.list(workspace, *, source=None, keyword=None, limit=20, cursor=None)` | `image list` |
| `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` | `image register` |
| `images.set_visibility(ref, *, visibility, workspace=None)` | `image set-visibility` |
| `images.wait_ready(ref, *, timeout=600, poll_interval=5, workspace=None)` | `image register --wait` |

### datasets

| SDK 方法 | CLI 子命令 |
|---|---|
| `datasets.applications(name=None, *, to_approve=False, keyword=None, limit=20, cursor=None)` | `dataset applications` |
| `datasets.get(ref)` | `dataset show` |
| `datasets.list(keyword=None, *, tag=None, limit=20, cursor=None)` | `dataset list` |
| `datasets.tags()` | `dataset tags` |
| `datasets.validate(specs, *, workspace)` | `dataset validate` |

### models

| SDK 方法 | CLI 子命令 |
|---|---|
| `models.delete(ref, *, force=False, workspace=None, project=None)` | `model delete` |
| `models.deploy_config(ref, *, workspace=None, project=None, version=None)` | `model deploy-config` |
| `models.get(ref, *, workspace=None, project=None)` | `model list（名称选择）` |
| `models.list(workspace, *, project=None, keyword=None, limit=20, cursor=None)` | `model list` |
| `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)` | `model register` |
| `models.status(refs, *, workspace=None, project=None)` | `model status` |
| `models.versions(ref, *, workspace=None, project=None)` | `model versions` |

### resources

| SDK 方法 | CLI 子命令 |
|---|---|
| `resources.availability(workspace, *, group=None, include_cpu=False)` | `resources availability` |
| `resources.node_events(nodes, *, since=None, type=None, reason=None, limit=None, from_component=None)` | `resources node-events` |
| `resources.policy(workspace, *, workload=None)` | `resources policy` |
| `resources.usage(workspace, *, project=None, user=None, task=None, group=None, mine=False, details=False, limit=None)` | `resources usage` |

### account_info

| SDK 方法 | CLI 子命令 |
|---|---|
| `account_info.check()` | `account check` |
| `account_info.context(*, limit=None)` | `account context` |
| `account_info.current()` | `account current` |
| `account_info.permissions(workspace=None)` | `account permissions` |

### api_keys

| SDK 方法 | CLI 子命令 |
|---|---|
| `api_keys.create(name)` | `account api-key create` |
| `api_keys.delete(ref)` | `account api-key delete` |
| `api_keys.get(ref)` | `account api-key list（名称选择）` |
| `api_keys.list(*, limit=20, cursor=None)` | `account api-key list` |
| `api_keys.plaintext(ref)` | `account api-key export（只返回明文，不导出文件）` |

### jobs

| SDK 方法 | CLI 子命令 |
|---|---|
| `jobs.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None)` | `job shell（非交互执行）` |
| `jobs.command(ref, *, workspace=None)` | `job command` |
| `jobs.create(spec, *, operation_id=None)` | `job create` |
| `jobs.delete(ref, *, workspace=None)` | `job delete` |
| `jobs.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `job events` |
| `jobs.follow_events(ref, *, interval=5, **filters)` | `job events --follow` |
| `jobs.follow_logs(ref, *, interval=2, **filters)` | `job logs --follow` |
| `jobs.get(ref, *, workspace=None)` | `job status` |
| `jobs.instance_names(ref, *, workspace=None)` | `job instances` |
| `jobs.instances(ref, *, workspace=None)` | `job instances` |
| `jobs.iter(workspace, *, status=None, keyword=None, max_items=None)` | `job list --all` |
| `jobs.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `job list` |
| `jobs.logs(ref, *, workspace=None, instances='all', window=None, start=None, end=None, tail=None, head=None, limit=100, max_chars=None)` | `job logs` |
| `jobs.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval='1m', group=None)` | `job metrics` |
| `jobs.plan(spec)` | `job create --dry-run` |
| `jobs.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `job quota` |
| `jobs.status(refs, *, workspace=None)` | `job status` |
| `jobs.stop(ref, *, workspace=None)` | `job stop` |
| `jobs.wait(ref, *, workspace=None, timeout=3600, poll_interval=10, raise_on_failure=False)` | `job wait` |

与 HPC、Ray、Serving 一样，训练任务 `Job.raw` 保留平台载荷，`Job.view` 是稳定的公开投影，`Job.to_dict()` 返回 `view` 的浅拷贝。`jobs.get()` 的 `view` 与同一详情载荷的 `inspire job status --json` 业务字段一致；`list()` / `iter()` 和批量 `status()` 也提供 `raw` / `view`，字段取决于平台记录。计算组、资源、节点与优先级等公开字段可从 `view` 读取；镜像等未进入公开投影的详情字段可从 `raw` 读取。

`jobs.logs()` 的默认时间范围及显式 `window` 最长为 30 天：保留结束时间并向后移动开始时间；CLI `job logs` 复用同一截断逻辑。

### notebooks

| SDK 方法 | CLI 子命令 |
|---|---|
| `notebooks.exec(ref, *, command, workspace=None, cwd=None, env=None, timeout=120, transport="auto", on_output=None)` | `notebook exec` |
| `notebooks.cancel_save_image(ref, *, workspace=None)` | `notebook cancel-save-image` |
| `notebooks.create(spec, *, operation_id=None)` | `notebook create` |
| `notebooks.delete(ref, *, workspace=None)` | `notebook delete` |
| `notebooks.estimate_image_size(ref, *, workspace=None)` | `notebook save-image --dry-run` |
| `notebooks.events(ref, *, keyword=None, limit=100, workspace=None)` | `notebook events` |
| `notebooks.follow_events(ref, *, interval=5, **filters)` | `notebook events --follow` |
| `notebooks.get(ref, *, workspace=None)` | `notebook status` |
| `notebooks.iter(workspace, *, status=None, keyword=None, max_items=None)` | `notebook list --all` |
| `notebooks.lifecycle(ref, *, limit=None, workspace=None)` | `notebook lifecycle` |
| `notebooks.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `notebook list` |
| `notebooks.metrics(ref, *, metric='core', window='1h', start=None, end=None, interval=None, group=None, workspace=None)` | `notebook metrics` |
| `notebooks.plan(spec)` | `notebook create --dry-run` |
| `notebooks.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `notebook quota` |
| `notebooks.realtime_metrics(ref, *, workspace=None)` | `notebook metrics --now` |
| `notebooks.save_image(ref, *, name, version=None, description=None, visibility=None, flatten=False, workspace=None)` | `notebook save-image` |
| `notebooks.start(ref, *, workspace=None)` | `notebook start` |
| `notebooks.status(refs, *, workspace=None)` | `notebook status` |
| `notebooks.stop(ref, *, workspace=None)` | `notebook stop` |
| `notebooks.wait(ref, *, timeout=600, poll_interval=5, target='RUNNING', raise_on_failure=False, workspace=None)` | `notebook create --wait / status（轮询）` |
| `notebooks.wait_image_ready(ref, *, timeout=600, poll_interval=5)` | `notebook save-image --wait` |

### hpc

| SDK 方法 | CLI 子命令 |
|---|---|
| `hpc.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None)` | `hpc shell（非交互执行）` |
| `hpc.create(spec, *, operation_id=None)` | `hpc create` |
| `hpc.delete(ref, *, workspace=None)` | `hpc delete` |
| `hpc.events(ref, *, workspace=None, reason=None, instance=None, workload_level=False, limit=100)` | `hpc events` |
| `hpc.follow_events(ref, *, interval=5, **filters)` | `hpc events --follow` |
| `hpc.get(ref, *, workspace=None)` | `hpc status` |
| `hpc.instances(ref, *, workspace=None)` | `hpc instances` |
| `hpc.iter(workspace, *, status=None, keyword=None, max_items=None)` | `hpc list --all` |
| `hpc.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `hpc list` |
| `hpc.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `hpc logs` |
| `hpc.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `hpc metrics` |
| `hpc.plan(spec)` | `hpc create --dry-run` |
| `hpc.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `hpc quota` |
| `hpc.status(refs, *, workspace=None)` | `hpc status` |
| `hpc.stop(ref, *, workspace=None)` | `hpc stop` |
| `hpc.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `hpc status（SDK 轮询等待终态）` |

### ray

| SDK 方法 | CLI 子命令 |
|---|---|
| `ray.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None)` | `ray shell（非交互执行）` |
| `ray.create(spec, *, operation_id=None)` | `ray create` |
| `ray.delete(ref, *, workspace=None)` | `ray delete` |
| `ray.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `ray events` |
| `ray.follow_events(ref, *, interval=5, **filters)` | `ray events --follow` |
| `ray.get(ref, *, workspace=None)` | `ray status` |
| `ray.instances(ref, *, workspace=None)` | `ray instances` |
| `ray.iter(workspace, *, status=None, keyword=None, max_items=None)` | `ray list --all` |
| `ray.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `ray list` |
| `ray.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `ray logs` |
| `ray.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `ray metrics` |
| `ray.plan(spec)` | `ray create --dry-run` |
| `ray.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `ray quota` |
| `ray.scaling(ref, *, group=None, limit=None, workspace=None)` | `ray scaling` |
| `ray.start(ref, *, workspace=None)` | `ray start` |
| `ray.status(refs, *, workspace=None)` | `ray status` |
| `ray.stop(ref, *, workspace=None)` | `ray stop` |
| `ray.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `ray status（SDK 轮询等待终态）` |

### servings

| SDK 方法 | CLI 子命令 |
|---|---|
| `servings.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None)` | `serving shell（非交互执行）` |
| `servings.api(ref, *, affinity_key=None, workspace=None)` | `serving api` |
| `servings.api_metrics(ref, *, metric=None, window='1h', interval=None, workspace=None)` | `serving api-metrics` |
| `servings.configs(workspace)` | `serving configs` |
| `servings.create(spec, *, operation_id=None)` | `serving create` |
| `servings.delete(ref, *, workspace=None)` | `serving delete` |
| `servings.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `serving events` |
| `servings.follow_events(ref, *, interval=5, **filters)` | `serving events --follow` |
| `servings.get(ref, *, workspace=None)` | `serving status` |
| `servings.instances(ref, *, workspace=None)` | `serving instances` |
| `servings.iter(workspace, *, project=None, status=None, keyword=None, max_items=None)` | `serving list --all` |
| `servings.list(workspace, *, project=None, status=None, keyword=None, limit=20, cursor=None)` | `serving list` |
| `servings.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `serving logs` |
| `servings.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `serving metrics` |
| `servings.plan(spec)` | `serving create --dry-run` |
| `servings.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `serving quota` |
| `servings.rollback(ref, *, version, workspace=None)` | `serving rollback` |
| `servings.scale(ref, *, replicas, workspace=None)` | `serving scale` |
| `servings.scale_history(ref, *, workspace=None, limit=20, cursor=None)` | `serving scale-history` |
| `servings.start(ref, *, workspace=None)` | `serving start` |
| `servings.status(refs, *, workspace=None)` | `serving status` |
| `servings.stop(ref, *, workspace=None)` | `serving stop` |
| `servings.versions(ref, *, workspace=None)` | `serving versions` |
| `servings.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None, target='RUNNING')` | `serving start / create（状态等待）` |

### 远程执行（exec）

Notebook、Job、HPC、Ray 和 Serving 都提供同步 `exec`，返回 frozen dataclass `ExecResult`，可从 `inspire` 或 `inspire.sdk` 导入。遵循统一签名契约，`ref` 之后的参数（包括 `command`）只接受关键字。

```python
result = client.notebooks.exec(
    notebook.ref,
    command="python -u check.py",
    cwd="/inspire/ssd/project/example/public/work",
    env={"MODE": "check"},
    timeout=120,
    transport="auto",
    on_output=lambda chunk: print(chunk, end="", flush=True),
)
print(result.returncode, result.completed, result.transport)

result = client.jobs.exec(job.ref, command="hostname", instance="rank=0")
result = client.hpc.exec(hpc_job.ref, command="hostname")       # launcher
result = client.ray.exec(ray_job.ref, command="hostname")       # head
result = client.servings.exec(serving.ref, command="hostname")  # 首个运行中副本
```

Notebook 支持两种不依赖浏览器的传输：`jupyter` 通过 Jupyter terminal websocket 执行；`ssh` 只使用当前 Client 账号下已缓存、Notebook ID 与工作区 ID 均匹配且可达的 rtunnel 桥。`auto` 优先使用符合这些条件的 SSH 桥，否则选择 Jupyter。显式 `ssh` 找不到可用桥时抛 `ValidationError`，提示先运行 `inspire notebook connection refresh <name>`。创建或刷新 SSH 桥仍为 CLI-only，SDK 不会隐式启动 Chromium 建桥。

Job/HPC/Ray/Serving 使用原始 PTY websocket。Job 的 `instance` 接受实例名、`rank=N`、裸数字或角色；不指定时要求恰好一个运行中实例，多实例会抛出列出候选的 `ValidationError`。HPC 默认选择 `launcher`，Ray 默认选择 `head`；默认角色不存在或匹配多个运行中实例时需要显式指定。Serving 默认选第一个运行中副本。显式实例名或工作负载公开标签必须匹配一个运行中实例；角色匹配多个副本同样报歧义。

命令按以下顺序组合：Client 配置的 `remote_env`、调用者的 `env`、可选的 `cd "<cwd>" && `，最后是 `command`。调用者的同名变量覆盖配置值；`env` 值按字面量引用，空字符串保留为空，不从本机环境补值。SSH 沿用 `bash -l` 执行方式。

`ExecResult` 字段为 `returncode`、`output`、`stdout`、`stderr`、`completed`、`transport` 和 `instance`。`transport` 为 `ssh`、`jupyter` 或 `pty`；工作负载 PTY 的 `instance` 是选中的实例名。SSH 保留独立 stdout/stderr，`output = stdout + stderr`，此拼接不表示跨流时间顺序。PTY 的 stdout/stderr 已由远端终端合并，`stdout == output`、`stderr == ""`。结果会去除已识别的输入回显前缀，通过唯一完成 marker 提取退出码；普通非零退出码直接返回结果。

`on_output` 在读取时按顺序接收解码后的字符串块。PTY 回调收到原始终端流，可能包含提示符、输入回显、ANSI 控制符及完成 marker；最终 `output` 才是解析后的输出。命令默认没有交互 stdin，需使用命令内的管道或远端文件重定向；人工交互仍用 CLI shell。

执行等待超时或连接在完成 marker 出现前结束时，返回 `returncode=124`、`completed=False`，尽可能保留已捕获输出；它不证明远端进程已停止。命令自己返回 124 且 marker 完整时，`completed=True`。SDK 的总 operation 时间预算也会限制传输等待；名称解析和实例查询沿用现有 SDK 错误约定。websocket 握手 401 可在命令发送前续期一次并重试，不经过 `Transport.request` 或 `single_send`，已发送的命令不会因执行失败自动重放。

### tensorboards

| SDK 方法 | CLI 子命令 |
|---|---|
| `tensorboards.create(spec, *, operation_id=None)` | `tensorboard create` |
| `tensorboards.delete(ref, *, workspace=None)` | `tensorboard delete` |
| `tensorboards.get(ref, *, workspace=None)` | `tensorboard status` |
| `tensorboards.list(workspace, *, status=None, job=None, keyword=None, limit=20, cursor=None)` | `tensorboard list` |
| `tensorboards.scalars(ref, *, tag='', run=None, points=None, workspace=None)` | `tensorboard scalars` |
| `tensorboards.start(ref, *, workspace=None)` | `tensorboard start` |
| `tensorboards.status(refs, *, workspace=None)` | `tensorboard status` |
| `tensorboards.stop(ref, *, workspace=None)` | `tensorboard stop` |
| `tensorboards.tags(ref, *, workspace=None)` | `tensorboard tags` |
| `tensorboards.url(ref, *, workspace=None)` | `tensorboard status（应用 URL）` |
| `tensorboards.wait(ref, *, target='running', raise_on_failure=False, timeout=60, poll_interval=3, workspace=None)` | `tensorboard start / stop（状态等待）` |

## 创建规格字段

创建统一使用 `create(spec, *, operation_id=None)`；Jobs、Notebooks、HPC、Ray、Servings 支持 `plan(spec)`，只解析与校验，不发送创建请求。TensorBoard 使用 `TensorboardCreateSpec` 直接创建。以下字段与对应 CLI 的平台创建选项共享校验与载荷逻辑；构造规格本身不会提交。

### JobCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `command` | `str` | `必填` |
| `nodes` | `int` | `1` |
| `shm_gib` | `int \| None` | `None` |
| `priority` | `int \| None` | `None` |
| `max_time_hours` | `float \| None` | `None` |
| `description` | `str \| None` | `None` |
| `framework` | `str` | `'pytorch'` |
| `auto_fault_tolerance` | `bool \| None` | `None` |
| `fault_tolerance_max_retry` | `int \| None` | `None` |
| `fault_tolerance_retry_interval_sec` | `int \| None` | `None` |
| `datasets` | `list[str \| DatasetMount]` | `[]` |
| `envs` | `dict[str, str]` | `{}` |
| `keep_after_success_hours` | `float \| None` | `None` |
| `keep_after_failure_hours` | `float \| None` | `None` |
| `public_path_readonly` | `bool \| None` | `None` |
| `enable_notification` | `bool \| None` | `None` |
| `exclude_nodes` | `list[str]` | `[]` |
| `specified_nodes` | `list[str]` | `[]` |

### NotebookCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `shm_gib` | `int \| None` | `None` |
| `auto_stop` | `bool` | `False` |
| `auto_stop_after` | `int \| None` | `None` |
| `datasets` | `list[str \| DatasetMount]` | `[]` |
| `enable_notification` | `bool \| None` | `None` |
| `public_path_readonly` | `bool \| None` | `None` |
| `project_path_readonly` | `bool \| None` | `None` |
| `priority` | `int \| None` | `None` |
| `node` | `str \| None` | `None` |

### HPCJobCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `entrypoint` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `image_type` | `str` | `'SOURCE_PRIVATE'` |
| `instance_count` | `int` | `1` |
| `priority` | `int \| None` | `None` |
| `number_of_tasks` | `int` | `1` |
| `cpus_per_task` | `int \| None` | `None` |
| `memory_per_cpu` | `int \| None` | `None` |
| `enable_hyper_threading` | `bool` | `False` |
| `max_time_hours` | `float \| None` | `None` |
| `keep_after_finish_hours` | `float \| None` | `None` |
| `datasets` | `list[str \| DatasetMount]` | `[]` |
| `description` | `str \| None` | `None` |
| `enable_notification` | `bool` | `False` |
| `public_path_readonly` | `bool \| None` | `None` |

### RayJobCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `command` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `image_type` | `str` | `'SOURCE_PUBLIC'` |
| `description` | `str` | `''` |
| `priority` | `int \| None` | `None` |
| `shm_gib` | `int \| None` | `None` |
| `workers` | `list[str]` | `[]` |
| `public_path_readonly` | `bool \| None` | `None` |

### ServingCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `model` | `str \| ModelRef` | `必填` |
| `command` | `str` | `必填` |
| `port` | `int` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `quota` | `str \| Quota \| QuotaRef` | `必填` |
| `image` | `str \| ImageRef \| ImageSelector` | `必填` |
| `model_version` | `int \| None` | `None` |
| `replicas` | `int` | `1` |
| `nodes_per_replica` | `int` | `1` |
| `shm_gib` | `int \| None` | `None` |
| `priority` | `int \| None` | `None` |
| `custom_domain` | `str \| None` | `None` |
| `description` | `str` | `''` |
| `auto_scaling` | `bool \| None` | `None` |
| `public_path_readonly` | `bool \| None` | `None` |

### TensorboardCreateSpec

| 字段 | 类型 | 默认值 |
|---|---|---|
| `name` | `str` | `必填` |
| `workspace` | `str \| WorkspaceRef` | `必填` |
| `project` | `str \| ProjectRef` | `必填` |
| `group` | `str \| ComputeGroupRef` | `必填` |
| `summary_path` | `str \| None` | `None` |
| `job` | `str \| JobRef \| None` | `None` |
| `auto_stop_hours` | `float \| None` | `None` |

Quota 内存与 shm_gib 单位为 GiB，时限与保留时间字段以名称中的单位为准。Job 共享 `build_training_job_plan`，优先级按工作区／项目策略解析，部分 None 字段使用账号配置。Plan 的 summary 不含命令或环境变量值；完整 create_kwargs 用于审阅载荷，应由调用方妥善处理。

HPC entrypoint 是 Slurm 执行正文，与 CLI 一样拒绝完整 shebang / SBATCH 脚本；CPU 布局参数按配额推导并验证。HPC 镜像解析为 URL，Ray 镜像解析为平台 ID。Ray workers 使用 CLI 语法，例如 `name=decode;image=镜像名称;group=完整组名;quota=0,8,32;min=1;max=4`；创建至少需要一个 worker 组，image-type 和 shm-size 可选。

Serving model_version 缺省取目录最新版本，port 范围为 1–65535，副本与每副本节点数至少为 1；运行后 `scale(ref, replicas=0)` 可缩至零。TensorBoard summary_path 创建时必须非空，job 只记录关联、不推导路径；auto_stop_hours 缺省采用 CLI 的 24 小时，上限 72 小时。

```python
from inspire import JobCreateSpec, Quota

spec = JobCreateSpec(
    name="sdk-job", command="python train.py", workspace="工作区名称",
    project="项目名称", group="完整计算组名称", quota=Quota(1, 8, 32), image="镜像名称",
)
plan = client.jobs.plan(spec)
print(plan.summary)
handle = client.jobs.create(spec, operation_id="pipeline-stage-1")
finished = client.jobs.wait(handle.ref, raise_on_failure=True)
```

## 写操作与 single_send

写操作由应用显式调用。注册签名为 `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` 和 `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)`。镜像注册预留推送槽位并返回 registry 地址，version 默认 v1、visibility 默认 private；模型注册共享盘目录，不上传本地文件。

每次实际写请求显式进入 `single_send`，最多发送一次；发送后失败不刷新、不重试、不换通道。创建／注册无法确认结果时抛 `SubmissionUncertainError(operation_id)`，其他变更抛 `MutationUncertainError`。operation_id 默认生成，允许任意非空诊断字符串，**不是服务器幂等键**。遇到不确定结果，先显式查询确认，再决定后续动作。

写请求发出后，由 `request()` 与 `single_send` 共用分类逻辑区分明确答复和未知结果；所有分支都不会自动补发：

| 写入结果 | SDK 异常 | 调用方后续处理 |
|---|---|---|
| 明确拒绝：一般 HTTP 4xx（401/403/429 除外）、v2 业务错误（如 Conflict） | ValidationError，保留平台消息；HTTP 错误包含状态和最多约 500 字符正文 | 修正参数或资源状态 |
| 明确无权限：HTTP 403 | AuthenticationError | 检查权限 |
| 明确拒绝执行／限流：已解析信封中的 InternalError、Throttling 等 TransientAPIError，或 HTTP 429 | TransportError，retryable=True，保留消息 | 可由调用方安全重试；SDK 不自动重试 |
| 结果未知：发送后 HTTP 401/3xx、HTTP 5xx、网络异常、超时、JSON 解码失败或无效响应 | 创建为 SubmissionUncertainError，其他变更为 MutationUncertainError；retryable=False | 先查询确认，避免重复写入 |

分类转换以 `from error` 保留原因链；block 内已抛出的 ValidationError、AuthenticationError、TransportError 原样透传。

Job/HPC/Ray/Serving 创建响应缺少 ID 直接报不确定。Notebook 和 TensorBoard 复用 CLI 创建后的只读确认，确认失败不会重新创建。API key 创建成功响应没有 ID，因此返回 `ref=None`，之后可显式 `get(name)`。已发送写入的确认读取位于 single_send 外，不能触发重提。

Notebook 保存镜像先尽力估算大小，估算失败不阻止保存；镜像 ID 暂不可查时返回 `ref=None`。可见性是确认镜像 ID 后的独立单次写入，其失败通过 handle.warning 保留；`wait_image_ready` 需要已确认引用，不会重复保存；与 `images.wait_ready` 一样返回 browser_api 的 `CustomImageInfo`。Models.delete 默认检查所有版本引用和 pending 部署，force=True 跳过同一预检。

## 日志/事件/指标


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

HPC 默认日志窗口取实例时间，Ray 取任务详情时间，均限制为最近 30 天。HPC 对 tail 或默认查询在 total 超过返回记录数时扩大一次请求，head 不扩大；Ray 在返回样本中选择。Serving 默认读取 24 小时／100 条，实例发现和日志使用 CLI 的共享核心。均不承诺全局最后 N 条或无损续读。

Notebook 没有平台程序日志接口；`metrics` 返回 `tuple[MetricGroup, ...]`，实时资源快照由 `realtime_metrics` 单独返回。TensorBoard tags/scalars 要求 running；标量按 step 汇总首末值、min/max，points 缺省只返回摘要，指定非负数可获取尾部点集。Serving api 返回结构化调用信息，不发送推理请求；端点存在不证明服务已就绪，api_metrics 另提供 QPS／成功率／延迟序列摘要。

## 错误与时间预算

写入开始时，若从未收到成功的平台响应，或距上次成功响应已满 60 秒，SDK 先通过普通 READ 路径执行一次 `GetUserDetail` 探测，必要时先续期再发送写入；60 秒内已有成功响应则省略探测。探测共享时间预算，其刷新与重试不计入写请求的单次发送。探测失败时不发送写入；写入发出后即使收到 401 也绝不重放，创建抛 `SubmissionUncertainError`，其他变更抛 `MutationUncertainError`。


SDK 错误（包括 `NotebookFailedError`）均从 `InspireError` 派生。配置、认证、冷却、参数错误、未找到、歧义、不完整枚举、传输失败、写入不确定、等待超时分别可捕获。`AmbiguousResourceError.candidates` 返回可选择引用；`AuthenticationCooldownError.retry_at` 是允许再次评估认证的 Unix 时间，不是鼓励盲目重试错误密码。冷却沿用 CLI 的账号级 guard。

`timeout` 控制单次请求预算，`operation_timeout` 控制普通操作的协作式总预算，`wait(timeout=...)` 设置整个等待预算；嵌套调用使用更早截止时间。请求、退避和刷新锁等待共享剩余预算。同步网络库的底层调用和已有浏览器登录流程不能被强制抢占，显式允许浏览器时登录可能超出总预算；这些参数不是硬实时取消保证。需要硬隔离的编排器应使用独立进程，并在超时后核查任何可能已发送的写请求。

传输策略由调用方声明：普通 `operation` 进入 `Transport.scope(timeout=...)`，按 READ 处理。READ 对 requests 异常、HTTP 429/5xx、共享 `_is_transient_v2_error_code` 判定的 v2 暂时错误最多尝试三次，退避与请求共用截止时间。HTTP 401/3xx 无论 allow_browser 设置均允许一次上述会话刷新，重试仍失效则抛 AuthenticationError；requests 层失败也只有显式允许浏览器才可换通道。

真正写入的 browser_api 调用必须包在 `transport.single_send(operation_id, create=True)` 或 `transport.single_send()` 中。一个 block 最多允许一次 request，第二次调用抛 RuntimeError；create 参数显式区分创建与其他变更。发送后不刷新、不重试、不换通道；明确拒绝映射为 ValidationError，明确限流／拒绝执行映射为可重试的 TransportError，HTTP 403 保留 AuthenticationError，仅未知结果映射为 SubmissionUncertainError / MutationUncertainError。Transport 不按 URL、Action 或 HTTP 动词猜测幂等性，也不校验信封形状或自动解包；解析后的 JSON 原样交给 browser_api。READ 的一般 HTTP 4xx 返回包含状态码和最多约 500 字符正文的 ValidationError，业务错误保留原消息。

工作负载 wait 的 raise_on_failure=True 抛对应 SDK 失败异常，携带最终资源快照：Job/HPC/Ray 使用 `.job`，Notebook 使用 `.notebook`，Serving 使用 `.serving`，TensorBoard 使用 `.tensorboard`。Job/HPC/Ray 等待终态；Notebook、Serving、TensorBoard 等待目标状态，具体默认目标见方法表。超时统一抛 WaitTimeoutError。

## CLI-only 范围


- 交互初始化提示、Playwright 安装、ssh-keygen，以及 `config *`、`update`、`uninstall`、`cache *`；非交互账号管理和初始化由 `Accounts`、`client.login()`、`client.init()` 提供。
- `api-key export` 的文件格式、权限和 stdout 渲染，以及 `api-key run` 的子进程和环境处理；平台密钥读写由 `client.api_keys` 提供。
- 所有工作负载的 JSON/TOML `batch`；SDK 应用自行循环或编排。
- Notebook 的 exec 由 SDK 提供；`ssh/shell/scp/ssh-config/ssh-proxy/connection */install-deps/proxy-url` 仍为 CLI-only，创建后的 `--post-start/--post-start-script` 及 `job/hpc/ray/serving shell` 也仅保留在 CLI。
- 日志 SSH 文件来源选项 `--path/--remote-log-path/--notebook/--source`，及终端专用格式、字符展示预算；SDK 使用平台日志来源并返回结构化记录。
- 指标 `--plot/--open/--sparkline` 和 TensorBoard 终端趋势渲染；SDK 返回样本或标量摘要。
- `serving api --format` 的 shell 格式输出；SDK 返回共享 access 核心的结构化 endpoint / invocation 信息。
