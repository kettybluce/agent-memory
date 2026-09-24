---
name: agent-dispatch
description: Claude 当大脑把实现/测试类工作派给 codex 或 pi 后台执行，回收结果并验收。用户说「派给 codex/pi 干」「让 codex 跑」「dispatch 到 pi」时使用。
---

# agent-dispatch

Claude 是设计/验收大脑；codex 和 pi 是执行苦力。命令垫片在 `~/.agent-dispatch/bin/agent-dispatch`，job 数据在 `~/.agent-dispatch/jobs/`，不进任何仓库。

## 什么时候用

- 用户明确说派活给 codex / pi，或要求 Claude 只设计不动手时。
- 需要多个执行端并行跑独立任务（不同 cwd 的实现/测试）。
- 大改动想让执行者带沙箱跑、Claude 事后统一审查 diff。

简单小活（改一两行、查个东西）不要 dispatch，Claude 直接做更快。

## 派单

```bash
# 基本派单（后台启动，立即返回 job id）
~/.agent-dispatch/bin/agent-dispatch dispatch --tool codex \
  --cwd /home/tfdx8045/code/agent/ai-assistant-platform \
  --sandbox workspace-write \
  "任务书：实现XX。约束：…。完成标准：跑 mvn test -pl runtime-service 全绿。"

# 长任务书用 stdin（含中文/引号更稳）
~/.agent-dispatch/bin/agent-dispatch dispatch --tool pi --cwd <目录> - <<'EOF'
任务书全文……
EOF

# 透传底层 CLI 参数（放最后）
~/.agent-dispatch/bin/agent-dispatch dispatch --tool codex --extra -m gpt-5.2 -- "..."
```

- `--tool codex` 默认；`--tool pi` 走 `pi -p --mode json`。
- job 会登记为 `codex-<job-id>` 或 `pi-<job-id>`；可用 `--agent-name` 固定名称，便于 Claude、Pi 互发消息。
- codex 默认沙箱 `workspace-write`；只读调研用 `read-only`。
- 任务书是给执行者的完整交接：背景 + 要改什么 + 约束（别动什么）+ **完成标准**（跑哪个测试/命令、什么算过）。
- 立即返回 job id（形如 `d-20260924-105000-123456`），不要轮询等待，先干别的，用户问起或需要结果时再查。
- 两个 agent 并行派单不会再因同一秒撞 job id；但仍必须使用不重叠的 cwd/文件范围。

## 统一通信（Pi / Codex / Claude）

三类 agent 共用一份消息 inbox：Pi 使用 `PI_MESSENGER_DIR` 下的原生 registry/inbox；Claude、Codex 的身份登记在 `~/.agent-dispatch/mesh/registry/`，但收件箱与 Pi 共用 `PI_MESSENGER_DIR/inbox/`。因此 Pi 原生 `pi_messenger({ action: "send" })` 和 `agent-dispatch message` 可以互相投递，不再有第二套收件箱。

非 Pi agent 启动后登记并轮询收件箱：

```bash
agent-dispatch register --name ClaudeSupervisor --type claude --cwd "$PWD"
agent-dispatch register --name CodexWorker --type codex --cwd "$PWD"
agent-dispatch inbox --name ClaudeSupervisor --consume
agent-dispatch message --to CodexWorker --sender ClaudeSupervisor "请汇报当前阶段"
```

`dispatch --tool codex` / `dispatch --tool pi` 会自动登记 job agent，并把身份、发送和收件箱命令注入任务书。Codex/Claude 不是常驻 Messenger 进程，必须在关键节点主动执行 `inbox --consume`；消息会持久化，不会因模型正在思考而丢失。`agents` 同时列出 Pi 和 dispatch mesh 中的 agent。

### Pi 运行中通信

Pi 正在模型回合中时，`follow` 会被拒绝；这时用 Messenger 收件箱通道给运行中的 agent 发 steer 消息：

