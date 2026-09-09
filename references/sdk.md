# Python SDK（实验性）

## 接入

同一个 `inspire-skill` 包提供两个受支持的实验性入口：`InspireClient` 用于同步脚本和同步 worker；`InspireAsyncClient` 用于 asyncio 应用、Agent runtime 和异步 Web 服务，将阻塞操作放到专用线程，避免占住事件循环。两者均可从 `inspire` 或 `inspire.sdk` 导入，使用相同的资源模型、引用、异常和平台能力；异步入口的线程池与取消边界见下文。

SDK 与 CLI 复用 browser_api、共享 services 和 `inspire.platform.web.transport.Transport`；安装依赖和 CLI 默认行为不变。源码安装可在 `cli/` 运行 `uv pip install -e .`，应用项目可用 `uv add /path/to/InspireSkill/cli`。

以下两个完整示例需要已配置的本地账号及可访问的工作区；`login()` 显式建立会话。保存为对应文件后，在 `cli/` 运行 `uv run python sync_example.py my-account "工作区名称"` 或 `uv run python async_example.py my-account "工作区名称"`。

同步快速开始（`sync_example.py`）：

```python
import sys
from inspire import InspireClient

def main(account: str, workspace: str) -> None:
    with InspireClient(account=account) as client:
        client.login()
        ws = client.workspaces.get(workspace)
        for job in client.jobs.iter(ws.ref, max_items=40):
            print(job.name, job.status)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

异步快速开始（`async_example.py`）：

```python
import asyncio
import sys
from inspire import InspireAsyncClient

async def main(account: str, workspace: str) -> None:
    async with InspireAsyncClient(account=account, concurrency=2) as client:
        await client.login()
        jobs, notebooks = await asyncio.gather(
            client.jobs.list(workspace, limit=5),
            client.notebooks.list(workspace, limit=5),
        )
        print("Jobs:", [job.name for job in jobs.items])
        print("Notebooks:", [notebook.name for notebook in notebooks.items])
        async for job in client.jobs.iter(workspace, max_items=10):
            print(job.name, job.status)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

SDK 面向能访问平台的本机或控制节点。运行环境需满足网络、账号缓存和文件锁条件；本次文档审阅仅核对代码和离线测试，未验证真实平台当前的会话寿命、分页上限、GPU 容器兼容性或网络共享盘锁语义。下文区分代码实施的限制与平台协议所提供的信息。

## 架构

依赖方向为 `sdk → services → platform`；CLI 也复用 services 与 platform，平台层和服务层不得导入 SDK。共享 dispatcher 位于 `inspire.platform.web.transport`，`inspire.sdk.transport` 保留兼容重导出及既有补丁入口。与传输共用的异常基础层位于 `inspire.platform.errors`，SDK 的 `exceptions` 重导出同一批类；携带 SDK 资源模型的工作负载失败异常仍由 SDK 定义。

两种前端共用一次 HTTP／浏览器发送路径，以独立响应策略保留异常类型和原始消息。SDK 与 CLI 的连接所有权、Referer、超时格式及请求体规则保持各自既有语义。CLI 的暂时 HTTP 状态仍是固定集合 `408/425/429/500/502/503/504`。重试循环显式保留两种策略的分支：CLI 刷新／浏览器回退不消耗暂时错误重试次数，SDK 按尝试次数与剩余截止时间计费；写请求发送后在进入这些分支前直接分类退出。

Session 层为传输提供公开的浏览器创建／获取／关闭、运行时错误报告、原地刷新与无凭据续期入口。刷新中的 `acquire_web_session` 与普通 `get_web_session` 有意区分：前者供已持有刷新锁的调用者使用，不触发前端 adoption；后者获取刷新锁并通知当前前端。CLI 刷新原地更新既有 WebSession，保留调用方持有的对象；SDK 刷新通过 adoption 接收新的 WebSession，并重新配置自己持有的 HTTP 连接。

browser_api 的控制台 v2 JSON 请求通过 `runtime.get_transport(session).request(...)`，数据广场请求通过同一 Transport 的 `plaza_request(...)`，实际发送共用 `_dispatch`。ContextVar 仍用于选择当前 Transport／CLI 会话接纳器；它不再承载一条 SDK 专用请求回调或在两套 dispatcher 之间分流。

数据广场有独立主机和 CAS 服务票据握手，内部仍有 `PlazaClient`、`requests.Session` 与 `datasets-session` Cookie，但它们由所属 Transport 持有、按账号与会话代次隔离并负责关闭。SDK 没有另一套独立生命周期的数据广场客户端，也不把它的 Cookie 混入控制台 HTTP 会话。Cookie 被拒时先重做广场握手，再升级到平台会话续期；广场使用自己的信封解包和重试分类，不走浏览器请求回退。远程 exec 的 websocket／SSH 是独立协议，执行数据流不经过 JSON dispatcher；TensorBoard 应用自身的读取接口也是独立数据路径，见其门面说明。

## 账号与会话

本地账号管理可直接使用 `from inspire import Accounts`，无需先构造 Client；`InspireClient.accounts` 和 `InspireAsyncClient.accounts` 指向同一个同步类，均不需要 `await`。账号状态默认保存在 `~/.inspire/accounts/<alias>/`，没有仓库级配置层。

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

同步构造签名为 `InspireClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60)`。异步构造签名为 `InspireAsyncClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, concurrency=1)`；同步构造时进行的本地配置工作，在异步客户端进入上下文或首次调用时才执行。不传凭据时使用现有账号，省略 account 使用当前账号。username/password 必须成对传入；提供凭据且省略 account 时，以 username 作为本地别名（须符合账号名称规则，邮箱等应显式提供合法 account）。缺失账号会创建；已有账号仅更新显式提供且不同的 auth/api/proxy 字段，保留其他配置。在凭据构造路径中，`proxy=None` 保留原代理，空字符串清空四个代理字段。不传 username/password 时，`base_url` 和 `proxy` 参数不覆盖现有账号配置，应先配置账号或使用凭据构造路径。构造客户端始终不改变默认账号指针，包括创建首个账号时。

`client.login(*, force=False) -> AccountInfo` 立即建立并验证会话：缓存未过期时复用它并查询当前用户，缺失或过期时调用现有 Transport 续期流程；`force=True` 主动进入续期流程。代码按会话创建时间和缓存 TTL 判断有效期（`SESSION_TTL=3600`，即 1 小时），不会因普通请求而延长本地有效期；这不证明服务器当前强制使用相同的失效时间。续期在账号刷新锁内先重读更新的磁盘缓存，再尝试 SSO Cookie 续期，最后执行受账号登录 guard 保护的 `login_without_browser`（requests CAS 凭据登录）。只有 `allow_browser=True` 才允许 Chromium 登录回退及浏览器请求通道。验证码要求保留原提示并抛 `AuthenticationError`，不会转入浏览器重试；冷却通过 `AuthenticationCooldownError.retry_at` 暴露。

`client.init(*, force=False) -> InitResult` 先登录，要求会话含真实可访问 workspace ID，然后按需原子写入账号配置，补齐平台地址和缺失的凭据；用户名可取会话中的登录身份。默认 `force=False` 仅合并这些配置字段，保留所有其他键值（包括未知节和旧表），仅在解析后的字典变化时写回；`force=True` 从账号模板和发现值重新构建，像 `inspire init --force` 一样丢弃旧表，并舍弃自定义节。返回 `config_path: Path`、按解析后字典比较的 `changed: bool` 和 `warnings: tuple[str, ...]`（当前实现为空元组）。它不写入仓库配置。**交互提示、Playwright 安装和 ssh-keygen 仍只由 CLI 提供**；SDK init 不执行这些步骤。CLI 的非交互 init 仍要求已有配置时显式指定 `--force`，其原有刷新语义保留。

