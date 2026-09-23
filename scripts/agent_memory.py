#!/usr/bin/env python3
"""agent-memory：让 pi / Codex / Claude Code 共享同一份记忆，两层（跨项目 / 项目），开工自动加载。

只用标准库，秒级、离线、确定性（不调模型）。

    agent_memory.py init      首次接线：建两层记忆库、迁移 Claude 记忆并软链、导入 pi(magic-context) 记忆、
                              渲染各入口文件、装 bashrc 包装函数与 Claude SessionStart hook
    agent_memory.py sync      import + harvest + render（每次启动前跑，-q 静默）
    agent_memory.py import    从 pi 的 magic-context 库导入/更新项目记忆（幂等，归档条目自动撤下）
    agent_memory.py harvest   扫三家最近会话 → 项目层 sessions/*.md + MEMORY.md「近期会话」块
    agent_memory.py render    两层索引渲染进各入口文件的标记区
    agent_memory.py status    查看接线状态
    agent_memory.py backup    工具 + 两层记忆库打包到 tmp/backup/（--to 指定目录）

    数据全部在家目录，不写任何项目仓库：
    跨项目层  ~/.agent-memory/global/
    项目层    ~/.agent-memory/projects/<工作区>/
    工作区清单  ~/.agent-memory/config.json（init --workspace 登记）
    加载：索引只渲染进 ~/.pi/agent/AGENTS.md、~/.codex/AGENTS.md、~/.claude/CLAUDE.md
          （三家每次会话必读、对所有目录生效、且都在仓库之外）。
记忆正文不进本仓库；仓库只有工具代码，可直接推到 GitHub。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DATA_ROOT = Path(os.environ.get("AGENT_MEMORY_HOME", HOME / ".agent-memory")).resolve()
CONFIG_PATH = DATA_ROOT / "config.json"
GLOBAL_HUB = Path(os.environ.get("AGENT_MEMORY_GLOBAL_HUB", DATA_ROOT / "global")).resolve()
TOOL_PATH = Path(__file__).resolve()
REPO_ROOT = TOOL_PATH.parent.parent
# 下面四个随 activate() 切换；默认空，避免在没有工作区时写到当前目录
WORKSPACE = Path.cwd()
PROJECT_HUB = DATA_ROOT / "projects" / "_none"
SESSIONS_DIR = PROJECT_HUB / "sessions"
PI_MEM_DIR = PROJECT_HUB / "pi-memories"

PI_SESSIONS = HOME / ".pi" / "agent" / "sessions"
PI_GLOBAL_AGENTS = HOME / ".pi" / "agent" / "AGENTS.md"
MAGIC_DB = HOME / ".local" / "share" / "cortexkit" / "magic-context" / "context.db"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CLAUDE_GLOBAL_MD = HOME / ".claude" / "CLAUDE.md"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_GLOBAL_AGENTS = HOME / ".codex" / "AGENTS.md"

MEM_START, MEM_END = "<!-- agent-memory:start -->", "<!-- agent-memory:end -->"
SESS_START, SESS_END = "<!-- agent-memory:sessions:start -->", "<!-- agent-memory:sessions:end -->"
PIMEM_START, PIMEM_END = "<!-- agent-memory:pi-memories:start -->", "<!-- agent-memory:pi-memories:end -->"
RECENT_LIMIT = 12
PI_INDEX_LIMIT = 60
QUIET = False

# 渲染进 AGENTS.md / CLAUDE.md 前的脱敏（记忆正文留在库里不动）
SECRET_PATTERNS = [
    re.compile(r"(密码|口令|password|passwd|pwd|secret|token|api[-_]?key)(\s*[:=：]\s*|\s+)(\S+)", re.I),
    re.compile(r"\b(ng_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{12,}|[a-f0-9]{40,})\b"),
]


def log(msg: str) -> None:
    if not QUIET:
        print(msg)


# ---------------------------------------------------------------- 通用
def workspace_slug(path: Path) -> str:
    return claude_slug(path).strip("-") or "workspace"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"workspaces": []}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"workspaces": []}
    data.setdefault("workspaces", [])
    return data


def save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def workspaces() -> list[Path]:
    out = []
    for raw in load_config().get("workspaces", []):
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            out.append(path)
    return out


def hub_for(workspace: Path) -> Path:
    override = os.environ.get("AGENT_MEMORY_HUB")
    if override:
        return Path(override).resolve()
    return DATA_ROOT / "projects" / workspace_slug(workspace)


def activate(workspace: Path) -> None:
    """把后续读写切到某个工作区的记忆库。记忆库在家目录，不在工作区里面。"""
    global WORKSPACE, PROJECT_HUB, SESSIONS_DIR, PI_MEM_DIR
    WORKSPACE = workspace.resolve()
    PROJECT_HUB = hub_for(WORKSPACE)
    SESSIONS_DIR = PROJECT_HUB / "sessions"
    PI_MEM_DIR = PROJECT_HUB / "pi-memories"


def register_workspace(path: Path) -> None:
    path = path.expanduser().resolve()
    if not path.is_dir():
        sys.exit(f"工作区不存在: {path}")
    data = load_config()
    items = [str(Path(p).expanduser().resolve()) for p in data.get("workspaces", [])]
    if str(path) not in items:
        items.append(str(path))
    data["workspaces"] = items
    save_config(data)
    log(f"init: 已登记工作区 {path}")


def repos() -> list[Path]:
    if not WORKSPACE.is_dir():
        return []
    return sorted(p for p in WORKSPACE.iterdir() if p.is_dir() and (p / ".git").exists())


def is_tracked(repo: Path, rel: str) -> bool:
    """该路径是否已被 git 跟踪；git 不可用时保守返回 True（宁可不写）。"""
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", rel],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return True
    return r.returncode == 0


def exclude_locally(repo: Path, names: list[str]) -> None:
    """[遗留] 早期版本用它把生成文件加进 .git/info/exclude；现已改为不往仓库写文件，仅用于清理。"""
    exc = repo / ".git" / "info" / "exclude"
    exc.parent.mkdir(parents=True, exist_ok=True)
    text = exc.read_text(encoding="utf-8") if exc.exists() else ""
    lines = text.splitlines()
    add = [f"/{n}" for n in names if f"/{n}" not in lines]
    if add:
        exc.write_text("\n".join(lines + add) + "\n", encoding="utf-8")
        log(f"render: {exc} 已加入 {', '.join(add)}")


def git_flag(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """跑一条 git 命令；失败不抛，返回结果供调用方判断。"""
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def hide_local(repo: Path, rel: str) -> str:
    """让仓库里的记忆文件只留在本地、绝不进提交。
    已跟踪文件用 skip-worktree（exclude 对它无效）；未跟踪文件写进 .git/info/exclude。"""
    if is_tracked(repo, rel):
        r = git_flag(repo, "update-index", "--skip-worktree", rel)
        return "skip-worktree" if r.returncode == 0 else f"skip-worktree 失败: {r.stderr.strip()}"
    exc = repo / ".git" / "info" / "exclude"
    try:
        exc.parent.mkdir(parents=True, exist_ok=True)
        lines = exc.read_text(encoding="utf-8").splitlines() if exc.exists() else []
        if f"/{rel}" not in lines:
            exc.write_text("\n".join(lines + [f"/{rel}"]) + "\n", encoding="utf-8")
    except OSError as e:
        return f"exclude 写入失败: {e}"
    return "exclude"


def skip_worktree(repo: Path, rel: str) -> bool:
    r = git_flag(repo, "ls-files", "-v", rel)
    return r.returncode == 0 and any(line[:1] in "S" for line in r.stdout.splitlines())


def unrender_repos() -> None:
    """撤回早期版本写进各子仓的记忆块，恢复仓库原样。
    记忆已改走三家全局入口，仓库不再承载。被跟踪的文件若曾设过 skip-worktree，一并解除。"""
    for repo in repos():
        f = repo / "AGENTS.md"
        if not f.exists():
            continue
        if skip_worktree(repo, "AGENTS.md"):
            git_flag(repo, "update-index", "--no-skip-worktree", "AGENTS.md")
            log(f"render: 已解除 {f} 的 skip-worktree")
        text = f.read_text(encoding="utf-8", errors="replace")
        if MEM_START not in text:
            continue
        if is_tracked(repo, "AGENTS.md"):
            git_flag(repo, "checkout", "--", "AGENTS.md")
            log(f"render: 已用 git checkout 还原 {f}")
            continue
        stripped = re.sub(re.escape(MEM_START) + r".*?" + re.escape(MEM_END) + r"\n?",
                          "", text, flags=re.S).strip()
        try:
            if stripped in (f"# AGENTS.md — {repo.name}", ""):
                f.unlink()
                log(f"render: 已删除工具生成的 {f}")
            else:
                f.write_text(stripped + "\n", encoding="utf-8")
                log(f"render: 已从 {f} 剔除记忆标记区（保留原有内容）")
        except OSError as e:
            log(f"render: 清理 {f} 失败：{e}")


def claude_slug(path: Path) -> str:
    return str(path).replace("/", "-")


def parse_ts(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            n = float(value)
            if n > 1e11:
                n /= 1000
            return dt.datetime.fromtimestamp(n, dt.timezone.utc)
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None


def fmt_local(t: dt.datetime | None) -> str:
    return t.astimezone().strftime("%Y-%m-%d %H:%M") if t else ""


def one_line(text: str, limit: int) -> str:
    text = re.sub(r"</?pasted_content[^>]*>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact(text: str) -> str:
    # 自用工具、局域网信息，默认不脱敏；置 AGENT_MEMORY_REDACT=1 打开
    if os.environ.get("AGENT_MEMORY_REDACT", "0") != "1":
        return text
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 3 else "***", text)
    return text


def under_workspace(cwd: str | None) -> bool:
    if not cwd:
        return False
    try:
        Path(cwd).resolve().relative_to(WORKSPACE)
        return True
    except ValueError:
        return False


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def replace_block(text: str, start: str, end: str, body: str) -> str:
    block = f"{start}\n{body.rstrip()}\n{end}"
    if start in text and end in text:
        return re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, text, flags=re.S)
    return text.rstrip("\n") + "\n\n" + block + "\n"


def read_frontmatter(path: Path) -> dict:
    meta = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return meta
            for line in f:
                if line.strip() == "---":
                    break
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            for line in f:
                if line.startswith("# "):
                    meta["title"] = line[2:].strip()
                    break
    except OSError:
        pass
    return meta


# ---------------------------------------------------------------- 会话解析
class Digest:
    def __init__(self, agent: str, source: Path):
        self.agent, self.source = agent, source
        self.id = self.cwd = self.model = self.final = ""
        self.start: dt.datetime | None = None
        self.end: dt.datetime | None = None
        self.prompts: list[str] = []
        self.tools: dict[str, int] = {}
        self.files: list[str] = []

    def touch(self, t: dt.datetime | None) -> None:
        if t:
            self.start = t if not self.start or t < self.start else self.start
            self.end = t if not self.end or t > self.end else self.end

    def tool(self, name) -> None:
        if name:
            self.tools[name] = self.tools.get(name, 0) + 1

    def file(self, path) -> None:
        if isinstance(path, str) and path and path not in self.files:
            self.files.append(path)

    def prompt(self, text) -> None:
        text = (text or "").strip()
        if not text or text.startswith("<") or re.match(r"^/\w", text):
            return
        # 各家注入的规则/环境说明不是用户指令；同一条指令在两种记录里各出现一次的去重
        if re.match(r"^#\s*(AGENTS\.md|CLAUDE\.md) instructions", text) or "<INSTRUCTIONS>" in text[:200]:
            return
        if self.prompts and self.prompts[-1] == text:
            return
        self.prompts.append(text)

    @property
    def substantive(self) -> bool:
        return bool(self.prompts) and (sum(self.tools.values()) >= 2 or len(self.final) >= 200)

    @property
    def digest_name(self) -> str:
        stamp = self.start.astimezone().strftime("%Y%m%d-%H%M") if self.start else "00000000-0000"
        return f"{self.agent}-{stamp}-{source_key(self.source)}.md"


def source_key(src: Path) -> str:
    """源文件的稳定短键：文件名末尾的 uuid 段（pi 是 日期_uuid，codex 是 rollout-日期-uuid，claude 是 uuid）"""
    return re.sub(r"[^A-Za-z0-9]", "", src.stem)[-12:]


def blocks_text(content, kinds=("text", "input_text", "output_text")) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") in kinds)
    return ""


def parse_pi(path: Path) -> Digest:
    d = Digest("pi", path)
    for rec in iter_jsonl(path):
        t = rec.get("type")
        d.touch(parse_ts(rec.get("timestamp")))
        if t == "session":
            d.id, d.cwd = rec.get("id", ""), rec.get("cwd", "")
        elif t == "model_change":
            d.model = rec.get("modelId", "") or d.model
        elif t == "message":
            m = rec.get("message", {})
            d.touch(parse_ts(m.get("timestamp")))
            if m.get("role") == "user":
                d.prompt(blocks_text(m.get("content")))
            elif m.get("role") == "assistant":
                for b in m.get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "toolCall":
                        d.tool(b.get("name"))
                        if b.get("name") in ("read", "edit", "write"):
                            d.file((b.get("arguments") or {}).get("path"))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        d.final = b["text"]
    return d


def parse_claude(path: Path) -> Digest:
    d = Digest("claude", path)
    for rec in iter_jsonl(path):
        t = rec.get("type")
        if t not in ("user", "assistant"):
            continue
        d.touch(parse_ts(rec.get("timestamp")))
        d.id = rec.get("sessionId") or d.id
        d.cwd = rec.get("cwd") or d.cwd
        m = rec.get("message", {}) or {}
        content = m.get("content")
        if t == "user":
            if isinstance(content, str):
                d.prompt(content)
            else:
                d.prompt(blocks_text(content, ("text",)))
        else:
            d.model = m.get("model") or d.model
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    d.tool(b.get("name"))
                    if b.get("name") in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
                        inp = b.get("input") or {}
                        d.file(inp.get("file_path") or inp.get("notebook_path"))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    d.final = b["text"]
    return d


def parse_codex(path: Path) -> Digest:
    d = Digest("codex", path)
    seen_event_prompts = False
    for rec in iter_jsonl(path):
        t = rec.get("type")
        pl = rec.get("payload", {}) or {}
        d.touch(parse_ts(rec.get("timestamp")))
        if t == "session_meta":
            d.id, d.cwd = pl.get("id", ""), pl.get("cwd", "")
        elif t == "event_msg":
            kind = pl.get("type")
            if kind == "user_message":
                seen_event_prompts = True
                d.prompt(pl.get("message"))
            elif kind == "agent_message" and (pl.get("message") or "").strip():
                d.final = pl["message"]
            elif kind == "task_complete" and (pl.get("last_agent_message") or "").strip():
                d.final = pl["last_agent_message"]
        elif t == "response_item":
            kind = pl.get("type")
            if kind == "message" and pl.get("role") == "user" and not seen_event_prompts:
                d.prompt(blocks_text(pl.get("content")))  # 旧格式：用户消息只在 response_item 里
            elif kind == "message" and pl.get("role") == "assistant" and not d.final:
                d.final = blocks_text(pl.get("content"))
            elif kind == "function_call":
                d.tool(pl.get("name"))
                if pl.get("name") == "apply_patch":
                    for m in re.finditer(r"\*\*\* (?:Update|Add|Delete) File: (.+)", str(pl.get("arguments", ""))):
                        d.file(m.group(1).strip())
    return d


PARSERS = {"pi": parse_pi, "claude": parse_claude, "codex": parse_codex}


def session_sources(days: int) -> list[tuple[str, Path]]:
    cutoff = dt.datetime.now().timestamp() - days * 86400
    out = []
    if PI_SESSIONS.is_dir():
        out += [("pi", p) for p in PI_SESSIONS.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff]
    if CLAUDE_PROJECTS.is_dir():
        prefix = claude_slug(WORKSPACE)
        for proj in CLAUDE_PROJECTS.iterdir():
            if proj.name.startswith(prefix):
                out += [("claude", p) for p in proj.glob("*.jsonl") if p.stat().st_mtime >= cutoff]
    if CODEX_SESSIONS.is_dir():
        out += [("codex", p) for p in CODEX_SESSIONS.rglob("*.jsonl") if p.stat().st_mtime >= cutoff]
    return out


def commits_between(start, end) -> list[str]:
    if not start or not end:
        return []
    out = []
    for repo in repos():
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "log", "--all", "--format=%h %s",
                 f"--since={start.isoformat()}", f"--until={(end + dt.timedelta(minutes=5)).isoformat()}"],
                capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            continue
        out += [f"{repo.name}@{line.strip()}" for line in res.stdout.splitlines()[:10] if line.strip()]
    return out


def render_digest(d: Digest) -> str:
    rel_files = []
    for f in d.files[:30]:
        try:
            rel_files.append(str(Path(f).resolve().relative_to(WORKSPACE)))
        except (ValueError, OSError):
            rel_files.append(f)
    tools = ", ".join(f"{k}×{v}" for k, v in sorted(d.tools.items(), key=lambda kv: -kv[1])[:8])
    lines = ["---", f"agent: {d.agent}", f"id: {d.id or d.source.stem}", f"cwd: {d.cwd}", f"model: {d.model}",
             f"start: {fmt_local(d.start)}", f"end: {fmt_local(d.end)}", f"source: {d.source}", "---", "",
             f"# {d.agent} 会话 {fmt_local(d.start)} · {one_line(d.prompts[0], 80)}", "", "## 用户指令"]
    lines += [f"- {one_line(p, 300)}" for p in d.prompts[:8]]
    if len(d.prompts) > 8:
        lines.append(f"- …（共 {len(d.prompts)} 条）")
    lines += ["", "## 最终回复", "", one_line(d.final, 800) or "（无）", "", "## 工具调用", "", tools or "（无）", ""]
    if rel_files:
        lines += ["## 读写过的文件", ""] + [f"- {f}" for f in rel_files] + [""]
    commits = commits_between(d.start, d.end)
    if commits:
        lines += ["## 期间提交", ""] + [f"- {c}" for c in commits] + [""]
    return "\n".join(lines)


def harvest(days: int) -> int:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for agent, src in session_sources(days):
        existing = next(SESSIONS_DIR.glob(f"{agent}-*-{source_key(src)}.md"), None)
        if existing and existing.stat().st_mtime >= src.stat().st_mtime:
            continue
        try:
            d = PARSERS[agent](src)
        except (OSError, UnicodeDecodeError) as e:
            log(f"跳过 {src}: {e}")
            continue
        if not d.substantive or not under_workspace(d.cwd):
            continue
        target = SESSIONS_DIR / d.digest_name
        if existing and existing != target:
            existing.unlink()
        target.write_text(render_digest(d), encoding="utf-8")
        written += 1
    update_recent_block()
    log(f"harvest: 新增/更新 {written} 份会话摘要 → {SESSIONS_DIR}")
    return written


def recent_sessions_lines() -> list[str]:
    items = []
    for p in SESSIONS_DIR.glob("*.md"):
        m = read_frontmatter(p)
        items.append((m.get("end", ""), m.get("agent", ""), m.get("title", p.stem), p.name))
    items.sort(reverse=True)
    out = []
    for end, agent, title, name in items[:RECENT_LIMIT]:
        title = re.sub(r"^\w+ 会话 [\d\- :]+ · ", "", title)
        out.append(f"- {end[:10]} {agent} · {one_line(title, 90)} → sessions/{name}")
    return out


def update_recent_block() -> None:
    mem = PROJECT_HUB / "MEMORY.md"
    text = mem.read_text(encoding="utf-8") if mem.exists() else "# Memory Index\n"
    body = "## 近期会话（agent-memory 自动生成）\n" + "\n".join(recent_sessions_lines() or ["- （暂无）"])
    mem.write_text(replace_block(text, SESS_START, SESS_END, body), encoding="utf-8")


# ---------------------------------------------------------------- pi(magic-context) 记忆导入
CATEGORY_TYPE = {"PROJECT_RULES": "feedback", "CONSTRAINTS": "project", "ARCHITECTURE": "project", "CONFIG_VALUES": "reference"}


def import_pi_memories() -> int:
    if not MAGIC_DB.exists():
        log("import: 未找到 magic-context 库，跳过")
        return 0
    PI_MEM_DIR.mkdir(parents=True, exist_ok=True)
    try:
        c = sqlite3.connect(f"file:{MAGIC_DB}?mode=ro", uri=True)
        rows = c.execute(
            "select id, category, content, importance, status, updated_at, created_at, project_path "
            "from memories order by id").fetchall()
    except sqlite3.Error as e:
        log(f"import: 读取 magic-context 失败: {e}")
        return 0
    changed = 0
    active_ids = set()
    for mid, category, content, importance, status, updated, created, project in rows:
        path = PI_MEM_DIR / f"pi-mem-{mid}.md"
        if status != "active":
            if path.exists():
                path.unlink()
                changed += 1
            continue
        active_ids.add(mid)
        updated_iso = fmt_local(parse_ts(updated or created))
        if path.exists() and read_frontmatter(path).get("updated") == updated_iso:
            continue
        title = one_line(content.split("：")[0].split(":")[0].split("。")[0], 90)
        body = "\n".join([
            "---", f"name: pi-mem-{mid}", f"description: {one_line(content, 160)}", "metadata:",
            f"  type: {CATEGORY_TYPE.get(category, 'project')}", f"  source: magic-context#{mid} ({category}, {project})",
            f"  importance: {importance}", f"updated: {updated_iso}", "---", "", f"# {title}", "", content.strip(), ""])
        path.write_text(body, encoding="utf-8")
        changed += 1
    for stale in PI_MEM_DIR.glob("pi-mem-*.md"):
        try:
            stale_id = int(stale.stem.split("-")[-1])
        except ValueError:
            continue
        if stale_id not in active_ids:
            stale.unlink()
            changed += 1
    update_pi_index()
    log(f"import: magic-context 活跃记忆 {len(active_ids)} 条，本次变更 {changed} → {PI_MEM_DIR}")
    return changed


def pi_memory_entries() -> list[tuple[str, str, str, str]]:
    """(updated, category, description, filename)，PROJECT_RULES/CONSTRAINTS 优先，其余按更新时间倒序。"""
    items = []
    for p in PI_MEM_DIR.glob("pi-mem-*.md") if PI_MEM_DIR.exists() else []:
        m = read_frontmatter(p)
        cat = re.search(r"\((\w+),", m.get("source", "")) if "source" in m else None
        items.append((m.get("updated", ""), cat.group(1) if cat else "", m.get("description", p.stem), p.name))
    rules = sorted((i for i in items if i[1] in ("PROJECT_RULES", "CONSTRAINTS")), reverse=True)
    others = sorted((i for i in items if i[1] not in ("PROJECT_RULES", "CONSTRAINTS")), reverse=True)
    return rules + others


def update_pi_index() -> None:
    entries = pi_memory_entries()
    full = ["# pi(magic-context) 导入的项目记忆索引", "", f"共 {len(entries)} 条，按 PROJECT_RULES/CONSTRAINTS 优先、更新时间倒序。", ""]
    full += [f"- [{one_line(desc, 110)}]({name}) — {cat} {upd[:10]}" for upd, cat, desc, name in entries]
    (PI_MEM_DIR / "INDEX.md").write_text("\n".join(full) + "\n", encoding="utf-8")
    mem = PROJECT_HUB / "MEMORY.md"
    text = mem.read_text(encoding="utf-8") if mem.exists() else "# Memory Index\n"
    shown = entries[:PI_INDEX_LIMIT]
    body = [f"## pi 记忆（magic-context 导入，共 {len(entries)} 条；完整索引 pi-memories/INDEX.md）"]
    body += [f"- [{one_line(desc, 100)}](pi-memories/{name}) — {cat}" for upd, cat, desc, name in shown]
    if len(entries) > len(shown):
        body.append(f"- …其余 {len(entries) - len(shown)} 条见 pi-memories/INDEX.md")
    mem.write_text(replace_block(text, PIMEM_START, PIMEM_END, "\n".join(body)), encoding="utf-8")


# ---------------------------------------------------------------- 渲染
def index_block(hub: Path, layer_name: str) -> str:
    mem = hub / "MEMORY.md"
    text = mem.read_text(encoding="utf-8") if mem.exists() else ""
    text = re.sub(r"\]\((?!/|https?://)([^)]+\.md)\)", lambda m: f"]({hub / m.group(1)})", text)
    text = text.replace("→ sessions/", f"→ {hub}/sessions/").replace("见 pi-memories/", f"见 {hub}/pi-memories/")
    text = re.sub(r"^# Memory Index\s*", "", text).strip()
    head = (f"## {layer_name}（agent-memory 自动生成，勿手改本段；改记忆请改 {hub}/ 下的文件）\n\n"
            f"以下是 pi / Codex / Claude Code 共享的记忆索引，开工前先过一遍；条目对应的文件用 read 工具按需展开。\n"
            f"发现值得长期记住的事实（约定、坑、决策），在 {hub}/ 新建一个 .md（frontmatter: name/description/type）"
            f"并在 MEMORY.md 加一行索引；跨项目的个人偏好放 {GLOBAL_HUB}/。\n\n")
    return redact(head + (text or "（暂无条目）") + "\n")


def ensure_claude_import(claude_md: Path, import_line: str, intro: str) -> bool:
    if claude_md.exists():
        text = claude_md.read_text(encoding="utf-8")
        if import_line in text:
            return False
        claude_md.write_text(text.rstrip("\n") + f"\n\n{import_line}\n", encoding="utf-8")
    else:
        claude_md.write_text(f"{intro}\n\n{import_line}\n", encoding="utf-8")
    return True


def write_block(target: Path, block: str, default_text: str) -> None:
    text = target.read_text(encoding="utf-8") if target.exists() else default_text
    new = replace_block(text, MEM_START, MEM_END, block)
    if new != text:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8")
        log(f"render: 已更新 {target}")


def render() -> None:
    """索引写入 pi 与 Codex 的全局文件，不写项目目录。
    Claude 的 ~/.claude/CLAUDE.md 已经 @ 了 pi 的 AGENTS.md，这里不重复写，避免读两遍。"""
    parts = [index_block(GLOBAL_HUB, "跨项目记忆")]
    seen = []
    for ws in workspaces():
        hub = hub_for(ws)
        if not hub.exists():
            continue
        seen.append(ws)
        parts.append(index_block(hub, f"项目记忆 · {ws.name}"))
    if len(seen) > 1:
        log(f"render: 已登记 {len(seen)} 个工作区，项目索引会全部进入每次会话（见 README 已知限制）")
    personal = "\n".join(parts)
    for target in (PI_GLOBAL_AGENTS, CODEX_GLOBAL_AGENTS):
        write_block(target, personal, f"# {target.name}\n")
    if CLAUDE_GLOBAL_MD.exists():
        text = CLAUDE_GLOBAL_MD.read_text(encoding="utf-8")
        stripped = re.sub(re.escape(MEM_START) + r".*?" + re.escape(MEM_END) + r"\n?",
                          "", text, flags=re.S)
        if stripped != text:
            CLAUDE_GLOBAL_MD.write_text(stripped, encoding="utf-8")
            log(f"render: 已从 {CLAUDE_GLOBAL_MD} 去掉重复索引（Claude 经 @pi AGENTS.md 读取）")
    if PI_GLOBAL_AGENTS.exists():
        ensure_claude_import(CLAUDE_GLOBAL_MD, f"@{PI_GLOBAL_AGENTS}", "# CLAUDE.md")
    for ws in seen:
        activate(ws)
        unrender_repos()
        remove_tool_files(ws)


def remove_tool_files(workspace: Path) -> None:
    """删掉早期版本写在工作区根的 AGENTS.md / CLAUDE.md。只删工具自己生成的，有用户内容的留下。"""
    for name in ("AGENTS.md", "CLAUDE.md"):
        f = workspace / name
        if not f.is_file() or (workspace / ".git").exists():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tool_made = "由 agent-memory 创建" in text or "agent-memory 维护" in text
        if tool_made and (MEM_START in text or name == "CLAUDE.md"):
            try:
                f.unlink()
                log(f"render: 已移除工作区根的工具文件 {f}")
            except OSError as e:
                log(f"render: 移除 {f} 失败：{e}")


# ---------------------------------------------------------------- 接线
def link_claude_memory_dirs() -> None:
    for root in [WORKSPACE] + repos():
        proj = CLAUDE_PROJECTS / claude_slug(root)
        proj.mkdir(parents=True, exist_ok=True)
        mem = proj / "memory"
        if mem.is_symlink():
            if mem.resolve() != PROJECT_HUB:
                mem.unlink()
                mem.symlink_to(PROJECT_HUB)
                log(f"init: 重新软链 {mem} → {PROJECT_HUB}")
            continue
        if mem.is_dir():
            for f in mem.iterdir():
                if f.is_file():
                    dst = PROJECT_HUB / f.name
                    if not dst.exists() or f.stat().st_mtime > dst.stat().st_mtime:
                        shutil.copy2(f, dst)
            backup = proj / f"memory.bak-{dt.datetime.now():%Y%m%d-%H%M%S}"
            mem.rename(backup)
            log(f"init: 已迁移 {mem} 内容到记忆库，原目录备份为 {backup.name}")
        mem.symlink_to(PROJECT_HUB)
        log(f"init: 软链 {mem} → {PROJECT_HUB}")


SHELL_HOOK = """# agent-memory：pi / codex / claude 启动前先同步共享记忆（由 agent_memory.py init 写入）
_agent_memory_sync() {{ python3 "{tool}" sync -q 2>/dev/null || true; }}
pi()     {{ _agent_memory_sync; command pi "$@"; }}
codex()  {{ _agent_memory_sync; command codex "$@"; }}
claude() {{ _agent_memory_sync; command claude "$@"; }}
"""


SKILL_DIR = REPO_ROOT / "plugins" / "agent-memory" / "skills" / "agent-memory"
LAUNCHER = DATA_ROOT / "bin" / "agent-memory"


def _packages() -> list:
    path = HOME / ".pi" / "agent" / "settings.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pkgs = data.get("packages") or []
    return pkgs if isinstance(pkgs, list) else []


def pi_plugin_installed() -> bool:
    repo = str(REPO_ROOT)
    for item in _packages():
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        if repo in text or text.rstrip("/").endswith("agent-memory"):
            return True
    return False


def plugin_dir_installed(root: Path) -> bool:
    if not root.exists():
        return False
    return any(p.is_dir() and p.name == "agent-memory" for p in root.rglob("agent-memory"))


def codex_plugin_installed() -> bool:
    return plugin_dir_installed(HOME / ".codex" / "plugins")


def claude_plugin_installed() -> bool:
    return plugin_dir_installed(HOME / ".claude" / "plugins")


def channel_report() -> str:
    def one(plugin: bool, link: Path) -> str:
        if plugin:
            return "插件"
        if link.is_symlink() and link.resolve() == SKILL_DIR:
            return "软链"
        return "未装"
    return (f"pi={one(pi_plugin_installed(), HOME / '.agents' / 'skills' / 'agent-memory')} "
            f"codex={one(codex_plugin_installed(), HOME / '.agents' / 'skills' / 'agent-memory')} "
            f"claude={one(claude_plugin_installed(), HOME / '.claude' / 'skills' / 'agent-memory')}")


def set_skill_link(dest: Path, enabled: bool) -> None:
    """enabled 时软链到唯一的 SKILL 目录；插件已装时删掉软链，保证只加载一份。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not enabled:
        if dest.is_symlink():
            dest.unlink()
            log(f"init: 已移除 {dest}（插件通道生效，避免同名技能加载两次）")
        return
    if dest.is_symlink() and dest.resolve() == SKILL_DIR:
        return
    if dest.exists() or dest.is_symlink():
        log(f"init: {dest} 已存在且不是本技能，跳过")
        return
    dest.symlink_to(SKILL_DIR)
    log(f"init: 技能软链 {dest} → {SKILL_DIR}")


