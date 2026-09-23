# agent-memory

pi、Codex、Claude Code 共用一份本地记忆。仓库里只有代码，记忆在 `~/.agent-memory/`，不进 Git，也不写进项目目录。

## 安装

```bash
git clone https://github.com/<you>/agent-memory.git ~/code/agent-memory
python3 ~/code/agent-memory/scripts/agent_memory.py init --workspace <项目目录>
```

`init` 默认走软链通道：

- `~/.agents/skills/agent-memory`：pi 和 Codex 共用
- `~/.claude/skills/agent-memory`：Claude Code

两条都指向 `plugins/agent-memory/skills/agent-memory`。不会写 `~/.codex/skills`（那是旧路径，会和上面重复）。

换机器时单独拷贝 `~/.agent-memory/`，再跑一次 `init`。

## 原生插件通道

三家也可以按各自的插件机制安装同一份代码。装过插件的那一端，`init` 不再软链，避免同名技能加载两次。`status` 会打印当前通道。

前提是上面的 clone 和 `init` 已经跑过。插件目录里没有脚本，`init` 才会建 `~/.agent-memory/bin/agent-memory`。没跑过 `init` 就装插件，技能里的命令会找不到。

```bash
pi install git:github.com/<you>/agent-memory
claude plugin marketplace add ~/code/agent-memory
claude plugin install agent-memory@agent-memory
codex plugin marketplace add ~/code/agent-memory
codex plugin add agent-memory@agent-memory
```

本机现在的 `pi install` 记的是相对路径 `../../code/agent-memory`，换机器会断。推到 GitHub 之后用上面的 `git:github.com/...` 形式。

插件通道装的是缓存副本，不是仓库里的实时文件。改了 `SKILL.md` 或脚本之后，各端仍然跑旧缓存，直到重新装上。

当前这种本地目录 marketplace，版本号不变时 `claude plugin update` 会回 `already at the latest version` 且不拷文件，`codex plugin marketplace upgrade` 会报 `marketplace is not configured as a Git marketplace`。这时要卸载重装：

```bash
claude plugin uninstall agent-memory@agent-memory && claude plugin install agent-memory@agent-memory --yes
codex plugin remove agent-memory@agent-memory && codex plugin add agent-memory@agent-memory
```

发布到 GitHub 之后，用 `claude plugin marketplace add kettybluce/agent-memory` 和 `codex plugin marketplace add kettybluce/agent-memory` 装成 Git marketplace，那时 `claude plugin update` 和 `codex plugin marketplace upgrade` 才会拉新版本。也可以给 `plugin.json` 的 `version` 升一级，再走更新。

`claude plugin enable` 会重写 `~/.claude/settings.json`，可能把 SessionStart hook 冲掉。改完设置后确认 `hooks.SessionStart` 还在（命令指向 `<仓库路径>/scripts/agent_memory.py sync -q`）。

## 启动时同步

`~/.bashrc` 里的包装函数会在终端执行 `pi`、`codex`、`claude` 前跑 `sync -q`。Claude 另外有 SessionStart hook。

Codex 桌面端和 VS Code 扩展不经过这个 shell，不会自动同步。那些入口里需要记新内容时，手动跑：

```bash
python3 ~/.agent-memory/bin/agent-memory sync
```

## 已知限制

每登记一个工作区，该工作区的项目索引都会写进 `~/.pi/agent/AGENTS.md` 和 `~/.codex/AGENTS.md`，之后所有会话全量加载。工作区一多，上下文会线性变大。当前没有上限截断。新增工作区前先确认这个代价。Claude 不再单独渲染，它通过全局 `CLAUDE.md` 引用 pi 的那份。