导入 SDK 不加载 Click、Rich 或 Playwright。同步客户端在构造时固定账号、平台来源和配置，后续切换默认账号不影响它；异步客户端在初始化池时固定账号。一个 `InspireClient` 只在创建它的进程和线程中使用；线程 worker 或 fork 子进程各自创建同步客户端，使用 `with` 或 `close()` 释放连接。`InspireAsyncClient` 在同一进程、同一事件循环的多个任务间共享，使用 `async with` 或 `await close()`。磁盘会话、刷新锁和登录冷却与 CLI 共用。`account_info` 查询平台信息；`api_keys.plaintext(ref)` 显式返回密钥值，其他密钥视图只包含元数据。

异步登录和初始化分别为 `await client.login(force=False) -> AccountInfo` 与 `await client.init(force=False) -> InitResult`，返回类型和行为与同步形式相同。`InspireAsyncClient.from_credentials(...)` 和构造函数均为同步工厂，无需 await；`Accounts` 会做本地文件操作，事件循环敏感的应用可在启动阶段调用它。

```python
# 放在 async def 内；也可以用 async with InspireAsyncClient(...)。
client = InspireAsyncClient.from_credentials(
    "login-name", "password", account="research", concurrency=2,
)
try:
    identity = await client.login()
    result = await client.init()
    print(identity.alias, result.config_path, result.changed, result.warnings)
finally:
    await client.close()
```

## 并发、取消与生命周期

**并发模型。** `concurrency` 必须是正整数，默认为 1。每个池成员都是普通 `InspireClient`，在自己的专用线程创建，所有调用、生成器推进和关闭也回到该线程，遵守 Transport 的进程／线程亲和性规则。底层继续使用同步传输，单次发送、续期、错误分类和时间预算沿用原实现。每个成员各自持有 WebSession、HTTP／浏览器／广场连接及目录缓存（磁盘认证状态共用）；`concurrency=1` 时同一异步客户端的调用串行，增大后 `asyncio.gather` 可占用不同成员并行执行。它不是单会话内的并发请求，也不会绕过账号级认证锁、平台配额或限流。异步客户端只能在同一进程和同一事件循环中使用。

构造函数不做 I/O，也不启动线程；进入 `async with` 或首次调用时才在工作线程初始化池，并固定解析后的账号。账号、超时和缓存配置错误在此时抛出；非法 `concurrency` 在构造时立即报错。`account` 和 `base_url` 属性在初始化完成后可读。提供用户名／密码时只由首个成员保存凭据，其余成员使用该账号配置。`Accounts` 仍是共享的同步账号管理 API。`await client.cache.clear()` 清空全部成员的缓存，`await client.cache.stats()` 返回各成员计数的合计。

这一模型来自底层同步栈：JSON API 和 CAS 登录使用 `requests`，PTY websocket 使用阻塞 socket，允许浏览器回退时使用 Playwright 的同步 API。专用线程保证会话及浏览器对象的创建、使用、生成器推进和关闭发生在同一线程；它是 asyncio 接口上的线程池适配，并未把这些底层协议改成原生异步 I/O。调用方自己的 CPU 密集代码、同步 `Accounts` 调用或同步文件消费仍可能阻塞事件循环。

按同时进行的操作数设置 `concurrency`：只有短请求时从 1 开始，需要并行才增加；有 S 条长期流且希望同时执行 R 个普通请求时，至少准备 S + R 个成员。`wait()` 也会在整个轮询期间占住成员。增加成员会增加线程、连接和独立缓存的数量，不保证等比例吞吐，账号认证锁与平台限流仍然生效。

`iter`、`follow_events`、`follow_logs` 和 `exec_stream` 从首次推进到关闭，全程占用一个成员，包括调用方处理项目的时间。同步生成器的状态与后续请求、exec 的活动连接仍属于该线程，不能每产出一项就把成员交给任意其他操作。队列最多暂存一项并施加背压，但分页当前页、去重集合以及 exec 捕获仍会占内存；这不是整条流内存恒定的保证。尤其长时间 follow 的去重集合随观察历史增长。

不要在流循环体内 await 一个需要该流所占成员才能完成的操作。可给普通请求预留额外成员、用另一个客户端承载长期流，或显式 `await list(..., cursor=...)` 按页读取，在处理每页前归还成员；持续观察也可自行重复调用 `events()`／`logs()` 并在调用之间 `await asyncio.sleep(...)`。提前退出流使用 `contextlib.aclosing`，单独 `break` 不保证立即释放成员。`cache.clear()`／`cache.stats()` 会等待所有成员，因此即使还有空闲成员，也应先关闭活跃流再 await 这两个方法。

排队等待成员及首次初始化池的时间不计入底层 `operation_timeout`；该预算从工作线程开始操作时计算。需要约束调用方整体等待可使用 `asyncio.wait_for`，但取消后仍要等待已经开始的同步调用结束，所以它也不是硬时限。

**取消与关闭。** 取消等待下一项的任务会通知工作线程停止 follow；其轮询等待可立即唤醒，不必等完 `interval`。适配器只为本次 follow 绑定私有的可中断等待，不修改同步客户端或全局 `time.sleep`，原有去重、终态判断和日志收尾逻辑仍执行同步函数代码。生成器关闭也在所属工作线程进行。

已经进入的同步网络调用不能被强制抢占。取消普通调用会等待当前调用完成；取消流或关闭客户端也可能等待正在执行的请求，受既有 timeout／operation_timeout 及同步底层限制约束。exec 的流取消可在下一输出回调中停止本地读取；静默命令可能要等传输返回或超时。取消不证明远端命令已停止，也不撤销已发送写入，更不会重放请求。SDK 异常的类型、消息和异常实例原样送到等待方。

`async with` 退出及 `await client.close()` 会停止活跃流、关闭所有同步客户端并 join 全部专用线程；即使关闭任务被取消，清理仍会完成。遗漏关闭的客户端使用 daemon 线程，不会阻止解释器退出，但应用仍应显式管理上下文以回收连接。关闭后再次调用会抛 `ClientClosedError`。

## 资源引用与分页

集合接口为 `list(workspace, *, ...filters, limit=20, cursor=None)`、`iter(workspace, *, ...filters, max_items=None)` 和 `quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)`；无工作区的目录保留其首个选择参数或无参数。以工作区为主要操作对象的方法（包括 `resources.availability/policy/usage`、`account_info.permissions` 和 `servings.configs`）将 `workspace` 作为首参数，接受位置或关键字传入，其余参数仅接受关键字；`account_info.permissions(workspace=None)` 的工作区可省略。其他方法中的工作区筛选参数仅接受关键字。读取或变更已有单资源时通常以 `ref` 为首参数；创建以 `spec`、注册以 `name` 为首参数，数据集申请与验证分别使用 `name` 和 `specs`，完整签名见方法表。批量 `status(refs, *, workspace=None)` 接受名称或类型化引用的序列，按输入顺序返回元组，空序列返回空元组。

训练 Jobs 和 HPC 的 `status()` 在解析引用后通过平台批量 Action 查询，按工作区分组，每 20 个引用一次请求（重复 ID 在分块前去重）。Ray、Serving、Notebook 仍逐个引用读取。两种路径返回的资源对象与 `get()` 完全相同，包括 `raw` 和 `view`；输入顺序和重复引用均保留。名称选择仍需先进行名称解析。

`Page` 提供 `items`、`next_cursor`、`total`，通过相同过滤条件与 `cursor=page.next_cursor` 继续。Jobs、Notebook、HPC、Ray、Serving 的列表采用服务端分页，按需取页直到收集到 `limit` 项或目录结束；游标记录平台行偏移，并绑定账号、门面及查询条件，不是平台快照。迭代器沿游标继续并对身份去重，`max_items` 控制产出数。Notebook、HPC、Ray、Serving 列表未应用本地过滤时，`total` 使用平台报告的总数；应用本地状态或关键词过滤时为 `None`，平台未提供可靠总数时也为 `None`。其他目录（包括 TensorBoard）仍可能先有界枚举再做本地分页。

