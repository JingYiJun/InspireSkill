# Serving API 调用入口调研与补全方案

调研日期：2026-09-07。源码基线：上游 `main`，`fc7c679`（v7.1.7）。
开发分支：`codex/serving-api-access`。

第 1–7 节保留实施前的调查与设计依据；第 8 节记录用户确认方案后的实现和验收。当前命令以 worktree 的 CLI Help 为准。

## 1. 核心结论

当前缺口确实存在，但需要分别补全三个层面：

1. **Serving Endpoint 输出**：平台已经在 `ListServings` 行和 `GetServing` 详情的 `extra_info.service` 中提供地址，当前 CLI 的公开输出投影未保留它。
2. **账号 API Key 管理**：控制台通过 `user` 路由管理密钥，与 Serving 创建请求分离。当前 CLI 没有封装网页使用的四个 Action。
3. **调用说明与示例**：网页把密钥放入调用者环境变量 `INF_API_KEY`，使用 `Authorization: Bearer $INF_API_KEY`；`x-inspire-inference-key` 是请求的 Hash Key，用于相同值请求的节点亲和性。

因此，不能把 `x-inspire-inference-key` 当成 API Key，不能因 CLI 缺少 `--api-key` 就直接向 `CreateServingConsole` 增加猜测字段，也不能把所有 CUSTOM 服务标记成 OpenAI 兼容服务。

## 2. 证据来源与取样边界

从已认证的正常 CLI Session 获取当前控制台页面，递归读取其引用的 JavaScript 资源。没有读取或展示本地账号配置、Cookie 文件或密钥明文。浏览器工具的独立浏览器最初未登录，因此本次使用 Session 客户端获取前端产物，未声称已通过浏览器点击完成操作。

关键线上来源：

