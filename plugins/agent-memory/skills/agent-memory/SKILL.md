---
name: agent-memory
description: 读取和更新 pi、Codex、Claude Code 共用的本地记忆。用户要回忆之前的决定、坑、配置，或要求把新事实记下来时使用。
---

# agent-memory

三家共用一份本地记忆，数据在 `~/.agent-memory/`，不在项目仓库里。

- 跨项目记忆：`~/.agent-memory/global/`
- 项目记忆：`~/.agent-memory/projects/<工作区>/`
- 工作区清单：`~/.agent-memory/config.json`

索引已经渲染进 `~/.pi/agent/AGENTS.md` 和 `~/.codex/AGENTS.md`。Claude 通过全局 `CLAUDE.md` 引用 pi 的那份，不要再写一遍。

## 什么时候用

- 用户问以前定过什么、某个环境怎么配、踩过什么坑。
- 用户明确说「记一下」或结论以后还会用到。

先读对应目录的 `MEMORY.md`，再按索引打开具体文件。不要把整库读进上下文。

## 写一条记忆

在对应层新建一个 `.md`，开头用 frontmatter：

```yaml
---
name: 短名
description: 一句话
metadata:
  type: project
---
```

然后在同层 `MEMORY.md` 加一行 `- [标题](文件名.md) — 一句话`。不要手改 `<!-- agent-memory:... -->` 标记区。

写完运行：

```bash
python3 ~/.agent-memory/bin/agent-memory sync -q
```

## 不要做的事

- 不要把记忆文件写进 git 仓库或公司项目目录。
- 不要把 `~/.agent-memory/` 提交到 GitHub，里面有本机账密。
- 登记多个工作区后，每个工作区的索引都会进入每次会话。新增工作区前先看 README 的已知限制。