Jobs、Notebook 的请求页大小固定为 100，HPC、Ray、Serving 分别为 50、20、20；不会按总数扩大请求。上述工作负载列表和名称扫描使用 SDK 的 `page_num × page_size ≤ 5000` 防护，越界请求发出前就抛 `ResolutionIncompleteError`，提示用 `status`/`keyword` 收窄或用 `max_items` 截止。这一代码防护不适用于所有目录，也不证明所有平台端点当前具有同样上限。缺页、重复页或单次扫描超过 100 页时同样抛 `ResolutionIncompleteError`。Serving、Notebook、TensorBoard 名称解析先发送 `keyword` 再精确匹配；当前 HPC/Ray ListJobs 合同不支持关键词过滤，名称解析最多扫描 100 页，无法确认唯一性时抛 `ResolutionIncompleteError`，可使用已有类型化引用直接查询。

名称按完整名称消歧，多个候选抛 `AmbiguousResourceError`，候选引用在 `.candidates`。名称查询工作负载、计算组、镜像和模型时显式给 workspace；已有类型化 Ref 可省略。Ref 校验类型、账号、来源以及显式工作区；`.to_dict()` / `XRef.from_dict()` 可用于保存和恢复，不能跨账号套用。项目目录默认是全局范围；模型 list 和账号 permissions 支持 `workspace="all"`，resources 查询只接受单工作区。

```python
workspace = client.workspaces.get("工作区名称")
page = client.jobs.list(workspace.ref, limit=20)
if page.next_cursor:
    next_page = client.jobs.list(workspace.ref, cursor=page.next_cursor)
statuses = client.jobs.status([job.ref for job in page.items])
```

异步分页使用相同的 Page 和游标，在 `async def` 中：

```python
workspace = await client.workspaces.get("工作区名称")
page = await client.jobs.list(workspace.ref, limit=20)
if page.next_cursor:
    next_page = await client.jobs.list(workspace.ref, cursor=page.next_cursor)
statuses = await client.jobs.status([job.ref for job in page.items])
```

资源结果通常为 frozen dataclass，嵌套字典并非递归冻结；`MetricGroup` 和 `ServingInstanceView` 是可变 dataclass，`JobEvent` 是 `dict[str, Any]` 类型别名。提供业务视图的对象可用 `.to_dict()` 取得映射，但并非每个导出类型都有此方法（例如 `ExecResult`、`MetricGroup`）；Ref 的 `.to_dict()` 则是引用序列化格式。镜像 `ImageSelector(name, source)` 可指定 official/public/project/private，跨来源同名会报歧义。镜像 list 可返回成功来源的目录，名称 get/detail 要求完整候选集。数据集 get 接受 code 或 DatasetRef，validate 接受 `"name:version"` 或 DatasetMount；applications 的单数据集扫描有界，不保证窗口之外的申请历史。

### 缓存

每个 `InspireClient` 有独立的进程内目录缓存，默认 TTL 为 60 秒，通过 `catalog_ttl` 设置（单位：秒；`0` 禁用）。缓存覆盖工作区路由、项目目录、计算组、各来源的镜像目录、配额价格与优先级、工作区公平调度标记和当前用户；按账号、平台地址及工作区／来源／调度类型等范围隔离。任务列表、资源详情、日志、事件、指标和实时用量不缓存。名称消歧仍检查全部候选，不缓存失败或不完整的目录结果。

```python
with InspireClient(catalog_ttl=60) as client:
    # 重复的名称解析和 plan() 在 TTL 内复用目录。
    print(client.cache.stats())  # {"hits": ..., "misses": ..., "entries": ...}
    client.cache.clear()         # 清空目录；命中／未命中计数保留
```

镜像注册、删除、可见性修改和 Notebook 保存镜像会清理执行该操作的同步客户端内受影响的镜像目录；会话续期不清理目录。指定 `ImageSelector(name=..., source=...)` 只读取该来源，`ImageRef` 直接读取详情，registry URL 不枚举镜像目录。长时间运行且必须立即看到外部目录变更的进程，应设置 `catalog_ttl=0`，或在需要最新目录时先调用 `client.cache.clear()`。

异步缓存的构造参数仍为 `catalog_ttl`；`await client.cache.clear()` 清空所有成员的目录并保留计数，`await client.cache.stats()` 合计各成员的 hits／misses／entries。不同成员不共享缓存，镜像写入自动失效只作用于执行该写入的成员；需要所有成员立即看到变更时，关闭活跃流后显式 clear，或设置 `catalog_ttl=0`。

## 观察结果类型

以下九个方法返回类型化观察模型（或包含它们的 tuple／Page），模型均为 frozen dataclass；所有类型均从 `inspire.sdk` 和 `inspire` 导出。序列字段使用元组，`.to_dict()` 返回转换前的共享业务映射，保留条件键的缺省状态；可选标量字段在对象上为 `None`，可选序列为相应的空元组，并不会因此在映射中补键。

| 方法 | 返回类型 |
|---|---|
| `servings.versions` | `tuple[ServingVersion, ...]` |
| `servings.scale_history` | `Page[ServingScaleHistoryEntry]` |
| `servings.configs` | `ServingConfigs`，含 `tuple[ServingConfigItem, ...]` |
| `servings.api` | `ServingInvocationInfo`，继承 `ServingInvocationCredentials` 的扁平字段 |
| `servings.api_metrics` | `ServingAPIMetrics`，含 `ServingAPIMetricTimeRange` 和 `tuple[ServingAPIMetricSeries, ...]` |
| `tensorboards.tags` | `TensorboardTags` |
| `tensorboards.scalars` | `TensorboardScalars`，含 `tuple[TensorboardScalarSeries, ...]` 与 `tuple[TensorboardScalarPoint, ...]` |
| `ray.scaling` | `tuple[RayScalingEvent, ...]` |
| `notebooks.lifecycle` | `tuple[NotebookRun, ...]` |

Serving 调用信息沿用 `credential_env`、`auth_header`、`auth_scheme`、`affinity_header` 字段，不获取密钥。配置的 `auto_stop` 是可选布尔值，配置项的 `auto_stop_rules` 保留服务返回的规则字符串；API 指标系列只有摘要，不添加原始点集。TensorBoard 标量点通过 `.step`、`.value` 读取，`.to_list()` 返回原有 `[step, value]`；外层 `.to_dict()` 将序列还原为列表。`scalar_tags` 按运行名称映射到标签元组。

```python
for version in client.servings.versions(serving_ref):
    print(version.version, version.status)
for series in client.tensorboards.scalars(board_ref, points=10).series:
    print(series.run, series.tag, series.last_value)
    for point in series.points:
        print(point.step, point.value)
```

异步形式同样返回这些类型化模型；例如在 `async def` 中：

```python
for version in await client.servings.versions(serving_ref):
    print(version.version, version.status)
scalars = await client.tensorboards.scalars(board_ref, points=10)
for series in scalars.series:
    for point in series.points:
        print(point.step, point.value)
```

## 各门面方法表

两种客户端均有 15 个资源门面；资源门面共有 149 个同步方法、154 个异步方法（多出的 5 个是 exec_stream）。另有 cache 的 2 个方法，因此全部实例门面合计 151／156 个方法。计数包含继承的公开方法，不包含客户端自身的 login/init/close/from_credentials、属性、上下文协议或共享的 Accounts 类。

| 门面 | 同步方法数 | 异步方法数 |
|---|---:|---:|
| `cache` | 2 | 2 |
| `workspaces` | 2 | 2 |
| `projects` | 4 | 4 |
| `compute_groups` | 2 | 2 |
| `images` | 7 | 7 |
| `jobs` | 19 | 20 |
| `hpc` | 16 | 17 |
| `ray` | 18 | 19 |
| `servings` | 24 | 25 |
| `tensorboards` | 11 | 11 |
| `notebooks` | 21 | 22 |
| `account_info` | 4 | 4 |
| `api_keys` | 5 | 5 |
| `datasets` | 5 | 5 |
| `models` | 7 | 7 |
| `resources` | 4 | 4 |

以下参数签名省略类型注解，返回类型单列；`*` 后参数必须以关键字传入。CLI 栏表示对应平台能力，名称解析、迭代及等待可以组合一个 CLI 子命令的能力；不会启动 CLI 子进程。

### workspaces

