---
name: tmux-ssh-workflow
description: 通过本机指定的 tmux socket 和一个或多个 tmux 会话名，复用会话中已建立的 SSH 或容器 shell，在远程服务器上执行命令、传输文件、启动与监控长期任务、停止远程任务，并支持容器重启后的重新进入与断线接管。文件传输使用 tmux 通道内的 Base64 分块协议，全程无需鉴权和人工参与，带分块与整体 SHA256 校验、原子改名。支持多会话并行编排（如批量退出容器、重启、重新进入）。当用户提供 tmux socket 与会话名、要求在远程机器跑任务或收取远程产物、或需要跨多台机器同步执行同一组操作时使用。不适用于新建 SSH 连接、管理远程主机本身或本地无 tmux 通道的场景。
---

# tmux SSH 工作流

在本机 tmux 会话已经持有远程 SSH 或容器 shell 的前提下，把这些会话当作远程控制通道使用。

## 核心模型

```
本机 tmux socket  →  tmux 会话（一个或多个）  →  会话内已鉴权的远程 shell  →  远程任务
```

只需要两个输入：`--socket` 和会话名。不要求也不接受 host、user、port、跳板机或 pane 映射配置。

四条不可违背的规则：

1. **每次操作重新发现活动 pane。** 容器重启或 SSH 重连会让旧 pane 失效。
2. **超时不等于失败。** 超时只说明回执还没出现，任务可能仍在运行；先检查，禁止自动重发。
3. **任务状态存在远程文件里，不存在 tmux 回滚缓冲里。** 本地进程退出后仍能接管。
4. **停止只针对远程任务。** 绝不执行 `tmux kill-session`、`kill-window`、`kill-pane`，会话必须留存以便排查。

## 标准流程

按顺序执行，除有明确理由外不要跳步。

1. 用 `session_preflight.py` 确认会话可用、远端 shell 位置正确、所需命令存在。
2. 用 `transfer.py put` 送入代码或输入数据。
3. 用 `tmux_exec.py` 做解包、依赖检查等准备动作。
4. 用 `remote_job.py launch` 启动长期任务，拿到 `pid` 与 `pgid`。
5. 用 `remote_job.py status` 轮询，直到状态变为 `SUCCEEDED` 或 `FAILED`。
6. 用 `remote_job.py collect` 在远端打包，再用 `transfer.py get` 取回并校验。
7. 需要中止时用 `remote_job.py stop`。

多台机器要做同一组有先后依赖的动作时，改用 `batch_sessions.py`。

## 脚本用法

全部脚本输出 JSON，成功返回 0。`--sessions` 接受逗号分隔的多个会话名，会话之间并行、单个会话内串行。

**前置检查**

```bash
python3 scripts/session_preflight.py --socket /path/to/sock --sessions n1,n2 \
  --expect-cwd-prefix /workspace --require-command python3
```

**执行命令**

```bash
python3 scripts/tmux_exec.py --socket S --sessions n1,n2 --command 'nvidia-smi -L' --show-output
python3 scripts/tmux_exec.py --socket S --sessions n1 --python-file body.py
```

`--python-file` 的内容是一个函数体，须以 `return` 结束，返回值必须可 JSON 序列化。适合做结构化检查，比解析人类可读输出可靠。

**传输文件**

```bash
python3 scripts/transfer.py put --socket S --sessions n1,n2 \
  --source ./bundle.zip --remote-path /remote/dir/bundle.zip --compress
python3 scripts/transfer.py get --socket S --session n1 \
  --remote-path /remote/dir/result.tar.gz --dest ./returns/result.tar.gz
```

要点：目录先在本地打包成单个归档再传；文本和高可压缩内容加 `--compress`；多会话下载会自动按会话名给本地文件加前缀，避免互相覆盖；覆盖已有文件需显式 `--overwrite`。

**远程任务**

```bash
python3 scripts/remote_job.py launch --socket S --sessions n1 --job-id run-001 \
  --job-root /remote/state/jobs --command './train.sh' --cwd /remote/project
python3 scripts/remote_job.py status  --socket S --sessions n1 --job-id run-001 --job-root /remote/state/jobs
python3 scripts/remote_job.py stop    --socket S --sessions n1 --job-id run-001 --job-root /remote/state/jobs
python3 scripts/remote_job.py collect --socket S --sessions n1 --job-id run-001 --job-root /remote/state/jobs
```

状态取值：`RUNNING`、`SUCCEEDED`、`FAILED`、`LOST`、`MISSING`。`LOST` 表示进程已不在但没有退出码，通常是被外部杀掉或机器重启，需要查日志而不是直接重跑。

任务在容器内运行时，`launch` 传 `--container NAME`，`stop` 会先停容器再处理进程组。

**多会话编排**

```bash
python3 scripts/batch_sessions.py --socket S --plan plan.json --dry-run
python3 scripts/batch_sessions.py --socket S --plan plan.json
```

计划文件里每个步骤声明自己的预期结果：

- `marker`：普通命令，`rc` 必须在 `allow_rc`（默认 `[0]`）内
- `disconnect`：预期 shell 消失，如退出容器或重启，不会向死 shell 重发命令
- `ready`：反复运行 `ready_command` 直到成功，用于重进后确认可用
- `json`：运行 Python 体并保留结构化结果

重启容器的标准计划见 [references/plan-examples.md](references/plan-examples.md)。

## 判断与取舍

**分块大小。** 默认 3 MiB。链路慢或 PTY 吞吐低时调小，可更快暴露问题；大文件可调大以减少往返。传输吞吐会记录在 `throughput_mib_s` 里，据此调整而不要凭感觉。

**是否压缩。** 源码、日志、JSON 一律加 `--compress`。已压缩的归档、图片、模型权重不要加，只会浪费 CPU。

**失败后的动作。** 传输或执行报 `TIMEOUT` 时，先用 `tmux_exec.py` 检查远端实际状态和目标文件，确认没有半成品再重试。所有上传都写临时 `.part-<nonce>` 后才原子改名，因此失败不会留下可用但内容错误的正式文件。

**并发边界。** 同一会话被两个驱动同时使用会交错污染回执，因此每个 `socket + 会话` 有独立文件锁。看到锁超时不要绕过，先查是谁在用。

更多细节按需查阅：

- 传输协议与故障处理：[references/transfer-protocol.md](references/transfer-protocol.md)
- 编排计划示例：[references/plan-examples.md](references/plan-examples.md)
- 常见故障与排查：[references/troubleshooting.md](references/troubleshooting.md)
