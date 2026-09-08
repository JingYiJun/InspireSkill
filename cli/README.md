# InspireSkill CLI

`inspire-skill` is the installable command-line interface for the Inspire
compute platform. The package exposes the `inspire` executable.

```bash
python -m pip install inspire-skill
inspire --help
```

The public CLI resolves resources by name. Human-readable output and the root
`--json` mode expose names, aliases, readable state, and bounded collection
metadata rather than implementation metadata. Use `--limit/-n` or `--all`
where offered, and consult the installed command help for the current syntax.

`inspire account use <name>` sets the saved default account. Every command
accepts `--account <name>` to override it for that invocation, including at the
root, command-group, or subcommand position. Commands keep their selected
account throughout execution. Switching the default preserves each account's
sessions, SSH connections, and resource caches.

Project documentation:

- [Project overview](https://github.com/realZillionX/InspireSkill/blob/main/README.md)
- [Capability overview](https://github.com/realZillionX/InspireSkill/blob/main/README.md#能力一览)
- [Agent Skill](https://github.com/realZillionX/InspireSkill/blob/main/SKILL.md)
- [Usage references](https://github.com/realZillionX/InspireSkill/tree/main/references)
- [Development guide](https://github.com/realZillionX/InspireSkill/blob/main/CONTRIBUTING.md)

## Python SDK

实验性同步入口 `from inspire import InspireClient` 覆盖全部平台侧 CLI 命令组，复用同一包的账号与共享服务。Client 提供 `workspaces`、`projects`、`compute_groups`、`images`、`datasets`、`models`、`resources`、`account_info`、`api_keys`、`jobs`、`notebooks`、`hpc`、`ray`、`servings`、`tensorboards`。本地配置、连接工具和终端渲染仍由 CLI 提供；参见 [Python SDK 指南](../references/sdk.md)。

Python SDK 支持 `Accounts` 本地账号管理、凭据构造 `InspireClient`、无浏览器 `login()` 与非交互 `init()`；详见 [SDK 文档](../references/sdk.md)。