同步用 `client.workspaces.方法(...)`；异步用 `await client.workspaces.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `workspaces.get(ref)` | `Resource[WorkspaceRef]` | `config context / account context（名称选择）` |
| `workspaces.list(*, limit=20, cursor=None)` | `Page[Resource[WorkspaceRef]]` | `config context / account context` |

### projects

同步用 `client.projects.方法(...)`；异步用 `await client.projects.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `projects.detail(ref, *, workspace=None)` | `ProjectDetail` | `project detail` |
| `projects.get(ref, *, workspace=None)` | `ProjectInfo` | `project list（名称选择）` |
| `projects.list(workspace=None, *, limit=20, cursor=None)` | `Page[ProjectInfo]` | `project list` |
| `projects.owners()` | `tuple[ProjectOwner, ...]` | `project owners` |

### compute_groups

同步用 `client.compute_groups.方法(...)`；异步用 `await client.compute_groups.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `compute_groups.get(ref, *, workspace=None)` | `Resource[ComputeGroupRef]` | `account context（计算组目录）（名称选择）` |
| `compute_groups.list(workspace, *, limit=20, cursor=None)` | `Page[Resource[ComputeGroupRef]]` | `account context（计算组目录）` |

### images

同步用 `client.images.方法(...)`；异步用 `await client.images.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `images.delete(ref, *, workspace=None)` | `None` | `image delete` |
| `images.detail(ref, *, workspace=None)` | `ImageDetail` | `image detail` |
| `images.get(ref, *, workspace=None)` | `Image` | `image list（名称选择）` |
| `images.list(workspace, *, source=None, keyword=None, limit=20, cursor=None)` | `Page[Image]` | `image list` |
| `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` | `ImageRegisterHandle` | `image register` |
| `images.set_visibility(ref, *, visibility, workspace=None)` | `None` | `image set-visibility` |
| `images.wait_ready(ref, *, timeout=600, poll_interval=5, workspace=None)` | `CustomImageInfo` | `image register --wait` |

### datasets

同步用 `client.datasets.方法(...)`；异步用 `await client.datasets.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `datasets.applications(name=None, *, to_approve=False, keyword=None, limit=20, cursor=None)` | `Page[DatasetApplication]` | `dataset applications` |
| `datasets.get(ref)` | `DatasetDetail` | `dataset show` |
| `datasets.list(keyword=None, *, tag=None, limit=20, cursor=None)` | `Page[DatasetInfo]` | `dataset list` |
| `datasets.tags()` | `tuple[DatasetTag, ...]` | `dataset tags` |
| `datasets.validate(specs, *, workspace)` | `tuple[DatasetValidation, ...]` | `dataset validate` |

### models

同步用 `client.models.方法(...)`；异步用 `await client.models.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `models.delete(ref, *, force=False, workspace=None, project=None)` | `None` | `model delete` |
| `models.deploy_config(ref, *, workspace=None, project=None, version=None)` | `ModelDeployConfig` | `model deploy-config` |
| `models.get(ref, *, workspace=None, project=None)` | `ModelInfo` | `model list（名称选择）` |
| `models.list(workspace, *, project=None, keyword=None, limit=20, cursor=None)` | `Page[ModelInfo]` | `model list` |
| `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)` | `ModelRegisterHandle` | `model register` |
| `models.status(refs, *, workspace=None, project=None)` | `tuple[ModelStatus, ...]` | `model status` |
| `models.versions(ref, *, workspace=None, project=None)` | `tuple[ModelVersion, ...]` | `model versions` |

### resources

同步用 `client.resources.方法(...)`；异步用 `await client.resources.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `resources.availability(workspace, *, group=None, include_cpu=False)` | `tuple[ResourceAvailability, ...]` | `resources availability` |
| `resources.node_events(nodes, *, since=None, type=None, reason=None, limit=None, from_component=None)` | `EventResult` | `resources node-events` |
| `resources.policy(workspace, *, workload=None)` | `tuple[WorkloadSchedulePolicy, ...]` | `resources policy` |
| `resources.usage(workspace, *, project=None, user=None, task=None, group=None, mine=False, details=False, limit=None)` | `ResourceUsage` | `resources usage` |

### account_info

同步用 `client.account_info.方法(...)`；异步用 `await client.account_info.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `account_info.check()` | `AccountCheck` | `account check` |
| `account_info.context(*, limit=None)` | `AccountContext` | `account context` |
| `account_info.current()` | `AccountInfo` | `account current` |
| `account_info.permissions(workspace=None)` | `tuple[Permission, ...]` | `account permissions` |

### api_keys

同步用 `client.api_keys.方法(...)`；异步用 `await client.api_keys.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `api_keys.create(name)` | `APIKeyInfo` | `account api-key create` |
| `api_keys.delete(ref)` | `APIKeyInfo` | `account api-key delete` |
| `api_keys.get(ref)` | `APIKeyInfo` | `account api-key list（名称选择）` |
| `api_keys.list(*, limit=20, cursor=None)` | `Page[APIKeyInfo]` | `account api-key list` |
| `api_keys.plaintext(ref)` | `str` | `account api-key export（只返回明文，不导出文件）` |

### jobs

同步用 `client.jobs.方法(...)`；异步用 `await client.jobs.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`follow_logs`、`iter` 同步返回迭代器，异步直接用 `async for item in client.jobs.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.jobs.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `jobs.command(ref, *, workspace=None)` | `str` | `job command` |
| `jobs.create(spec, *, operation_id=None)` | `JobHandle` | `job create` |
| `jobs.delete(ref, *, workspace=None)` | `None` | `job delete` |
| `jobs.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `job events` |
| `jobs.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `job shell（非交互执行）` |
| `jobs.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `job events --follow` |
| `jobs.follow_logs(ref, *, interval=2, **filters)` | `Iterator[LogResult]／AsyncIterator[LogResult]` | `job logs --follow` |
| `jobs.get(ref, *, workspace=None)` | `Job` | `job status` |
| `jobs.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `job instances` |
| `jobs.instances(ref, *, workspace=None)` | `tuple[JobInstance, ...]` | `job instances` |
| `jobs.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Job]／AsyncIterator[Job]` | `job list --all` |
| `jobs.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Job]` | `job list` |
| `jobs.logs(ref, *, workspace=None, instances='all', window=None, start=None, end=None, tail=None, head=None, limit=100, max_chars=None)` | `LogResult` | `job logs` |
| `jobs.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval='1m', group=None)` | `tuple[MetricGroup, ...]` | `job metrics` |
| `jobs.plan(spec)` | `JobPlan` | `job create --dry-run` |
| `jobs.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `job quota` |
| `jobs.status(refs, *, workspace=None)` | `tuple[Job, ...]` | `job status` |
| `jobs.stop(ref, *, workspace=None)` | `None` | `job stop` |
| `jobs.wait(ref, *, workspace=None, timeout=3600, poll_interval=10, raise_on_failure=False)` | `Job` | `job wait` |

与 HPC、Ray、Serving 一样，训练任务 `Job.raw` 保留平台载荷，`Job.view` 是稳定的公开投影，`Job.to_dict()` 返回 `view` 的浅拷贝。`jobs.get()` 的 `view` 与同一详情载荷的 `inspire job status --json` 业务字段一致；`list()` / `iter()` 和批量 `status()` 也提供 `raw` / `view`，字段取决于平台记录。计算组、资源、节点与优先级等公开字段可从 `view` 读取；镜像等未进入公开投影的详情字段可从 `raw` 读取。

`jobs.logs()` 的默认时间范围及显式 `window` 最长为 30 天：保留结束时间并向后移动开始时间；CLI `job logs` 复用同一截断逻辑。

### notebooks