def install_skill() -> None:
    """默认走软链通道。某一端已经用原生插件安装时，该端不再软链。"""
    if not (SKILL_DIR / "SKILL.md").is_file():
        sys.exit(f"技能目录缺少 SKILL.md: {SKILL_DIR}")
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    if not LAUNCHER.exists():
        LAUNCHER.symlink_to(TOOL_PATH)
    set_skill_link(HOME / ".agents" / "skills" / "agent-memory",
                   not (pi_plugin_installed() or codex_plugin_installed()))
    set_skill_link(HOME / ".claude" / "skills" / "agent-memory", not claude_plugin_installed())
    legacy = HOME / ".codex" / "skills" / "agent-memory"
    if legacy.is_symlink():
        legacy.unlink()
        log(f"init: 已移除 legacy 软链 {legacy}（Codex 会和 ~/.agents/skills 重复）")
    log(f"init: 当前通道 {channel_report()}")


def install_shell_hooks() -> None:
    hook_file = TOOL_PATH.parent / "shell-hooks.sh"
    hook_file.write_text(SHELL_HOOK.format(tool=TOOL_PATH), encoding="utf-8")
    bashrc = HOME / ".bashrc"
    marker, end = "# >>> agent-memory >>>", "# <<< agent-memory <<<"
    block = f'{marker}\n[ -f "{hook_file}" ] && source "{hook_file}"\n{end}\n'
    text = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
    if marker in text and end in text:
        text = re.sub(re.escape(marker) + r".*?" + re.escape(end) + r"\n?", block, text, count=1, flags=re.S)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    bashrc.write_text(text, encoding="utf-8")
    log(f"init: ~/.bashrc 指向 {hook_file}（新开终端生效）")