| 来源 | 支撑的事实 |
| --- | --- |
| [Serving 详情前端](https://qz.sii.edu.cn/assets/index.ZKONY0we.js) | API 调用页、`extra_info.service`、环境变量、Bearer 认证、Hash Header、CUSTOM 与非 CUSTOM 示例区别 |
| [Serving 详情请求封装](https://qz.sii.edu.cn/assets/inferenceDetailService.C5la48ou.js) | `GetServing`、`GetMyAPIList`、`GetAPIKeyPlaintext` 请求合同 |
| [用户中心前端](https://qz.sii.edu.cn/assets/index.CKdOFnXR.js) | `GenerateAPIKey`、`DeleteAPIKey`、名称输入及显式查看密钥的交互 |
| [Serving 请求封装](https://qz.sii.edu.cn/assets/inferenceService.B8REVnff.js) | 创建使用 `CreateServingConsole`，与账号密钥管理分离 |
| [Serving 创建表单](https://qz.sii.edu.cn/assets/index.C9cSetmj.js) | 域名前缀和资源配置，需在实施时继续逐项对照提交数据 |

资源名是当前构建的内容指纹，后续平台更新可能使链接失效。关键资源 SHA-256：

```text
index.ZKONY0we.js
d4bcf3abc07126a70cc210966188e75fa96ca2ffb49532222d50306aa04644cf
inferenceDetailService.C5la48ou.js
c5eabc4261584db73f73972ac2773a0beb1489c440b713602193b91a1be4dc48
index.CKdOFnXR.js
c8f0c58e5db7ae3ac6cb0790faf67808a9569552553c4d0532ae3d9f3db7fc97
inferenceService.B8REVnff.js
e48de1a7d6e464a4a1a872125677c69d72b9cd2fc296b0cba47a852413673208
```

只读实测：

- 最新 worktree 的 CLI 可以正常查询 Serving。
- 取样的 STOPPED 服务在列表与详情中均有 `extra_info.service`；另取 RUNNING 服务，详情同样有该字段。
- 取样地址为 HTTPS，未带 userinfo、query 或 fragment。这里只证明样本形状，不能推广为平台永久保证。
- `GetMyAPIList` 成功，解包后为 `{items: [...]}`；条目的键为 `created_at`、`key_id`、`name`、`value`。调查只输出键名，没有输出 `value` 或任何密钥标识符。
- 未调用读取密钥明文、创建、删除接口；未发起推理请求；未创建、重启、停止或修改任何 Serving。

## 3. 网页接口合同

### 3.1 Endpoint

```text
POST /api/v2/inference_serving?Action=GetServing
body: {inference_serving_id: <内部句柄>}
Result.extra_info.service -> API 调用页 apiLink
```

`ListServings` 的每行也带同名字段，因此为列表增加 Endpoint 不需要逐条追加详情请求。

这个字段由平台决定。不得根据服务名称、域名前缀、容器端口或当前域名后缀自行拼接；也不能用 `GetInferenceServingTerms` 替代，它表示运行时段。

服务处于 STOPPED 时地址仍可能存在：地址存在不等于后端已就绪，CLI 应同时展示状态。

### 3.2 API Key

四个 Action 都是 `POST /api/v2/user?Action=...`，请求依赖正常控制台 Session，响应沿用公共 `_v2_result()` 信封解包。

| Action | 网页请求体 | 网页消费者 / 验证程度 |
| --- | --- | --- |
| `GetMyAPIList` | 无业务字段；只读实测使用 `{}` | `Result.items`；前端使用 `name`、`key_id`、`value`、`created_at`；已实测结构 |
| `GetAPIKeyPlaintext` | `{api_key_id}` | 前端读取 `Result.value`；仅前端证据，未取真实明文 |
| `GenerateAPIKey` | `{key_name}` | 成功后刷新列表；仅前端证据，尚未验证创建返回结构与 round-trip |
| `DeleteAPIKey` | `{api_key_id}` | 成功后刷新列表；仅前端证据，尚未做受控生命周期验证 |

网页只在用户显式查看或选择时调用明文接口。即使列表的 `value` 通常用于掩码展示，CLI 也不应相信后端永远返回掩码：默认只投影名字和时间，丢弃 `value`。

用户中心创建表单限制名字非空、最多 256 字符，提示允许字母、数字、下划线、短横线、小数点。具体正则和服务端错误语义在实现时进一步核对。

这些请求没有携带 Serving ID 或 Workspace ID，支持把 CLI 入口放在账号管理组。它们没有提供本次所见的逐 Serving 绑定或权限范围字段；密钥的完整跨服务权限边界仍需另行核验，不能推断为每个服务专属密钥。

### 3.3 鉴权、亲和性、OpenAI 示例

| 名称 | 语义 | 应出现的位置 |
| --- | --- | --- |
| `INF_API_KEY` | 网页示例中调用者保存平台 API Key 的环境变量名 | 客户端 shell / 示例代码，不自动注入 Serving 容器 |
| `Authorization: Bearer ...` | 网页示例的推理请求鉴权 | 请求 Header |
| `x-inspire-inference-key` | 网页声明的 Hash Key；相同值请求路由到同一节点 | 可选请求 Header，示例可使用业务会话标识 |
| `extra_info.service` | 平台返回的服务调用地址 | Endpoint 输出 |

网页 CUSTOM 示例直接使用服务地址，不附加固定业务路径；具体路由、方法、请求体由自定义服务决定。非 CUSTOM 示例展示 `${service}/v1` 的 OpenAI SDK base URL 和 chat completions；组件内还存在 completions 分支，但本次所见调用处将 chat template 设为真，不能把该分支描述为当前 UI 已能自动识别模型能力。

Hash Key 的实际负载均衡算法、扩缩容后的映射稳定性，以及故障切换行为尚未通过多副本推理请求验证；手册应限定为平台当前声明的亲和性语义。

## 4. CLI 缺口与代码落点

| 位置 | 当前行为 | 修改方向 |
| --- | --- | --- |
| `cli/inspire/platform/web/browser_api/servings.py` | `ServingInfo` 有 raw 数据，但没有显式 Endpoint；现有 wrapper 已取得所需数据 | 提取平台返回的 Endpoint；避免增加 N+1 请求 |
| `cli/inspire/cli/commands/serving/public_output.py` | `public_serving()` 使用固定字段表；列表再做一次字段投影 | 明确允许 Endpoint 和认证说明，保留其他字段的清洗边界 |
| `cli/inspire/cli/commands/serving/serving_commands.py` | list/status/create 都没有调用说明 | 为查询输出补字段；创建成功后查询详情或明确提示稍后查询 |
| `cli/inspire/cli/commands/serving/__init__.py` | 无 API 调用指引入口 | 注册一个专用调用信息命令，避免把长示例放入常规列表 |
| `cli/inspire/platform/web/browser_api/users.py` | 目前只封装权限查询 | 增加聚焦 API Key 的 wrapper 模块并按现有模式导出 |
| `cli/inspire/cli/commands/account/` | 缺少 API Key 生命周期入口 | 增加按名字管理的命令组，复用全局账号选择 |
| `references/compute-workloads.md` | 未解释相关变量和请求头 | 补调用闭环，区分 CUSTOM 和 OpenAI 示例 |
| `references/dev/browser-api.md` | 缺少上述密钥 Action 和 Endpoint 输出合同 | 仅写入已验证合同，修订明文与 Endpoint 的输出边界 |

## 5. 建议的命令设计

以下是建议，尚未实现：

```text
inspire serving status NAME --workspace WORKSPACE
inspire --json serving list --workspace WORKSPACE
inspire serving api NAME --workspace WORKSPACE
inspire serving api NAME --workspace WORKSPACE --format curl

inspire account api-key list
inspire account api-key create --name NAME
inspire account api-key export NAME --output PATH
inspire account api-key delete NAME
```

### Endpoint 和调用指引

- status 展示 Endpoint；JSON 列表保留 `endpoint`；文本列表采用紧凑展示或显式列选项，实施时按现有格式决定。
- `serving api` 返回服务类型、Endpoint、鉴权 Header、环境变量名、可选亲和性 Header 和使用示例。
- CUSTOM 默认输出普通 HTTP 调用提示；如用户明确要求 OpenAI 示例，应说明这依赖自定义容器自身提供兼容路由。
- 示例只引用 `$INF_API_KEY`，不代替用户获取并内嵌真实密钥。
- 对平台返回 URL 做专门校验：只允许 HTTP(S)，拒绝嵌入用户名密码；敏感 query、fragment 和带凭证路径应有明确策略。仅为验证过的 Endpoint 字段保留完整 URL，不绕开整个对象的清洗。
- 创建响应未必立即带地址。成功后的只读查询失败时，要明确区分“Serving 已创建”和“暂时未取得 Endpoint”，避免误导用户重复创建。
- `--dry-run` 不伪造未来 Endpoint，不创建或导出密钥。现有 `--custom-domain` 继续只表示平台域名前缀。

### API Key 管理

- 默认 list/create/delete 输出只包含公开元数据，不能打印明文或依赖列表掩码。
- Name resolver 只在内部使用 `key_id`；若存在重名，应给可读候选并使用既有 `--pick` 约定，不能猜测名字唯一。
- 明文建议通过显式 export 写到用户指定文件，权限 `0600`，默认拒绝覆盖已有文件；终端、JSON、日志和错误都只返回成功元数据。
- 如未来确实需要 stdout，应作为单独且显式的输出选择，不能让普通 `--json` 隐式开启明文输出。
- 删除需采用仓库既有的确认与非交互约定；不能在调研中删除用户现有密钥。
- 账号选择、Session 续期、错误退出码和列表预算沿用现有实现，不维护新的认证配置。

不建议在 `serving create` 中增加无平台契约支撑的 `--openapi`、`--api-key`、`--endpoint` 请求字段。实际功能补全是拿到平台 Endpoint、管理账号密钥并生成正确调用说明。

## 6. 实施与验收顺序

1. 先完成 Endpoint 的 typed projection、list/status 和调用说明命令，测试 CUSTOM 与非 CUSTOM、地址缺失、停止状态、URL 校验、纯 JSON 输出和无 N+1 请求。
2. 封装 Key 列表及按名字解析，测试额外字段不会穿透、列表 `value` 即使是完整明文也不输出、空结果与接口失败可区分、账号隔离和重名处理。
3. 实现显式导出和创建/删除合同，测试文件权限、已存在路径、错误路径、密钥不进日志；创建结果不能假定含明文。
4. 同步 Help、Serving 日常 reference 和 Browser API 开发 reference，移除与新接口冲突的“只能网页操作”或绝对输出禁令。
5. 对新行为跑目标测试、Ruff、mypy；合并或发布前完成仓库全量 pytest、构建与 `git diff --check`。
6. 受控 live 验证只使用新建的临时 Key：创建后列表确认，显式导出但不展示值，验证调用后删除，再确认列表不再包含它。操作现有服务前确认测试请求的业务路由及副作用。
7. 如果要宣称 Hash Header 的运行保证，另做多副本、扩缩容和故障切换测试；不以网页文案替代运行证据。

## 7. 本阶段交付状态

- 已 pull 最新上游并创建独立 worktree。
- 已完成源码缺口定位、当前网页产物追踪、Endpoint 与 API Key 列表只读实测。
- 已形成分层实现方案、测试矩阵与待验证清单。
- 按“先调研、分析”的阶段要求，本阶段仅新增本调查报告；未修改 CLI 实现、未提交或推送、未更新全局安装。
- 未修改任何平台资源或密钥；无本次创建的云端资源需要清理。


## 8. 实现与验收（2026-09-07）

用户确认上述方案后，已在本分支完成：

- `serving list/status/create` 输出经过校验的 Endpoint；列表复用现有响应，不增加逐条详情查询。创建后的 Endpoint 查询失败不会把成功创建误报为失败。
- 新增 `serving api`，支持默认说明、`--format curl`、`--affinity-key`、结构化 JSON，以及既有 Workspace / Name / `--pick` / 账号选择约定。
- 新增 `account api-key list/create/export/delete`；列表丢弃后端 value；创建和删除读回确认；导出通过同目录临时文件与原子硬链接发布到新文件，权限 0600，既有文件和符号链接均不覆盖。
- 同步 SKILL 路由、Serving 日常参考、Browser API 开发参考、Help 和 Unreleased changelog。
- JSON 的例外仅作用于显式指定的已校验 Endpoint、派生 base URL 和生成模板；ID 与敏感字段即使指定 preserve_raw 也不能通过。

验证结果：

| 验证 | 结果 |
| --- | --- |
| 新增回归覆盖 | 41 项，涵盖 URL / Header 注入、CUSTOM 与 OpenAI 类型分支、输出清洗、Key wrapper 合同、私有文件权限、原子发布竞争、重名消歧及创建后读取失败 |
| 全量 pytest | 2886 passed，1 skipped |
| Ruff | 通过 |
| mypy | 206 个源码文件通过 |
| 构建 | sdist 与 wheel 构建通过 |
| diff 检查 | 通过 |
| 临时 Key 生命周期 | 创建 → 列表确认 → 导出 0600 文件 → 删除 → 列表确认消失，通过；密钥值未输出 |
| 真实 Serving 查询 | API 指引、JSON list/status 均含可用格式的 Endpoint |
| 网关请求对照 | 同一地址匿名 HEAD 返回 401，带临时 Bearer Key 和亲和性 Header 返回 404；只证明该取样中的鉴权响应区别，不证明推理成功或亲和性运行保证 |

第一次全量检查发现新增命令的三处 `--pick` Help 未复用公共文案，已修正；镜像列表测试重复出现并发顺序导致的偶发失败，确认实现使用线程池后，仅修正该测试的顺序假设，仍检查四种来源各调用一次；镜像业务逻辑未修改。最终全量通过。

受控验证产生的临时 Key 已删除并再次核验不存在；临时导出文件随验证目录清理。没有创建、停止或修改现有 Serving，没有发送模型推理 POST 请求。

本次完成的是独立 worktree 的实现与验收，未发布版本或替换全局安装。导出同时核验目标文件系统实际落实 0600 权限；Windows 原生环境在获取明文前拒绝导出并提示使用 WSL。

已知限制仍为：Endpoint 仅放行当前验证过的 HTTP(S) origin；CUSTOM 的真实业务路由由容器定义；多副本亲和性、扩缩容映射与故障切换行为尚未验证。