同步用 `client.notebooks.方法(...)`；异步用 `await client.notebooks.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.notebooks.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.notebooks.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。`lifecycle` 返回 `tuple[NotebookRun, ...]`，`metrics` 与 `realtime_metrics` 是不同接口。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `notebooks.cancel_save_image(ref, *, workspace=None)` | `bool` | `notebook cancel-save-image` |
| `notebooks.create(spec, *, operation_id=None)` | `NotebookHandle` | `notebook create` |
| `notebooks.delete(ref, *, workspace=None)` | `None` | `notebook delete` |
| `notebooks.estimate_image_size(ref, *, workspace=None)` | `NotebookImageSizeEstimate` | `notebook save-image --dry-run` |
| `notebooks.events(ref, *, keyword=None, limit=100, workspace=None)` | `EventResult` | `notebook events` |
| `notebooks.exec(ref, *, command, workspace=None, cwd=None, env=None, timeout=120, transport='auto', on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `notebook exec` |
| `notebooks.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `notebook events --follow` |
| `notebooks.get(ref, *, workspace=None)` | `Notebook` | `notebook status` |
| `notebooks.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Notebook]／AsyncIterator[Notebook]` | `notebook list --all` |
| `notebooks.lifecycle(ref, *, limit=None, workspace=None)` | `tuple[NotebookRun, ...]` | `notebook lifecycle` |
| `notebooks.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Notebook]` | `notebook list` |
| `notebooks.metrics(ref, *, metric='core', window='1h', start=None, end=None, interval=None, group=None, workspace=None)` | `tuple[MetricGroup, ...]` | `notebook metrics` |
| `notebooks.plan(spec)` | `NotebookPlan` | `notebook create --dry-run` |
| `notebooks.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `notebook quota` |
| `notebooks.realtime_metrics(ref, *, workspace=None)` | `tuple[NotebookResourceSnapshot, ...]` | `notebook metrics --now` |
| `notebooks.save_image(ref, *, name, version=None, description=None, visibility=None, flatten=False, workspace=None)` | `ImageSaveHandle` | `notebook save-image` |
| `notebooks.start(ref, *, workspace=None)` | `None` | `notebook start` |
| `notebooks.status(refs, *, workspace=None)` | `tuple[Notebook, ...]` | `notebook status` |
| `notebooks.stop(ref, *, workspace=None)` | `None` | `notebook stop` |
| `notebooks.wait(ref, *, timeout=600, poll_interval=5, target='RUNNING', raise_on_failure=False, workspace=None)` | `Notebook` | `notebook create --wait / status（轮询）` |
| `notebooks.wait_image_ready(ref, *, timeout=600, poll_interval=5)` | `CustomImageInfo` | `notebook save-image --wait` |

### hpc

