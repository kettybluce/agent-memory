#!/usr/bin/env python3
"""agent-dispatch：Claude 当大脑，把实现/测试类工作派给 codex / pi 后台执行，并回收结果验收。

只用标准库，秒级、离线、确定性（不调模型）。数据在 ~/.agent-dispatch/，不进仓库。

    agent_dispatch.py init        首次接线：建数据目录、bin 垫片、三端技能软链
    agent_dispatch.py dispatch    派单：后台启动 codex exec / pi -p，立即返回 job id
    agent_dispatch.py status      job 状态（--id 看单个；缺省列全部）
    agent_dispatch.py log         job 输出（--id；--tail N 行；--last 只看最终回复）
    agent_dispatch.py follow      向同一会话追加指令（job 已结束后；codex=exec resume，pi=--session 文件续接）
    agent_dispatch.py agents      列出在线 Pi Messenger agent
    agent_dispatch.py message     向 Pi / Codex / Claude 的统一收件箱原子投递消息
    agent_dispatch.py inbox       读取统一收件箱中的消息
    agent_dispatch.py register    注册 Claude / Codex 等非 Pi agent
    agent_dispatch.py watch       监控 agent 停滞并自动投递唤醒消息
    agent_dispatch.py stop        终止 job（SIGTERM 进程组）
    agent_dispatch.py gc          清理已结束超过 N 天的 job（默认 14 天）

每个 job 一个目录 ~/.agent-dispatch/jobs/<id>/：
  job.json（状态/命令/进程）  stdout.log（过程 JSONL）  stderr.log  follow-*.txt（续接指令）
  last-message.txt（最终回复；codex 由 --output-last-message 落盘，pi 由本工具从日志提取）
  pi 的 session 文件留在 job 目录内，follow 按路径续接。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
DATA_ROOT = Path(os.environ.get("AGENT_DISPATCH_HOME", HOME / ".agent-dispatch")).resolve()
JOBS_DIR = DATA_ROOT / "jobs"
LAUNCHER = DATA_ROOT / "bin" / "agent-dispatch"
TOOL_PATH = Path(__file__).resolve()
REPO_ROOT = TOOL_PATH.parent.parent
SKILL_DIR = REPO_ROOT / "plugins" / "agent-dispatch" / "skills" / "agent-dispatch"
MESH_DIR = DATA_ROOT / "mesh"
MESH_REGISTRY = MESH_DIR / "registry"
MESH_INBOX = MESH_DIR / "inbox"

SANDBOX_CHOICES = ["read-only", "workspace-write", "danger-full-access"]


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- job 读写
def atomic_write_json(path: Path, payload: dict) -> None:
    """Write a complete message before exposing it to pi's fs.watch consumer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def is_process_alive(pid: int) -> bool:
    return pid > 0 and pid_alive(pid)


def messenger_root() -> Path:
    return Path(os.environ.get("PI_MESSENGER_DIR", HOME / ".pi" / "agent" / "messenger")).expanduser()