def install_claude_hook() -> None:
    cmd = f"python3 {TOOL_PATH} sync -q"
    settings = {}
    if CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"init: {CLAUDE_SETTINGS} 不是合法 JSON，跳过 SessionStart hook")
            return
    entries = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    if any(h.get("command") == cmd for e in entries for h in e.get("hooks", [])):
        return
    entries.append({"hooks": [{"type": "command", "command": cmd}]})
    CLAUDE_SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"init: Claude SessionStart hook 已写入 {CLAUDE_SETTINGS}")


def ensure_hubs() -> None:
    for hub in (GLOBAL_HUB, PROJECT_HUB):
        hub.mkdir(parents=True, exist_ok=True)
        if not (hub / "MEMORY.md").exists():
            (hub / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
    SESSIONS_DIR.mkdir(exist_ok=True)


def require_workspaces() -> list[Path]:
    found = workspaces()
    if not found:
        sys.exit(f"还没有登记工作区。先运行: python3 {TOOL_PATH} init --workspace <项目目录>")
    return found


def sync(days: int) -> None:
    GLOBAL_HUB.mkdir(parents=True, exist_ok=True)
    if not (GLOBAL_HUB / "MEMORY.md").exists():
        (GLOBAL_HUB / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
    for ws in require_workspaces():
        activate(ws)
        ensure_hubs()
        import_pi_memories()
        harvest(days)
        link_claude_memory_dirs()
        (PROJECT_HUB / ".last-sync").write_text(dt.datetime.now().isoformat(timespec="seconds") + "\n",
                                                encoding="utf-8")
    render()


def init(days: int, workspace: str | None) -> None:
    if workspace:
        register_workspace(Path(workspace))
    require_workspaces()
    install_skill()
    install_shell_hooks()
    install_claude_hook()
    sync(days)
    status()


def backup(to: Path) -> Path:
    """把工具 + 两层记忆库打成一个 tar.gz（含 Claude 记忆备份目录不含会话原文）"""
    import tarfile
    to.mkdir(parents=True, exist_ok=True)
    out = to / f"agent-memory-{dt.datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for src in (TOOL_PATH.parent, GLOBAL_HUB, PROJECT_HUB):
            if src.exists():
                tar.add(src, arcname=src.name if src != PROJECT_HUB else "project-memory")
    log(f"backup: {out}  ({out.stat().st_size // 1024} KB)")
    return out


def status() -> None:
    found = workspaces()
    if found:
        activate(found[0])
    def n(p: Path, pat="*.md"):
        return len(list(p.glob(pat))) if p.exists() else 0
    print(f"跨项目层: {GLOBAL_HUB}  （{n(GLOBAL_HUB)} 个文件）")
    print(f"项目层:   {PROJECT_HUB}  （{n(PROJECT_HUB)} 个记忆文件，{n(PI_MEM_DIR, 'pi-mem-*.md')} 条 pi 记忆，{n(SESSIONS_DIR)} 份会话摘要）")
    for target in (PI_GLOBAL_AGENTS, CODEX_GLOBAL_AGENTS):
        ok = target.exists() and MEM_START in target.read_text(encoding="utf-8")
        print(f"- 全局入口 {target}: {'✓' if ok else '✗'}")
    claude_via_pi = (CLAUDE_GLOBAL_MD.exists()
                     and f"@{PI_GLOBAL_AGENTS}" in CLAUDE_GLOBAL_MD.read_text(encoding="utf-8")
                     and MEM_START not in CLAUDE_GLOBAL_MD.read_text(encoding="utf-8"))
    print(f"- 全局入口 {CLAUDE_GLOBAL_MD}: {'经 @pi 读取（不重复渲染）' if claude_via_pi else '✗'}")
    for root in [WORKSPACE] + repos():
        mem = CLAUDE_PROJECTS / claude_slug(root) / "memory"
        linked = mem.is_symlink() and mem.resolve() == PROJECT_HUB
        agents = root / "AGENTS.md"
        rendered = agents.exists() and MEM_START in agents.read_text(encoding="utf-8", errors="replace")
        print(f"- {root.name}: Claude记忆软链={'✓' if linked else '✗'}  仓库记忆块={'有（不应出现）' if rendered else '无'}")
    last = PROJECT_HUB / ".last-sync"
    print(f"- 上次 sync: {last.read_text(encoding='utf-8').strip() if last.exists() else '从未'}")
    bashrc = (HOME / ".bashrc").read_text(encoding="utf-8") if (HOME / ".bashrc").exists() else ""
    print(f"- bashrc 包装函数: {'✓' if 'agent-memory' in bashrc else '✗'}")
    hooked = CLAUDE_SETTINGS.exists() and str(TOOL_PATH) in CLAUDE_SETTINGS.read_text(encoding="utf-8")
    print(f"- Claude SessionStart hook: {'✓' if hooked else '✗'}")
    print(f"- 生效通道: {channel_report()}")
    print("- 已知限制: 登记多个工作区后，每个工作区的项目索引都会进入 pi/Codex 全局文件，所有会话全量加载")


def main() -> None:
    global QUIET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "sync", "import", "harvest", "render", "status", "backup"])
    ap.add_argument("--workspace", help="init 时登记的工作区目录")
    ap.add_argument("--to", default=str(DATA_ROOT / "backup"), help="backup 输出目录")
    ap.add_argument("--days", type=int, default=60, help="只看最近 N 天的会话（默认 60）")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()
    QUIET = args.quiet
    if args.command == "init":
        init(args.days, args.workspace)
    elif args.command == "sync":
        sync(args.days)
    elif args.command == "import":
        for ws in require_workspaces():
            activate(ws)
            ensure_hubs()
            import_pi_memories()
    elif args.command == "harvest":
        for ws in require_workspaces():
            activate(ws)
            ensure_hubs()
            harvest(args.days)
    elif args.command == "render":
        render()
    elif args.command == "status":
        status()
    elif args.command == "backup":
        backup(Path(args.to))


if __name__ == "__main__":
    main()