同步用 `client.hpc.方法(...)`；异步用 `await client.hpc.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.hpc.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.hpc.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `hpc.create(spec, *, operation_id=None)` | `HPCJobHandle` | `hpc create` |
| `hpc.delete(ref, *, workspace=None)` | `None` | `hpc delete` |
| `hpc.events(ref, *, workspace=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `hpc events` |
| `hpc.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `hpc shell（非交互执行）` |
| `hpc.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `hpc events --follow` |
| `hpc.get(ref, *, workspace=None)` | `HPCJob` | `hpc status` |
| `hpc.instances(ref, *, workspace=None)` | `tuple[HPCInstanceView, ...]` | `hpc instances` |
| `hpc.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[HPCJob]／AsyncIterator[HPCJob]` | `hpc list --all` |
| `hpc.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[HPCJob]` | `hpc list` |
| `hpc.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `hpc logs` |
| `hpc.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `hpc metrics` |
| `hpc.plan(spec)` | `HPCJobPlan` | `hpc create --dry-run` |
| `hpc.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `hpc quota` |
| `hpc.status(refs, *, workspace=None)` | `tuple[HPCJob, ...]` | `hpc status` |
| `hpc.stop(ref, *, workspace=None)` | `None` | `hpc stop` |
| `hpc.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `HPCJob` | `hpc status（SDK 轮询等待终态）` |

### ray

同步用 `client.ray.方法(...)`；异步用 `await client.ray.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.ray.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.ray.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `ray.create(spec, *, operation_id=None)` | `RayJobHandle` | `ray create` |
| `ray.delete(ref, *, workspace=None)` | `None` | `ray delete` |
| `ray.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `ray events` |
| `ray.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `ray shell（非交互执行）` |
| `ray.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `ray events --follow` |
| `ray.get(ref, *, workspace=None)` | `RayJob` | `ray status` |
| `ray.instances(ref, *, workspace=None)` | `tuple[RayInstanceView, ...]` | `ray instances` |
| `ray.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[RayJob]／AsyncIterator[RayJob]` | `ray list --all` |
| `ray.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[RayJob]` | `ray list` |
| `ray.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `ray logs` |
| `ray.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `ray metrics` |
| `ray.plan(spec)` | `RayJobPlan` | `ray create --dry-run` |
| `ray.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `ray quota` |
| `ray.scaling(ref, *, group=None, limit=None, workspace=None)` | `tuple[RayScalingEvent, ...]` | `ray scaling` |
| `ray.start(ref, *, workspace=None)` | `None` | `ray start` |
| `ray.status(refs, *, workspace=None)` | `tuple[RayJob, ...]` | `ray status` |
| `ray.stop(ref, *, workspace=None)` | `None` | `ray stop` |
| `ray.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `RayJob` | `ray status（SDK 轮询等待终态）` |

### servings

同步用 `client.servings.方法(...)`；异步用 `await client.servings.方法(...)`，本节全部参数、默认值与返回模型相同。例外：`follow_events`、`iter` 同步返回迭代器，异步直接用 `async for item in client.servings.follow_events(...)`，不用先 await；提前退出时用 `contextlib.aclosing`。异步另有 `client.servings.exec_stream(...) -> AsyncIterator[str]`，参数及默认值与下表 `exec` 完全相同，直接 async for；只交付字符串块，不返回最终 ExecResult。完整输出、回调与关闭规则见[远程执行](#远程执行exec)。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `servings.api(ref, *, affinity_key=None, workspace=None)` | `ServingInvocationInfo` | `serving api` |
| `servings.api_metrics(ref, *, metric=None, window='1h', interval=None, workspace=None)` | `ServingAPIMetrics` | `serving api-metrics` |
| `servings.configs(workspace)` | `ServingConfigs` | `serving configs` |
| `servings.create(spec, *, operation_id=None)` | `ServingHandle` | `serving create` |
| `servings.delete(ref, *, workspace=None)` | `None` | `serving delete` |
| `servings.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `serving events` |
| `servings.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `serving shell（非交互执行）` |
| `servings.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]／AsyncIterator[EventResult]` | `serving events --follow` |
| `servings.get(ref, *, workspace=None)` | `Serving` | `serving status` |
| `servings.instances(ref, *, workspace=None)` | `tuple[ServingInstanceView, ...]` | `serving instances` |
| `servings.iter(workspace, *, project=None, status=None, keyword=None, max_items=None)` | `Iterator[Serving]／AsyncIterator[Serving]` | `serving list --all` |
| `servings.list(workspace, *, project=None, status=None, keyword=None, limit=20, cursor=None)` | `Page[Serving]` | `serving list` |
| `servings.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `serving logs` |
| `servings.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `serving metrics` |
| `servings.plan(spec)` | `ServingPlan` | `serving create --dry-run` |
| `servings.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `serving quota` |
| `servings.rollback(ref, *, version, workspace=None)` | `None` | `serving rollback` |
| `servings.scale(ref, *, replicas, workspace=None)` | `None` | `serving scale` |
| `servings.scale_history(ref, *, workspace=None, limit=20, cursor=None)` | `Page[ServingScaleHistoryEntry]` | `serving scale-history` |
| `servings.start(ref, *, workspace=None)` | `None` | `serving start` |
| `servings.status(refs, *, workspace=None)` | `tuple[Serving, ...]` | `serving status` |
| `servings.stop(ref, *, workspace=None)` | `None` | `serving stop` |
| `servings.versions(ref, *, workspace=None)` | `tuple[ServingVersion, ...]` | `serving versions` |
| `servings.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None, target='RUNNING')` | `Serving` | `serving start / create（状态等待）` |

### 远程执行（exec）

Notebook、Job、HPC、Ray 和 Serving 都提供同步 `client.<门面>.exec(...)` 和异步 `await client.<门面>.exec(...)`，返回 frozen dataclass `ExecResult`，可从 `inspire` 或 `inspire.sdk` 导入。遵循统一签名契约，`ref` 之后的参数（包括 `command`）只接受关键字。

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

训练 Job、HPC Job、Ray Job 和 Serving 的 exec 始终使用平台交互式 PTY websocket，不提供 SSH 或分离输出选项。Job 的 `instance` 接受实例名、`rank=N`、裸数字或角色；不指定时要求恰好一个运行中实例，多实例会抛出列出候选的 `ValidationError`。HPC 默认选择 `launcher`，Ray 默认选择 `head`；默认角色不存在或匹配多个运行中实例时需要显式指定。Serving 默认选第一个运行中副本。显式实例名或工作负载公开标签必须匹配一个运行中实例；角色匹配多个副本同样报歧义。

命令按以下顺序组合：Client 配置的 `remote_env`、调用者的 `env`、可选的 `cd "<cwd>" && `，最后是 `command`。调用者的同名变量覆盖配置值；`env` 值按字面量引用，空字符串保留为空，不从本机环境补值。SSH 沿用 `bash -l` 执行方式。

`ExecResult` 的全部字段为 `returncode`、`output`、`stdout`、`stderr`、`completed`、`transport`、`instance=""`、`truncated=False` 和 `total_output_bytes=0`。`transport` 为 `ssh`、`jupyter` 或 `pty`；工作负载 PTY 的 `instance` 是选中的实例名。SSH 保留独立 stdout/stderr，`output = stdout + stderr`，此拼接不表示跨流时间顺序。PTY（包括 Notebook Jupyter terminal）的 stdout/stderr 已由远端终端合并，`stdout == output`、`stderr == ""`。PTY／Jupyter 结果尽力去除已识别的输入回显前缀，通过唯一完成 marker 提取退出码；SSH 使用子进程退出状态，不依赖终端 marker。普通非零退出码直接返回结果。

同步 exec、异步 exec 及异步 exec_stream 都支持以下仅关键字参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `max_output_bytes` | `4 * 1024 * 1024`（4 MiB） | UTF-8 内存捕获预算，包含省略标记；允许 `None` 显式取消限制，整数至少为 56 |
| `output_to` | `None` | 本地路径（`str` / `os.PathLike`）或可写文本文件对象，逐块写入完整解码流 |
| `capture` | `True` | `False` 时 `output`、`stdout`、`stderr` 均为空，仍检测命令完成情况（PTY／Jupyter 使用 marker）并统计字节数 |

超出捕获预算时保留头尾，中间插入 `\n[... output truncated ...]\n`，并设置 `ExecResult.truncated=True`。`total_output_bytes` 统计实际读到的解码字符串按 UTF-8 编码后的字节数，包括终端提示符、回显和完成标记，与是否捕获无关。新字段在结果末尾提供默认值；字节计数属于观测元数据，不参与结果相等性比较。`capture=False` 的主动关闭捕获不算截断，`truncated=False`。

PTY 和 Jupyter 使用固定扫描窗口及额外有界的终端前缀空间（128 KiB），用于清理输入回显；返回文本仍遵守捕获预算。内存还包括当前传输帧和解码临时对象，预算不是整个进程 RSS 的硬上限。SSH 将预算平分给 stdout/stderr，任一流超过自己的份额就报告截断；返回 `output` 仍按 stdout + stderr 拼接。

| 传输 | `max_output_bytes` / `capture` / `output_to` / `on_output` |
|---|---|
| Job、HPC、Ray、Serving PTY | 全部支持；文件和回调保留原始合并终端流 |
| Notebook Jupyter | 全部支持；文件和回调保留原始合并终端流 |
| Notebook SSH | 全部支持；分别捕获 stdout/stderr，文件和回调按实际读取顺序合并 |

路径以 UTF-8、覆盖模式打开，保留换行；执行完成或失败时关闭 SDK 打开的文件。调用者传入的文本对象由调用者关闭和 flush。文件或回调错误向调用方传播，不自动重放命令。`output_to` 保存的是实际读到的完整原始流，不是清理回显后的 `output`；需原样保留时使用路径或保留换行的文本对象。

```python
from inspire.sdk import iter_output_file

result = client.jobs.exec(
    job_ref,
    command="python produce_large_report.py",
    output_to="report.txt",
    max_output_bytes=1024 * 1024,
)
print(result.returncode, result.truncated, result.total_output_bytes)
# 每次最多读取 65536 个字符；超长单行也不会整行装入内存。
for page in iter_output_file("report.txt", chunk_size=65536):
    consume_page(page)

# 只保存完整文件，不保留返回文本：
result = client.notebooks.exec(
    notebook_ref, command="cat /tmp/large.log",
    output_to="large.log", capture=False,
)
```

`on_output` 在读取时按顺序接收解码后的字符串块。PTY 回调收到原始终端流，可能包含提示符、输入回显、ANSI 控制符及完成 marker；最终 `output` 才是解析后的输出。命令默认没有交互 stdin，需使用命令内的管道或远端文件重定向；人工交互仍用 CLI shell。

PTY／Jupyter 执行等待超时或连接在完成 marker 出现前结束时，返回 `returncode=124`、`completed=False`，尽可能保留已捕获输出；它不证明远端进程已停止。命令自己返回 124 且 marker 完整时，`completed=True`。Notebook SSH 超时同样返回 124／False；SSH 正常返回退出状态（包括 124）时 completed=True，但 SSH 自身连接失败也可能表现为非零退出状态，仍需检查 stderr。SDK 的总 operation 时间预算也会限制传输等待；名称解析和实例查询沿用现有 SDK 错误约定。websocket 握手 401 可在命令发送前续期一次并重试，不经过 `Transport.request` 或 `single_send`，已发送的命令不会因执行失败自动重放。

平台 PTY 接口只给一条终端字节流：stdout／stderr 的合并、退出码需通过命令内 marker 回传、终端可能回显输入，都是该执行路径的限制。SDK 无法从合并后的字节流可靠恢复原始通道，清理回显也只是尽力而为。Notebook 需要分离输出时显式选择已有 SSH 桥，`auto` 的结果需检查 `result.transport`；其他四种工作负载需在远端命令中将两路输出重定向到不同文件，再通过适当文件访问方式读取。

此前的无界内存捕获和反复扫描完整历史输出属于 SDK 实现问题，现已用默认 4 MiB 头尾捕获、固定窗口增量 marker 扫描和摊销线性的缓冲写入修复。`max_output_bytes=None` 仍可显式恢复无限捕获；`capture=False` 配合 `output_to`／回调／异步块流适合长输出。平台 PTY 限制与这些已修复的实现问题应分别理解。

Jobs、Notebooks、HPC、Ray 和 Servings 的 `exec_stream(...)` 参数与各自 `exec(...)` 相同，提供字符串块异步迭代器；它执行命令一次，不在流结束后再次执行。PTY／Jupyter 的块是原始合并终端流；Notebook SSH 的块按读取顺序交错，字符串块不附带 stdout／stderr 标签，因此要取得分离输出应使用 `await client.notebooks.exec(ref, transport="ssh", command=...)` 的最终结果。`exec_stream` 只交付块，不交付最终 `ExecResult`；需要返回码、completed 或截断统计时使用 `await client.jobs.exec(...)` 等普通形式。异步客户端的 `exec` 和 `exec_stream` 的 `on_output` 回调均在所属工作线程按顺序运行（同步 exec 在调用线程运行），必须是同步回调；异步应用可直接使用 `exec_stream` 消费块，无须自己桥接线程。`output_to`、`capture` 和输出大小限制沿用同步接口，长输出建议使用 `capture=False`。

```python
from contextlib import aclosing

async def execute(client: InspireAsyncClient, job_ref) -> None:
    async with aclosing(client.jobs.exec_stream(
        job_ref, command="python -u train.py", capture=False,
    )) as chunks:
        async for chunk in chunks:
            print(chunk, end="", flush=True)

async def check_notebook(client: InspireAsyncClient, notebook_ref) -> None:
    result = await client.notebooks.exec(
        notebook_ref, command="python check.py", transport="ssh",
    )
    print(result.returncode, result.stdout, result.stderr)
```

### tensorboards

TensorBoard 资源的创建、状态和生命周期查询走共享控制台传输；`tags`／`scalars` 的运行目录、标签和标量数据来自 TensorBoard 应用自身的 HTTP 接口。应用读取通过共享 `build_requests_session` 构造临时会话并同步 GET，不经过 `Transport.request()`；该临时会话不属于 Transport 持有的连接池。当前每次应用 GET 默认超时为 60 秒，不继承客户端 `timeout` 或剩余 `operation_timeout`，也没有该 dispatcher 的续期／重试保证。异步形式仍在所属池线程执行。`points` 只裁剪结果中的尾部点集，底层会读取相应系列再汇总。

同步用 `client.tensorboards.方法(...)`；异步用 `await client.tensorboards.方法(...)`，本节全部参数、默认值与返回模型相同。

| 方法（同步调用／异步 await） | 返回值／await 结果 | CLI 子命令 |
|---|---|---|
| `tensorboards.create(spec, *, operation_id=None)` | `TensorboardHandle` | `tensorboard create` |
| `tensorboards.delete(ref, *, workspace=None)` | `None` | `tensorboard delete` |
| `tensorboards.get(ref, *, workspace=None)` | `Tensorboard` | `tensorboard status` |
| `tensorboards.list(workspace, *, status=None, job=None, keyword=None, limit=20, cursor=None)` | `Page[Tensorboard]` | `tensorboard list` |
| `tensorboards.scalars(ref, *, tag='', run=None, points=None, workspace=None)` | `TensorboardScalars` | `tensorboard scalars` |
| `tensorboards.start(ref, *, workspace=None)` | `None` | `tensorboard start` |
| `tensorboards.status(refs, *, workspace=None)` | `tuple[Tensorboard, ...]` | `tensorboard status` |
| `tensorboards.stop(ref, *, workspace=None)` | `None` | `tensorboard stop` |
| `tensorboards.tags(ref, *, workspace=None)` | `TensorboardTags` | `tensorboard tags` |
| `tensorboards.url(ref, *, workspace=None)` | `str` | `tensorboard status（应用 URL）` |
| `tensorboards.wait(ref, *, target='running', raise_on_failure=False, timeout=60, poll_interval=3, workspace=None)` | `Tensorboard` | `tensorboard start / stop（状态等待）` |

## 公共导出与类型清单

`inspire.sdk.__all__` 当前包含 **125 个名称**；`from inspire import X` 对这些名称返回同一个对象。以下按导出名内省分组，不包含内部 facade 类。`inspire` 使用懒加载属性，并未定义同等的 `__all__`；使用显式导入，不依赖 `from inspire import *`。

- 入口与账号工具（3）：`Accounts`、`InspireAsyncClient`、`InspireClient`。
- 引用类型（19）：`APIKeyRef`、`ComputeGroupRef`、`DatasetApplicationRef`、`DatasetRef`、`DatasetTagRef`、`DatasetVersionRef`、`HPCJobRef`、`ImageRef`、`JobRef`、`ModelRef`、`NotebookRef`、`ProjectOwnerRef`、`ProjectRef`、`QuotaRef`、`RayJobRef`、`ResourceRef`、`ServingRef`、`TensorboardRef`、`WorkspaceRef`。
- 创建规格（6）：`HPCJobCreateSpec`、`JobCreateSpec`、`NotebookCreateSpec`、`RayJobCreateSpec`、`ServingCreateSpec`、`TensorboardCreateSpec`。
- 结果、资源与值模型（75）：`APIKeyInfo`、`AccountCheck`、`AccountContext`、`AccountInfo`、`DatasetApplication`、`DatasetDetail`、`DatasetInfo`、`DatasetMount`、`DatasetTag`、`DatasetValidation`、`DatasetVersion`、`EventResult`、`ExecResult`、`HPCInstanceView`、`HPCJob`、`HPCJobHandle`、`HPCJobPlan`、`Image`、`ImageDetail`、`ImageRegisterHandle`、`ImageSaveHandle`、`ImageSelector`、`InitResult`、`Job`、`JobHandle`、`JobInstance`、`JobPlan`、`LogResult`、`MetricGroup`、`ModelDeployConfig`、`ModelInfo`、`ModelRegisterHandle`、`ModelStatus`、`ModelVersion`、`Notebook`、`NotebookHandle`、`NotebookImageSizeEstimate`、`NotebookPlan`、`NotebookResourceSnapshot`、`NotebookRun`、`Page`、`Permission`、`ProjectDetail`、`ProjectInfo`、`ProjectOwner`、`Quota`、`QuotaOption`、`RayInstanceView`、`RayJob`、`RayJobHandle`、`RayJobPlan`、`RayScalingEvent`、`Resource`、`ResourceAvailability`、`ResourceUsage`、`Serving`、`ServingAPIMetricSeries`、`ServingAPIMetricTimeRange`、`ServingAPIMetrics`、`ServingConfigItem`、`ServingConfigs`、`ServingHandle`、`ServingInstanceView`、`ServingInvocationCredentials`、`ServingInvocationInfo`、`ServingPlan`、`ServingScaleHistoryEntry`、`ServingVersion`、`Tensorboard`、`TensorboardHandle`、`TensorboardScalarPoint`、`TensorboardScalarSeries`、`TensorboardScalars`、`TensorboardTags`、`WorkloadSchedulePolicy`。
- 异常（20）：`AmbiguousResourceError`、`AuthenticationCooldownError`、`AuthenticationError`、`ClientClosedError`、`ClientThreadError`、`ConfigurationError`、`HPCJobFailedError`、`InspireError`、`JobFailedError`、`MutationUncertainError`、`NotebookFailedError`、`RayJobFailedError`、`ResolutionIncompleteError`、`ResourceNotFoundError`、`ServingFailedError`、`SubmissionUncertainError`、`TensorboardFailedError`、`TransportError`、`ValidationError`、`WaitTimeoutError`。
- 函数与类型别名（2）：`JobEvent`、`iter_output_file`。

其中 100 个导出对象满足 `dataclasses.is_dataclass`（包括继承 dataclass 的引用类），98 个 frozen；不能把“类型化”理解为所有返回值均不可变或均有 to_dict。`CustomImageInfo` 是两处镜像等待方法的返回类，可从 `inspire.platform.web.browser_api.images` 导入，不在上述顶层导出清单中。

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

异步创建使用相同的规格对象与返回模型，在 `async def` 中执行：

```python
plan = await client.jobs.plan(spec)
print(plan.summary)
handle = await client.jobs.create(spec, operation_id="pipeline-stage-1")
finished = await client.jobs.wait(handle.ref, raise_on_failure=True)
```

`plan()` 可能读取目录和校验接口，属于只读平台操作；构造 `CreateSpec` 本身不联网。异步 `plan()` 也必须 await。

## 写操作与 single_send

写操作由应用显式调用。注册签名为 `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` 和 `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)`。镜像注册预留推送槽位并返回 registry 地址，version 默认 v1、visibility 默认 private；模型注册共享盘目录，不上传本地文件。

SDK 的资源 JSON 变更请求显式进入 `single_send`（不包括认证握手或 exec 数据流），最多发送一次；发送后失败不刷新、不重试、不换通道。创建／注册无法确认结果时抛 `SubmissionUncertainError(operation_id)`，其他变更抛 `MutationUncertainError`。operation_id 默认生成，允许任意非空诊断字符串，**不是服务器幂等键**。遇到不确定结果，先显式查询确认，再决定后续动作。

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

异步观察返回相同模型；follow 是异步迭代器而不是需 await 的协程：

```python
from contextlib import aclosing

