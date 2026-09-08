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

默认 `allow_browser=False`，不自动登录或启动 Chromium。缓存失效返回 `AuthenticationError`，应用可以提示用户通过 CLI 恢复认证。需要由控制节点自动登录/浏览器传输时显式指定 `allow_browser=True`；仍需按现有 CLI 安装说明准备 Chromium。SDK 导入与 Job 模块导入不会加载 Playwright 或 Click。

一个同步 Client 只允许在创建它的进程和线程中使用。线程 worker 或 fork 子进程必须各自创建 Client，并用 `with` 或 `close()` 释放资源。不同 Client 的 HTTP session、浏览器和关闭动作相互独立；账号磁盘缓存、刷新锁和登录冷却仍与 CLI 共用。同账号多个进程需在本地文件锁有效的文件系统上运行；未验证任意网络共享盘锁语义。

## 发现资源与确定性选择

| 服务 | 接口 | 约束 |
|---|---|---|
| `workspaces` | `list/get` | 完整名称匹配 |
| `projects` | `list/get` | 必须指定 workspace |
| `compute_groups` | `list/get` | 必须指定 workspace |
| `images` | `list/get` | official/public/project/private；跨来源同名报歧义 |
| `jobs` | `quotas` | 指定 workspace、group；返回可选规格与引用 |
| `jobs` | `list/iter/get` | list 默认且只支持 owner=self；名称 get 完整消歧 |

`list()` 返回 `Page(items, next_cursor, total)`；通过 `cursor=page.next_cursor` 继续，`total=None` 表示该查询没有可靠总数。任务列表按原始页按需读取；本地状态过滤可能扫描多页。名称解析需要扫描所有匹配任务，超过 100 页或平台返回重复/缺失页时返回 `ResolutionIncompleteError`，不会根据不完整结果挑选第一个。

其他资源目录先完整枚举再切出有界返回页。游标绑定账号、服务端、服务和筛选条件。它是当前列表的偏移标记，不是服务端快照；任务并发新增/删除时可能重复或遗漏。`jobs.iter(max_items=...)` 去重已经看到的引用，但不承诺快照完整性。

使用 `.ref` 在后续操作中保持身份。引用可 `to_dict()` / `JobRef.from_dict()` 序列化；包含内部资源身份但不包含认证材料，默认 repr 隐藏 key。引用不授予权限，也不绕过服务器校验。跨账号、跨来源、跨工作区或资源类型不匹配会报错。公开选择器不接受裸 ID。

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
    print(plan.summary)  # 不含 command/env，不预留资源。
    try:
        handle = client.jobs.create(spec)
    except SubmissionUncertainError as exc:
        print("提交结果待核查，诊断号：", exc.operation_id)
        raise  # 查询候选任务并核查；不要在这里自动再次 create。
    result = client.jobs.wait(handle.ref, timeout=3600, raise_on_failure=True)
    print(result.status)
```

`plan()` 执行实时只读资源解析，检查项目、镜像、训练计算组能力、精确规格和优先级，复用 CLI 的训练 payload 构建服务。`create()` 会重新规划，避免把预检当作预约；它只发送一次创建请求，并直接返回 `JobHandle`，不以提交后的详情查询决定创建是否成功。

`operation_id` 可由调用方传入 UUID，仅用于关联诊断，不是平台幂等键，未写入平台。它不支持自动按诊断号找到任务。创建后响应丢失、JSON 损坏、会话失效、暂时错误或缺少 ID 均可能返回 `SubmissionUncertainError`。明确业务/HTTP 参数拒绝返回 `ValidationError`。读请求最多三次总尝试；写请求不进行认证重放、浏览器换通道重放或暂时错误重试。

`wait()` 首次解析后固定 JobRef，使用共享状态词表；UNKNOWN 不视为成功。超时仅结束等待，不停止或删除远端任务。默认返回任何终态，`raise_on_failure=True` 在失败/取消时抛出 `JobFailedError`。`jobs.stop(ref)` 显式停止；`jobs.delete(ref)` 先确认终态，运行中的任务拒绝删除。终态检查与删除不是平台原子事务。

## 日志和事件

```python
logs = client.jobs.logs(handle.ref, window="1h", instances="all", limit=100, max_chars=16000)
print(logs.text)
print(logs.truncated, logs.total)
events = client.jobs.events(handle.ref, limit=20)
```

首版提供**有界日志样本**，不提供 `tail=N`、无损游标或 follow。`limit` 限制所有实例合计返回的记录条数，`max_chars` 限制格式化后的总字符数；两者任一截断都会标记 `truncated=True`。样本内按时间、实例和记录 ID 排序，不把样本排序宣称为全局最近 N 条。

`instances="all"` 先完整发现实例，也可传已发现的实例名称列表。`window` 支持 `1h` / `24h`；终态任务优先使用结束时间作窗口终点。显式起止时间须为带时区 datetime，且指定 `window=None`，窗口最长 24 小时。日志内容可能包含训练程序自己的敏感输出，由调用方决定是否记录或分享，SDK 不主动打印。

本机只读探针在同一任务同一时间窗请求 5/10 条，平台都报告 total=10，5 条样本是 10 条样本的前部；已验证实例发现、日志时间窗和有界读取，尚未建立“全局最后 N 条”、多实例稳定排序或日志保留期限合同。因此 `tail` 保持不支持。

## 错误与时间预算

公共错误均从 `InspireError` 派生：配置、认证、冷却、参数错误、未找到、歧义、不完整枚举、传输失败、写入不确定、等待超时分别可捕获。`AmbiguousResourceError.candidates` 返回可选择引用；`AuthenticationCooldownError.retry_at` 是允许再次评估认证的 Unix 时间，不是鼓励盲目重试错误密码。冷却沿用 CLI 的账号级 guard。

`timeout` 控制单次请求预算，`operation_timeout` 控制普通操作的协作式总预算，`wait(timeout=...)` 设置整个等待预算；嵌套调用使用更早截止时间。请求、退避和刷新锁等待共享剩余预算。同步网络库的底层调用和已有浏览器登录流程不能被强制抢占，显式允许浏览器时登录可能超出总预算；这些参数不是硬实时取消保证。需要硬隔离的编排器应使用独立进程，并在超时后核查任何可能已发送的写请求。

## 当前验证边界

回归覆盖 CLI/SDK payload 等价、跨账号与线程隔离、单次创建、envelope 重试、缺失确认、分页、同名消歧、镜像来源、日志窗口、状态等待与显式清理。真实平台只做工作区、任务和日志读取，未提交收费任务或执行 stop/delete。GPU 容器、CPU 节点及网络共享盘内调用尚未实测。SDK 为实验性新增入口，发布前仍需要专门的真实创建闭环验收。

维护者参见 [迁移审计与测试清单](sdk-migration-audit.md)。