def active_agents() -> list[dict]:
    root = messenger_root()
    registry = root / "registry"
    result = []
    if not registry.is_dir():
        return result
    for path in sorted(registry.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            pid = int(item.get("pid", 0))
            if not is_process_alive(pid):
                continue
            item["_registry"] = str(path)
            item["_transport"] = "pi"
            item["_inbox"] = str(root / "inbox" / str(item.get("name")))
            result.append(item)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return result


def mesh_agents() -> list[dict]:
    result = []
    if not MESH_REGISTRY.is_dir():
        return result
    for path in sorted(MESH_REGISTRY.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            item["_registry"] = str(path)
            item["_transport"] = "dispatch"
            # Use Pi Messenger's shared inbox so pi_messenger({ action: "send" })
            # can reach Claude/Codex without a second relay daemon.
            item["_inbox"] = str(messenger_root() / "inbox" / str(item.get("name")))
            result.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    return result


def all_agents() -> list[dict]:
    # Prefer a live Pi registration if a name exists in both registries.
    result = {str(agent.get("name")): agent for agent in mesh_agents() if agent.get("name")}
    result.update({str(agent.get("name")): agent for agent in active_agents() if agent.get("name")})
    return list(result.values())



def write_mesh_registration(name: str, kind: str, pid: int, cwd: str | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        sys.exit("register: name 只允许字母、数字、下划线和连字符")
    path = MESH_REGISTRY / f"{name}.json"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {
        "name": name,
        "type": kind,
        "pid": pid,
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "state": "online",
        "registeredAt": now,
        "lastActivityAt": now,
        "transport": "dispatch",
    }
    atomic_write_json(path, payload)
    (messenger_root() / "inbox" / name).mkdir(parents=True, exist_ok=True)
    return path


def update_mesh_registration(name: str, **updates: object) -> None:
    path = MESH_REGISTRY / f"{name}.json"
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload.update(updates)
    atomic_write_json(path, payload)



def mailbox_for(name: str) -> Path:
    for agent in all_agents():
        if str(agent.get("name")) == name:
            return Path(agent["_inbox"])
    # Durable dispatch inbox: the recipient can register later and still receive it.
    return MESH_INBOX / name

def resolve_agents(names: list[str] | None, cwd: str | None = None) -> list[dict]:
    agents = all_agents()
    if names:
        by_name = {str(a.get("name")): a for a in agents}
        missing = [name for name in names if name not in by_name]
        if missing:
            available = ", ".join(by_name) or "（无在线 agent）"
            sys.exit(f"message: 找不到 agent {', '.join(missing)}；当前在线: {available}")
        # Preserve CLI order while avoiding duplicate delivery.
        return [by_name[name] for name in dict.fromkeys(names)]

    matches = [a for a in agents if not cwd or str(a.get("cwd", "")) == cwd]
    if len(matches) == 1:
        return matches
    if not matches:
        available = ", ".join(str(a.get("name")) for a in agents) or "（无在线 agent）"
        sys.exit(f"message: 找不到 agent；当前可用: {available}")
    names_text = ", ".join(str(a.get("name")) for a in matches)
    sys.exit(f"message: 目标不唯一，请用 --to 指定 agent 名称: {names_text}")


def job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    if not d.is_dir():
        sys.exit(f"job 不存在: {job_id}（看全部: agent-dispatch status）")
    return d


def load_job(d: Path) -> dict:
    return json.loads((d / "job.json").read_text(encoding="utf-8"))


def save_job(job: dict, d: Path) -> None:
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def runtime_min(job: dict) -> float:
    start = dt.datetime.fromisoformat(job["started_at"])
    end = job.get("ended_at")
    t = dt.datetime.fromisoformat(end) if end else dt.datetime.now()
    return (t - start).total_seconds() / 60


def extract_pi_final(out_dir: Path) -> str | None:
    """从 pi 的 JSONL 日志里提取最后一条 assistant 文本，落盘成 last-message.txt。"""
    text_path = out_dir / "last-message.txt"
    log = out_dir / "stdout.log"
    if not log.exists():
        return None
    final = None
    for line in log.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        if ev.get("type") in ("message_end", "turn_end") and msg.get("role") == "assistant":
            parts = [b.get("text", "") for b in msg.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
            if parts:
                final = "\n".join(parts)
    if final is not None:
        text_path.write_text(final + "\n", encoding="utf-8")
    return final


def codex_session_id(out_dir: Path) -> str | None:
    log = out_dir / "stdout.log"
    if not log.exists():
        return None
    m = re.search(r'"(?:thread_id|session_id|conversation_id)":\s*"([^"]+)"',
                  log.read_text(errors="replace"))
    return m.group(1) if m else None


def pi_session_file(out_dir: Path) -> Path | None:
    files = [p for p in out_dir.rglob("*.jsonl") if p.name != "stdout.log"]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def notify_job_completion(job: dict) -> None:
    recipient = job.get("reply_to")
    if not recipient or job.get("completion_notified"):
        return
    inbox = mailbox_for(str(recipient))
    final_path = JOBS_DIR / str(job["id"]) / "last-message.txt"
    summary = ""
    if final_path.exists():
        summary = one_line(final_path.read_text(errors="replace"), 1200)
    text = (f"job {job['id']} ({job['tool']}) 已{('成功完成' if job.get('state') == 'done' else '失败')}，"
            f"退出码={job.get('exit_code')}。")
    if summary:
        text += f" 最终回复：{summary}"
    message_id = f"job-{job['id']}-completion"
    atomic_write_json(inbox / f"{message_id}.json", {
        "id": message_id,
        "from": job.get("agent_name") or job["tool"],
        "to": recipient,
        "text": text,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "replyTo": job["id"],
    })
    job["completion_notified"] = True


def refresh(job: dict, d: Path) -> dict:
    """running 状态的对账：看 exit-code 文件 / 进程是否还活着。"""
    if job["state"] != "running":
        return job
    exit_file = d / "exit-code"
    code: int | None = None
    if exit_file.exists():
        try:
            code = int(exit_file.read_text().strip())
        except ValueError:
            code = -1
    elif not pid_alive(job["pid"]):
        code = -1
    if code is not None:
        job["state"] = "done" if code == 0 else "failed"
        job["exit_code"] = code
        job["ended_at"] = dt.datetime.now().isoformat(timespec="seconds")
        # follow 轮会复用 last-message.txt，结束后按工具各自重提/确认
        if job["tool"] == "pi":
            extract_pi_final(d)
        notify_job_completion(job)
        save_job(job, d)
        if job.get("agent_name"):
            update_mesh_registration(
                job["agent_name"],
                state=job["state"],
                lastActivityAt=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
    return job


# ---------------------------------------------------------------- 命令
def cmd_dispatch(args) -> None:
    prompt = (args.prompt or "").strip()
    if prompt == "-":
        prompt = ""
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        sys.exit("dispatch: 缺少任务书（位置参数，或用 - 从 stdin 读长任务书）")
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        sys.exit(f"dispatch: 工作目录不存在: {cwd}")

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    job_id = f"d-{stamp}"
    agent_name = getattr(args, "agent_name", None) or f"{args.tool}-{job_id}"
    communication_note = (
        f"\n\n[agent-dispatch mailbox] 你的统一身份是 {agent_name}。需要沟通时使用 "
        f"agent-dispatch message --to <目标名> --sender {agent_name} <消息>；开始工作和每个关键节点可用 "
        f"agent-dispatch inbox --name {agent_name} --consume 读取收件箱；任务进展/完成情况请发送给 {getattr(args, 'reply_to', None) or 'ClaudeSupervisor'}。\n"
    )
    prompt = prompt + communication_note
    d = JOBS_DIR / job_id
    d.mkdir()
    (d / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")

    if args.tool == "codex":
        cmd = ["codex", "exec", "--json", "--color", "never", "--skip-git-repo-check",
               "--sandbox", args.sandbox,
               "--output-last-message", str(d / "last-message.txt")]
        cmd += (args.extra or []) + [prompt]
    else:
        cmd = ["pi", "-p", "--mode", "json",
               "--session-dir", str(d), "--session-id", job_id]
        cmd += (args.extra or []) + [prompt]

    # bash 包装：命令跑完后把退出码写进 exit-code，status 靠它对账
    wrapped = ["bash", "-c", '"$@"; code=$?; printf "%s\\n" "$code" > "$0"; exit "$code"',
               str(d / "exit-code")] + cmd
    out = open(d / "stdout.log", "wb")
    err = open(d / "stderr.log", "wb")
    proc = subprocess.Popen(wrapped, cwd=cwd, stdout=out, stderr=err,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    out.close()
    err.close()
    job = {
        "id": job_id, "tool": args.tool, "cwd": str(cwd), "sandbox": args.sandbox,
        "prompt": one_line(prompt, 200), "pid": proc.pid,
        "state": "running", "exit_code": None,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "timeout_min": args.timeout,
        "cmd": cmd,
        "agent_name": agent_name,
        "reply_to": getattr(args, "reply_to", None),
        "completion_notified": False,
    }
    save_job(job, d)
    write_mesh_registration(job["agent_name"], args.tool, proc.pid, str(cwd))
    print(f"job: {job_id}  tool: {args.tool}  cwd: {cwd}  sandbox: {args.sandbox}")
    print(f"跟踪: agent-dispatch status --id {job_id}")
    print(f"输出: agent-dispatch log --id {job_id} --last")


def cmd_status(args) -> None:
    if args.id:
        d = job_dir(args.id)
        job = refresh(load_job(d), d)
        state = job["state"]
        line = f"job {job['id']} [{state}] {job['tool']}  cwd={job['cwd']}"
        if state == "running":
            line += f"  已运行 {runtime_min(job):.1f} 分钟"
            if job.get("timeout_min") and runtime_min(job) > job["timeout_min"]:
                line += f"  超过 timeout {job['timeout_min']} 分钟，考虑 stop"
        else:
            line += f"  exit={job.get('exit_code')}"
        print(line)
        print(f"任务书: {job['prompt']}")
        final = d / "last-message.txt"
        if final.exists() and final.stat().st_size > 0:
            print(f"最终回复: {one_line(final.read_text(errors='replace'), 300)}")
        return
    if not JOBS_DIR.is_dir():
        print("(还没有 job)")
        return
    rows = []
    for d in sorted(JOBS_DIR.iterdir(), reverse=True):
        if not (d / "job.json").exists():
            continue
        job = refresh(load_job(d), d)
        mark = {"running": "▶", "done": "✓", "failed": "✗", "stopped": "■"}.get(job["state"], "?")
        rows.append(f"{mark} {job['id']} [{job['state']}] {job['tool']}  {one_line(job['prompt'], 70)}")
    print("\n".join(rows) if rows else "(还没有 job)")


def cmd_log(args) -> None:
    d = job_dir(args.id)
    refresh(load_job(d), d)
    if args.last:
        final = d / "last-message.txt"
        if not final.exists() or final.stat().st_size == 0:
            sys.exit("log: 还没有最终回复（job 可能仍在跑，用 --tail 看过程输出）")
        print(final.read_text(errors="replace").strip())
        return
    log = d / ("stderr.log" if args.stderr else "stdout.log")
    if not log.exists():
        sys.exit(f"log: 文件不存在: {log}")
    lines = log.read_text(errors="replace").splitlines()
    tail = lines[-args.tail:] if args.tail > 0 else lines
    print("\n".join(tail) if tail else "(空)")


def cmd_follow(args) -> None:
    d = job_dir(args.id)
    job = refresh(load_job(d), d)
    if job["state"] == "running":
        sys.exit("follow: job 还在跑，先 stop 或等它结束")
    text = args.message.strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        sys.exit("follow: 缺少续接指令")
    n = job.get("turns", 0) + 1
    (d / f"follow-{n}.txt").write_text(text + "\n", encoding="utf-8")

    sandbox = args.sandbox or job.get("sandbox") or "workspace-write"
    if job["tool"] == "codex":
        sid = args.session or job.get("session_id") or codex_session_id(d)
        if not sid:
            sys.exit("follow: 找不到 codex 会话 id（看 stdout.log），可用 --session <id> 指定")
        job["session_id"] = sid
        cmd = ["codex", "exec", "resume", sid, "--json", "--color", "never",
               "--skip-git-repo-check", "--sandbox", sandbox,
               "--output-last-message", str(d / "last-message.txt"), text]
    else:
        sess = Path(args.session) if args.session else pi_session_file(d)
        if not sess:
            sys.exit("follow: 找不到 pi 的 session 文件（job 目录内 *.jsonl）")
        cmd = ["pi", "-p", "--mode", "json", "--session-dir", str(d),
               "--session", str(sess), text]

    out = open(d / "stdout.log", "ab")
    err = open(d / "stderr.log", "ab")
    wrapped = ["bash", "-c", '"$@"; code=$?; printf "%s\\n" "$code" > "$0"; exit "$code"',
               str(d / "exit-code")] + cmd
    proc = subprocess.Popen(wrapped, cwd=Path(job["cwd"]), stdout=out, stderr=err,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    out.close()
    err.close()
    job.update({"pid": proc.pid, "state": "running", "exit_code": None,
                "turns": n, "sandbox": sandbox,
                "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                "ended_at": None})
    save_job(job, d)
    if job.get("agent_name"):
        update_mesh_registration(job["agent_name"], pid=proc.pid, state="online",
                                 lastActivityAt=dt.datetime.now(dt.timezone.utc).isoformat())
    print(f"follow #{n} 已派发: job {job['id']}（同一会话续接）")
    print(f"跟踪: agent-dispatch status --id {job['id']}")


def cmd_agents(args) -> None:
    agents = all_agents()
    if not agents:
        print("（无在线 agent）")
        return
    for agent in agents:
        activity = agent.get("activity") or {}
        last = activity.get("lastActivityAt", agent.get("lastActivityAt", agent.get("startedAt", "?")))
        status = agent.get("statusMessage") or agent.get("state") or "-"
        print(f"{agent.get('name')} type={agent.get('type', agent.get('_transport'))} "
              f"pid={agent.get('pid', '?')} cwd={agent.get('cwd', '-')} "
              f"last={last} status={status} transport={agent.get('_transport')}")


def cmd_register(args) -> None:
    path = write_mesh_registration(args.name, args.type, args.pid, args.cwd)
    print(f"已注册 {args.name} type={args.type} pid={args.pid} registry={path}")


def cmd_inbox(args) -> None:
    matches = [agent for agent in all_agents() if str(agent.get("name")) == args.name]
    if matches:
        inbox = Path(matches[0]["_inbox"])
    else:
        inbox = MESH_INBOX / args.name
    if not inbox.is_dir():
        print("（收件箱不存在或暂无消息）")
        return
    files = sorted(inbox.glob("*.json"))[:args.limit]
    if not files:
        print("（无消息）")
        return
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{path.name}: 无法读取: {exc}", file=sys.stderr)
            continue
        print(json.dumps(payload, ensure_ascii=False))
        if args.consume:
            path.unlink(missing_ok=True)


def cmd_message(args) -> None:
    targets = resolve_agents(args.to, args.cwd)
    text = (args.message or "").strip()
    if text == "-":
        text = ""
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        sys.exit("message: 缺少消息内容")
    root = messenger_root()
    results = []
    for index, target in enumerate(targets):
        inbox = Path(target.get("_inbox") or mailbox_for(str(target["name"])))
        message_id = f"{int(time.time() * 1000)}-{os.getpid()}-{index}"
        path = inbox / f"{message_id}.json"
        payload = {
            "id": message_id,
            "from": args.sender,
            "to": target["name"],
            "text": text,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "replyTo": None,
        }
        atomic_write_json(path, payload)
        results.append((target["name"], message_id, path))

    deadline = time.monotonic() + args.verify
    pending = {path for _, _, path in results}
    while pending and time.monotonic() < deadline:
        pending = {path for path in pending if path.exists()}
        if pending:
            time.sleep(0.1)

    for name, message_id, path in results:
        if path.exists():
            print(f"消息已投递到 {name} 收件箱，尚未消费 ({message_id})")
        else:
            print(f"消息已消费: {name} ({message_id})")
    if pending:
        print("部分消息尚未消费：agent 可能正在模型回合中、watcher 未启动或已卡住；不要盲目重复发送。")
        print("若对方是 pi，请检查其回复是否缺少 action:\"send\"；缺省会回退成 status，导致消息丢失。")


def pending_mtime(path) -> float:
    """pending 消息文件的 mtime，读取失败返回 0（视为早就过期）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def parse_activity_time(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def watch_registry_entry(name: str) -> tuple[dict | None, Path, str]:
    candidates = [
        (messenger_root() / "registry" / f"{name}.json", "pi"),
        (MESH_REGISTRY / f"{name}.json", "dispatch"),
    ]
    for registry_path, transport in candidates:
        if not registry_path.exists():
            continue
        try:
            return json.loads(registry_path.read_text(encoding="utf-8")), registry_path, transport
        except (OSError, json.JSONDecodeError):
            return None, registry_path, transport
    return None, candidates[0][0], "pi"


def cmd_watch(args) -> None:
    if args.interval <= 0 or args.stall <= 0 or args.max_nudges <= 0:
        sys.exit("watch: --interval、--stall、--max-nudges 必须为正数")

    name = args.to
    root = messenger_root()
    inbox = root / "inbox" / name
    nudges = 0
    pending: Path | None = None
    stalled_since: float | None = None

    def log(message: str) -> None:
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"[{now}] {message}", flush=True)

    log(f"开始监控 {name}：stall={args.stall:g}s interval={args.interval:g}s max-nudges={args.max_nudges}")
    try:
        while nudges < args.max_nudges:
            entry, registry_path, transport = watch_registry_entry(name)
            inbox = messenger_root() / "inbox" / name
            activity = entry.get("activity") if isinstance(entry, dict) else None
            activity = activity if isinstance(activity, dict) else {}
            last_value = activity.get("lastActivityAt")
            if last_value is None and isinstance(entry, dict):
                last_value = entry.get("lastActivityAt") or entry.get("startedAt")
            last_activity = parse_activity_time(last_value)
            now = time.time()
            activity_age = now - last_activity if last_activity is not None else None

            if pending is not None and not pending.exists():
                log(f"唤醒消息已消费 ({pending.name})，重新计时")
                pending = None
                stalled_since = now
            elif pending is not None and (now - pending_mtime(pending)) >= min(args.stall, 60):
                log(f"唤醒消息长期未被消费 ({pending.name})，视为对方未读取，继续下一轮")
                pending = None
            if activity_age is not None and activity_age < args.stall:
                if pending is not None:
                    log("检测到 agent 活动恢复，重置计时")
                    pending = None
                stalled_since = None
            elif pending is None:
                if stalled_since is None:
                    stalled_since = last_activity if last_activity is not None else now - args.stall
                if now - stalled_since >= args.stall:
                    message_id = f"{int(now * 1000)}-{os.getpid()}-{nudges}"
                    path = inbox / f"{message_id}.json"
                    elapsed = int(max(0, now - (last_activity or now - args.stall)))
                    payload = {
                        "id": message_id,
                        "from": args.sender,
                        "to": name,
                        "text": (f"【自动唤醒】你已停滞超过 {elapsed} 秒。请继续当前任务并把进展 send 给监督方；"
                                 "回复必须带 action:\"send\"，缺省会回退 status 丢消息。"),
                        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "replyTo": None,
                    }
                    atomic_write_json(path, payload)
                    nudges += 1
                    pending = path
                    log(f"已投递第 {nudges}/{args.max_nudges} 次唤醒到 {name} ({message_id})"
                        f"；transport={transport}；registry={'存在' if registry_path.exists() else '不存在'}")
                    if nudges >= args.max_nudges:
                        break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log(f"收到 Ctrl-C，退出；已投递 {nudges} 次唤醒")
        return
    log(f"达到 --max-nudges={args.max_nudges}，退出；共投递 {nudges} 次唤醒")


def cmd_stop(args) -> None:
    d = job_dir(args.id)
    job = refresh(load_job(d), d)
    if job["state"] != "running":
        sys.exit(f"stop: job {job['id']} 已结束（{job['state']}）")
    try:
        os.killpg(job["pid"], signal.SIGTERM)
        print(f"已发 SIGTERM 到进程组 {job['pid']}")
    except ProcessLookupError:
        print("进程已不在")
    job["state"] = "stopped"
    job["ended_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_job(job, d)
    if job.get("agent_name"):
        update_mesh_registration(job["agent_name"], state="stopped",
                                 lastActivityAt=dt.datetime.now(dt.timezone.utc).isoformat())
    print(f"job {job['id']} 标记为 stopped（改动不会回滚，记得 git diff 检查）")


def cmd_gc(args) -> None:
    if not JOBS_DIR.is_dir():
        return
    cutoff = dt.datetime.now() - dt.timedelta(days=args.days)
    removed = 0
    for d in JOBS_DIR.iterdir():
        if not (d / "job.json").exists():
            continue
        job = refresh(load_job(d), d)
        if job["state"] == "running":
            continue
        ended = job.get("ended_at")
        t = dt.datetime.fromisoformat(ended) if ended else dt.datetime.fromtimestamp(d.stat().st_mtime)
        if t < cutoff:
            shutil.rmtree(d)
            removed += 1
    print(f"gc: 清理 {removed} 个 {args.days} 天前已结束的 job")


# ---------------------------------------------------------------- init
def plugin_installed(root: Path) -> bool:
    return root.is_dir() and any(p.is_dir() and p.name == "agent-dispatch" for p in root.rglob("agent-dispatch"))


def set_skill_link(dest: Path) -> None:
    if plugin_installed(HOME / ".claude" / "plugins") and dest.parent == HOME / ".claude" / "skills":
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
            print(f"init: 已移除 {dest}（Claude 插件通道生效，避免重复加载）")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() and dest.resolve() == SKILL_DIR:
        return
    if dest.exists() or dest.is_symlink():
        print(f"init: {dest} 已存在且不是本技能，跳过")
        return
    dest.symlink_to(SKILL_DIR)
    print(f"init: 技能软链 {dest} → {SKILL_DIR}")


def cmd_init(args) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    if not LAUNCHER.exists():
        LAUNCHER.symlink_to(TOOL_PATH)
    if not (SKILL_DIR / "SKILL.md").is_file():
        sys.exit(f"技能目录缺少 SKILL.md: {SKILL_DIR}")
    set_skill_link(HOME / ".agents" / "skills" / "agent-dispatch")
    set_skill_link(HOME / ".claude" / "skills" / "agent-dispatch")
    print(f"init: 命令垫片 {LAUNCHER}")
    print(f"init: 数据目录 {JOBS_DIR}")
    print("用前确认 codex / pi 在 PATH 里：codex --version && pi --version")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dispatch", help="派单（后台执行，立即返回）")
    p.add_argument("prompt", nargs="?", help="任务书；- 从 stdin 读")
    p.add_argument("--tool", choices=["codex", "pi"], default="codex")
    p.add_argument("--cwd", help="执行者的工作目录（默认当前目录）")
    p.add_argument("--sandbox", choices=SANDBOX_CHOICES, default="workspace-write", help="codex 沙箱级别")
    p.add_argument("--timeout", type=int, default=30, help="超时提醒阈值（分钟，仅提醒不杀）")
    p.add_argument("--agent-name", help="统一消息网格中的执行 agent 名称")
    p.add_argument("--reply-to", default="ClaudeSupervisor", help="job 完成后通知的 agent 名称")
    p.add_argument("--extra", nargs=argparse.REMAINDER, help="透传底层 CLI 的附加参数，放最后")
    p.set_defaults(fn=cmd_dispatch)

    p = sub.add_parser("status", help="job 状态")
    p.add_argument("--id", help="job id（缺省列全部）")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("log", help="job 输出")
    p.add_argument("--id", required=True)
    p.add_argument("--tail", type=int, default=40, help="末尾 N 行（0=全部）")
    p.add_argument("--last", action="store_true", help="只看最终回复全文")
    p.add_argument("--stderr", action="store_true", help="看 stderr 而不是 stdout")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("follow", help="向同一会话追加指令")
    p.add_argument("--id", required=True)
    p.add_argument("message", nargs="?", help="续接指令；缺省从 stdin 读")
    p.add_argument("--sandbox", choices=SANDBOX_CHOICES)
    p.add_argument("--session", help="手动指定会话 id（codex）或 session 文件路径（pi）")
    p.set_defaults(fn=cmd_follow)

    p = sub.add_parser("agents", help="列出统一消息网格中的 agent")
    p.set_defaults(fn=cmd_agents)

    p = sub.add_parser("register", help="注册 Claude / Codex 等非 Pi agent 到统一消息网格")
    p.add_argument("--name", required=True, help="agent 名称")
    p.add_argument("--type", choices=["claude", "codex", "pi", "other"], default="other")
    p.add_argument("--pid", type=int, default=os.getpid(), help="对应进程 PID")
    p.add_argument("--cwd", help="工作目录，默认当前目录")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("inbox", help="读取统一收件箱")
    p.add_argument("--name", required=True, help="agent 名称")
    p.add_argument("--limit", type=int, default=20, help="最多读取消息数")
    p.add_argument("--consume", action="store_true", help="读取后删除消息")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("message", help="向运行中的 Pi Messenger agent 原子投递消息")
    p.add_argument("--to", action="append", help="目标 agent 名称；可重复指定多个 agent")
    p.add_argument("--cwd", help="未指定 --to 时按工作目录筛选")
    p.add_argument("--sender", default="ClaudeSupervisor", help="消息发送者名称")
    p.add_argument("--verify", type=float, default=2.0, help="等待收件箱消费的秒数")
    p.add_argument("message", nargs="?", help="消息内容；- 或省略时从 stdin 读取")
    p.set_defaults(fn=cmd_message)

    p = sub.add_parser("watch", help="监控 agent 停滞并自动投递唤醒消息")
    p.add_argument("--to", required=True, help="目标 agent 名称")
    p.add_argument("--interval", type=float, default=30, help="轮询间隔秒数")
    p.add_argument("--stall", type=float, default=300, help="无活动超过该秒数后唤醒")
    p.add_argument("--max-nudges", type=int, default=10, help="最多投递唤醒次数")
    p.add_argument("--sender", default="ClaudeSupervisor", help="消息发送者名称")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("stop", help="终止 job")
    p.add_argument("--id", required=True)
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("gc", help="清理旧 job")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=cmd_gc)

    p = sub.add_parser("init", help="首次接线")
    p.set_defaults(fn=cmd_init)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
