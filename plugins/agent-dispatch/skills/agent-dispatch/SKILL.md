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
- codex 默认沙箱 `workspace-write`；只读调研用 `read-only`。
- 任务书是给执行者的完整交接：背景 + 要改什么 + 约束（别动什么）+ **完成标准**（跑哪个测试/命令、什么算过）。
- 立即返回 job id（形如 `d-20260924-105000-123456`），不要轮询等待，先干别的，用户问起或需要结果时再查。
- 两个 agent 并行派单不会再因同一秒撞 job id；但仍必须使用不重叠的 cwd/文件范围。

## 运行中通信（不要再手动进 Pi 窗口补发）

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

消息使用临时文件 + 原子 rename 投递，避免 `fs.watch` 在 JSON 尚未写完时被 Pi 读取。命令会报告「已消费」或「已进入收件箱但尚未消费」；后者应检查 `agents`、Pi 会话是否仍在模型回合中，而不是盲目重复发送造成重复执行。

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
