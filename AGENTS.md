# AGENTS.md

本仓库是个人 agent skills 的源码仓库（GitHub `mmdsnb/skills`），面向 qoder CLI、opencode 等 agent 工具。

本文件只约束「怎么判断、开发、维护 skill」。服务器运维、业务项目各有自己的 AGENTS.md，不写进这里。

## 判据：值不值得做

- 先指出它拿掉哪一项重复成本：上下文 / token、人的注意力与心智负担、跨会话记忆、每个项目重复交代的规范、人工回归、理解陌生项目的时间。说不出具体成本就不做。
- 先找现成工具或 skill。工具能解决就不写 skill：repo-brief 的教训是冲着「做个 skill」去，最后真正有价值的是 archify 这个工具。
- 只写 AI 不知道的：这个项目的具体约定、踩过的坑、工具的真实行为。AI 已经会的（Playwright 怎么用、Markdown 怎么写）不重述，否则 skill 会变成教程。
- 验收用真实任务，不用玩具例子。看产出是否被实际拿去用、是否被用户纠正，而不是「效果不错」。内容型 skill 验收结论是否成立、证据是否够，不是页面好不好看；规范类 skill 看第二个项目能否直接套用。
- 失败信号：用户只夸效果没检验内容；SKILL.md 越写越长像教程；同一条约定需要在多处重复。

## 事实源

- 源码只在当前仓库修改；`~/.agents/skills/<name>` 是安装副本，改那里等于改机器，不回流。
- 安装 / 更新：`npx skills@latest add mmdsnb/skills --skill=<name> -g -y`
- 卸载：`npx skills remove <name> -g`
- 新增、删除、改名 skill 时同步根目录 README 的表格；README 是唯一清单，不另建索引。

## skill 的形状

```
<name>/
├── SKILL.md        # 必需
├── references/     # 长文、后端配置、清单，按需加载
├── scripts/        # 可执行脚本
├── tests/          # 带脚本的 skill 必须有
└── assets/         # 产出用模板
```

- 不建 README、CHANGELOG、示例目录；skill 目录里只放 agent 真正要用的东西。
- 目录名 = frontmatter `name`，小写连字符。
- SKILL.md 只写行为指令；细节下沉 references/，并在 SKILL.md 指过去。

## description 是唯一触发器

- 它常驻上下文，决定「是否加载」，不是内容摘要。
- 写清三件事：做什么、什么场景用、**不适用什么**。不适用条款用来切开与相邻 skill 的触发重叠（参考 outsource-agent、voter 的写法）。
- 单行，中文；用户用中文下指令，description 也按中文匹配习惯写。
- 改完自测：用一句真实用户口吻的请求问自己——会命中本 skill 吗？会误命中哪个？

## skill 是触发器，不是常驻规则

需要「每次都生效」的行为，写进使用方项目的 AGENTS.md 或全局规则，不要指望 skill 被加载。skill 只在命中 description 后生效。

## 可移植性

- skill 在多台机器上用（Linux / Windows），路径只写稳定位置（如 `~/obsidian-second-brain`），不写 `~/work/...`、用户名或本机路径。
- SKILL.md 里的脚本路径用 `<skill_dir>` 占位，由调用方替换。
- 会写文件的 skill 要处理 Windows：文件名禁用 `/ \ : * ? " < > |`；行尾用 `.gitattributes` 固定 LF；中文路径要能正常显示。

## 脚本

- Python 单文件用 PEP 723 内联依赖、`uv run --script` 运行；不建本地 `.venv`；纯标准库可直接 `python3`。
- stdout 给 agent 读：默认输出 `{"ok": ...}` JSON；错误信息短且可行动，不打印 traceback。
- 退出码：`0` 成功，`2` 参数错，`1` 运行错 / 超时。
- 凭据只从环境变量读；不写入仓库、不回显；错误输出脱敏；写外部系统前检查内容里是否夹带 key。
- 参数与边界在入口校验（数量上限、超时、路径存在），不替调用方猜。

## 验证

- 先验证技术拼图（最小脚本或请求）再接进 skill；SKILL.md 里每条硬事实都要实测过。
- 用真实任务跑；把踩到的坑按「现象 / 原因 / 规避」写回 SKILL.md，不要只留在对话里。
- 带脚本的 skill 维护 `tests/`（unittest，假后端或本地服务，不连外网），改脚本后跑：

```sh
cd ~/work/skills && python3 -m unittest discover -s <name>/tests -v
```

## 单一事实源

同一个约定只写一处。skill 与它作用的仓库（AGENTS.md / README）冲突时，先确定谁优先，另一处改成指针。

教训：obsidian-memory 的结构与格式一度同时写在 skill、vault README、静态索引三处，随后漂移。同一约定只能有一个权威位置，其余位置写指针。

## 语言与风格

- 中文文档，技术术语保留英文；祈使句；不写「强大、灵活、高效」这类词。
- SKILL.md 控制在几百行内，超出就下沉 references/；引用大文件时给出检索方式（如 rg 关键词）。

## Git

- 不擅自 commit / push；提交前先看 `git status` 与 `git diff`。
- 一个逻辑改动一个提交，用中文 conventional：`docs(<skill>): …`、`feat(<skill>): …`、`fix(<skill>): …`。
- 未验证的实验目录保持未跟踪，不混进提交。
- 不提交凭据；不 force push；不动历史。

## 当前 skill

| 目录 | 职责 |
|---|---|
| `obsidian-memory` | 长期记忆：检索 Obsidian vault，沉淀后自动 commit/push |
| `agents-md-reflect` | 从历史会话提炼真实 AGENTS.md，对比现状给差异与证据 |
| `outsource-agent` | 目录 / 任务外包给便宜模型，干净上下文、只读、结果直返 |
| `voter` | 局域网人工复核：单题或批量投票，结果 JSON |
| `web-e2e` | Web E2E 规范：Playwright + browserless CDP，tests/ 组织约定 |