# 放在 async def 内。
logs = await client.jobs.logs(handle.ref, window="30m", instances="all", tail=50)
events = await client.jobs.events(handle.ref, type="Warning", reason="sched", limit=20)
metrics = await client.jobs.metrics(handle.ref, metric="gpu,cpu", window="2h")
async with aclosing(client.jobs.follow_logs(handle.ref, interval=2)) as updates:
    async for update in updates:
        print(update.text)
        break  # aclosing 确保提前退出时关闭生成器并归还成员。
```

训练 Jobs 日志的 window 与 CLI 共用解析器，接受 `30m`、`2h`、`1d` 等正整数窗口。显式 window 以当前时间为终点；默认 None 使用任务创建 / 完成时间并前后各留 10 分钟，缺少创建时间时回看 24 小时。也可传 datetime start/end 指定绝对窗口。

Jobs 的 `instances="all"` 发现实例；显式列表直接用于平台调用，不先校验它是否在发现结果中。tail/head 互斥，与 CLI 共用日志拉取与排序选择逻辑：请求条数为 `max(limit, tail, head)`，按时间排序后取头部或尾部；省略 tail/head 时取 limit 条尾部记录。平台返回有限样本，这不额外保证全局最后 N 条或无损续读。`max_chars=None` 默认不做字符截断；指定时裁剪格式化文本并设置 truncated。items 保留按条数选择的结构化记录。

训练 Jobs、HPC、Ray、Serving 的事件默认合并任务级与实例级事件；type 精确匹配 Normal/Warning（大小写不敏感），reason 做子串匹配。instance 接受单个标签或标签列表，按工作负载选择实例（Jobs 支持 `rank=0`、`0` 和角色名称）；workload_level 与 instance 互斥。limit 选择过滤后的最近事件。follow 按事件内容或日志标识去重，是轮询观察接口，不是平台持久订阅或无损游标。Jobs 的事件 follow 到终态停止，日志 follow 在检测终态后再拉取一轮；Notebooks、HPC、Ray、Servings 的事件 follow 持续轮询，需调用方关闭。

指标与 CLI 共用参数解析和平台样本提取：metric 支持 core/all、逗号分隔别名和原始指标名；start/end 支持 CLI 时间字符串，SDK 也接受 datetime。start 优先于 window。group 可覆盖从详情推断的计算组。

HPC 默认日志窗口取实例时间，Ray 取任务详情时间，窗口长度均最多 30 天，保留窗口结束时间。HPC 对 tail 或默认查询在 total 超过返回记录数时扩大一次请求，head 不扩大；Ray 在返回样本中选择。Serving 默认读取 24 小时／100 条，实例发现和日志使用 CLI 的共享核心。均不承诺全局最后 N 条或无损续读。

SDK 的 notebooks 门面不提供平台程序日志接口；`metrics` 返回 `tuple[MetricGroup, ...]`，实时资源快照由 `realtime_metrics` 单独返回。TensorBoard tags/scalars 要求 running；标量按 step 汇总首末值、min/max，points 缺省只返回摘要，指定正整数可获取尾部点集，`points=0` 与省略参数一样只返回摘要。Serving api 返回结构化调用信息，不发送推理请求；端点存在不证明服务已就绪，api_metrics 另提供 QPS／成功率／延迟序列摘要。

## 错误与时间预算

进入 SDK JSON 写入块时，若 `Transport.request()` 从未返回响应，或距上次返回已满 60 秒，SDK 先通过普通 READ 路径执行一次 `GetUserDetail` 探测，必要时先续期再发送写入；60 秒内已有返回响应则省略探测；此时间戳不代表业务操作一定成功，广场响应也不更新该时间戳。探测共享时间预算，其刷新与重试不计入写请求的单次发送。探测失败时不发送写入；写入发出后即使收到 401 也绝不重放，创建抛 `SubmissionUncertainError`，其他变更抛 `MutationUncertainError`。

SDK 定义的异常（包括 `NotebookFailedError`）均从 `InspireError` 派生。配置、认证、冷却、参数错误、未找到、歧义、不完整枚举、传输失败、写入不确定、等待超时分别可捕获。`AmbiguousResourceError.candidates` 返回可选择引用；`AuthenticationCooldownError.retry_at` 是允许再次评估认证的 Unix 时间，不是鼓励盲目重试错误密码。冷却沿用 CLI 的账号级 guard。

`timeout` 控制单次请求预算，`operation_timeout` 控制普通操作的协作式总预算，`wait(timeout=...)` 设置整个等待预算；嵌套调用使用更早截止时间。控制台 Transport 的请求、退避和账号刷新锁等待共享剩余预算；TensorBoard 应用 GET 是上述独立超时路径。同步网络库的底层调用和已有浏览器登录流程不能被强制抢占，显式允许浏览器时登录可能超出总预算；这些参数不是硬实时取消保证。需要硬隔离的编排器应使用独立进程，并在超时后核查任何可能已发送的写请求。

传输策略由调用方声明：普通 `operation` 进入 `Transport.scope(timeout=...)`，按 READ 处理。READ 对 requests 异常、HTTP 429/5xx、共享 `_is_transient_v2_error_code` 判定的 v2 暂时错误最多尝试三次，退避与请求共用截止时间。HTTP 401/3xx 无论 allow_browser 设置均允许一次上述会话刷新，重试仍失效则抛 AuthenticationError；requests 层失败也只有显式允许浏览器才可换通道。

SDK 中真正写入的 JSON browser_api 调用必须包在 `transport.single_send(operation_id, create=True)` 或 `transport.single_send()` 中。一个 block 最多允许一次 request，第二次调用抛 RuntimeError；create 参数显式区分创建与其他变更。发送后不刷新、不重试、不换通道；明确拒绝映射为 ValidationError，明确限流／拒绝执行映射为可重试的 TransportError，HTTP 403 保留 AuthenticationError，仅未知结果映射为 SubmissionUncertainError / MutationUncertainError。Transport 不按 URL、Action 或 HTTP 动词猜测幂等性。`request()` 不自动解包 v2 信封，解析后的 JSON 交给 browser_api；只读路径会先识别其中的暂时错误。`plaza_request()` 则使用广场专用的信封解包器返回 data。READ 的一般 HTTP 4xx 返回包含状态码和最多约 500 字符正文的 ValidationError，业务错误保留原消息。

工作负载 wait 的 raise_on_failure=True 抛对应 SDK 失败异常，携带最终资源快照：Job/HPC/Ray 使用 `.job`，Notebook 使用 `.notebook`，Serving 使用 `.serving`，TensorBoard 使用 `.tensorboard`。Job/HPC/Ray 等待终态；Notebook、Serving、TensorBoard 等待目标状态，具体默认目标见方法表。超时统一抛 WaitTimeoutError。

## 维护接口

维护接口时，在 `cli/` 运行 `uv run python scripts/generate_sdk_async.py` 更新已签入的显式包装方法。`tests/test_sdk_async.py` 比较所有实例 facade、方法签名和返回类型，并检查生成文件完全一致；新增同步方法未生成异步版本会导致测试失败，mypy 可直接检查真实签名。

## CLI-only 范围

- 交互初始化提示、Playwright 安装、ssh-keygen，以及 `config *`、`update`、`uninstall`、CLI 的磁盘资源缓存命令 `cache *`（SDK 的 `client.cache` 是独立的进程内目录缓存）；非交互账号管理和初始化由 `Accounts`、`client.login()`、`client.init()` 提供。
- `api-key export` 的文件格式、权限和 stdout 渲染，以及 `api-key run` 的子进程和环境处理；平台密钥读写由 `client.api_keys` 提供。
- 所有工作负载的 JSON/TOML `batch`；SDK 应用自行循环或编排。
- Notebook 的 exec 由 SDK 提供；`ssh/shell/scp/ssh-config/ssh-proxy/connection */install-deps/proxy-url` 仍为 CLI-only，创建后的 `--post-start/--post-start-script` 及 `job/hpc/ray/serving shell` 也仅保留在 CLI。
- 日志 SSH 文件来源选项 `--path/--remote-log-path/--notebook/--source`，及终端专用格式、字符展示预算；SDK 使用平台日志来源并返回结构化记录。
- 指标 `--plot/--open/--sparkline` 和 TensorBoard 终端趋势渲染；SDK 返回样本或标量摘要。
- `serving api --format` 的 shell 格式输出；SDK 返回共享 access 核心的结构化 endpoint / invocation 信息。