```bash
# 查看当前真正存活的 Pi Messenger agent（自动过滤死 PID）
agent-dispatch agents

# 按 agent 名称发消息；默认等待 2 秒确认收件箱已被消费
agent-dispatch message --to SagePhoenix "先停止当前重跑，读取现有校验结果后回复进度"
agent-dispatch message --to VividBear - <<'EOF'
只做本机验证，不要修改业务代码。完成后报告测试命令和退出码。
EOF

# 同一条指挥同时发给两个 agent（--to 可重复）
agent-dispatch message --to SagePhoenix --to VividBear "汇报当前阶段、阻塞点和下一步；不要重复全量重跑"

# 名称不确定时，可按 cwd 筛选；多个匹配时工具会拒绝猜测
agent-dispatch message --cwd /home/tfdx8045/code/agent "同步当前阶段结果"
```

### 防「假发送」

给 pi 的指令必须明确要求：回复要用 `pi_messenger({ action: "send", to: "<监督方名>", message: "..." })`；缺 `action` 会回退成 `status`，导致消息丢失。消息使用临时文件 + 原子 rename 投递，避免 `fs.watch` 在 JSON 尚未写完时被 Pi 读取。命令会报告「已消费」或「已进入收件箱但尚未消费」；后者应检查 `agents`、Pi 会话是否仍在模型回合中，而不是盲目重复发送。若对方是 pi，还要检查其回复是否缺 `action:"send"`；缺省会回退成 `status` 丢消息。

## 停滞自动唤醒

当 agent 停在等待回复、没有进入下一轮时，可启动前台哨兵轮询 registry 的 `lastActivityAt`，超过阈值自动向其收件箱投递唤醒消息：

```bash
agent-dispatch watch --to SagePhoenix --stall 300 --interval 30 --max-nudges 10
```

`watch` 同时支持 Pi registry 和 dispatch mesh registry；消息被消费或检测到活动恢复后会重新计时。registry 暂时不存在时也会按无活动处理并尝试投递，便于排查收件箱链路。日志打 stdout（时间戳 + 事件），达到 `--max-nudges` 上限自动退出，按 `Ctrl-C` 可退出。

## 跟踪与验收

```bash
~/.agent-dispatch/bin/agent-dispatch status                 # 列全部
~/.agent-dispatch/bin/agent-dispatch status --id <job>      # 单个（对账退出码）
~/.agent-dispatch/bin/agent-dispatch log --id <job> --tail 40
~/.agent-dispatch/bin/agent-dispatch log --id <job> --last  # 最终回复全文（验收就靠它）
```

- `status` 显示 running/done/failed/stopped；done≠验收通过。
- **验收流程（每次都做）**：`log --last` 看执行者自述 → `git -C <cwd> diff` 亲自复核改动 → 对照完成标准 → 不合格就 `follow` 打回。
- failed 时先 `log --stderr` 看崩溃原因，再决定重派或修任务书。

## 续接与终止

```bash
# 向同一会话追加指令（保留执行者已建立的上下文，比重派省 token）
~/.agent-dispatch/bin/agent-dispatch follow --id <job> "上一条任务书里第2点没做完，补上后再跑一遍测试"

~/.agent-dispatch/bin/agent-dispatch stop --id <job>   # SIGTERM 进程组；改动不回滚，记得 git diff
~/.agent-dispatch/bin/agent-dispatch gc                # 清 14 天前已结束的 job
```

## 纪律

- 派单前 `git -C <cwd> status` 确认干净，避免执行者的改动混进你未提交的半成品。
- 任务书里写清「不要 push、不要动 tmp/、不要改无关文件」等约束，别指望执行者自己领会。
- 并行派多个 job 时确保各 job 的 cwd/文件范围不重叠。
- UAT/测试环境操作遵循项目记忆 pi-mem-95 的纪律，任务书里明确「本机验证，不碰 UAT」。
