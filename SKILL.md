---
name: tmux-ssh-workflow
description: 通过本机指定的 tmux socket 和一个或多个 tmux 会话名，复用会话中已鉴权的 SSH 或容器 shell，执行远程命令、传输文件、管理长期任务及编排容器重启。文件传输带下载分块与上传及下载整文件 SHA256 校验、原子改名。适用于用户提供 tmux socket 与会话名、要求运行远程任务或收取产物，以及跨多会话同步操作。不适用于新建 SSH 连接或本地无 tmux 通道的场景。macOS 原生运行，Windows 通过 WSL 运行。
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

## macOS / Linux

本地需 Python 3.10+、tmux 和可访问的 socket；脚本原生运行。远端需 Python 3.8+、bash、base64、stty；管理长期任务还需 `ps`。macOS 本地路径和远端任务路径含空格时，要在调用命令中引用完整参数。

## Windows

只支持在 WSL 中运行：tmux socket 和其中的 SSH/容器会话都必须建在 WSL 里。原生 Windows Python 连不上 WSL 的 socket，脚本会直接退出并提示对应的 `wsl` 命令。

- **Agent 跑在 Windows 侧**：优先用 PowerShell 调用 `wsl -d <发行版> -e python3 "/mnt/c/<skill 路径>/scripts/X.py" ...`。发行版和用户必须与创建 tmux 的环境一致；必要时加 `-u <用户>`。若用 Git Bash，命令前必须加 `MSYS_NO_PATHCONV=1`，否则 `/tmp/x` 等参数会被改写成 `C:/...`；脚本会拒绝被改写的 socket 和远程路径。
- **Claude Code 跑在 WSL 里时**：直接按下文 Linux 用法调用，无需 `wsl -e`。
- **路径**：本地文件参数（`--source`、`--dest`、`--python-file`、`--plan`）可以直接写 `C:\...`，会自动转成 `/mnt/c/...`；远程路径必须是 POSIX 路径。
- **超时**：按当前 Agent 工具的实际超时设置，为长传输留足时间。调用被中断后先只读查看 pane；若停在传输读循环里，确认可以中断后用 Ctrl-C 退出，再按下文恢复会话。

## 标准流程

按顺序执行，除有明确理由外不要跳步。

1. 用 `session_preflight.py` 确认会话可用、远端 shell 位置正确、所需命令存在。
   会话本应在容器内（如常驻 `docker exec -it <ctr> bash`）时加 `--expect-in-container`，容器重启后 pane 掉回宿主机 shell 会被拦下。
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

**pane 已在容器内**（如常驻 `docker exec -it <ctr> bash`）时，直接调用 `remote_job.py`、**不加** `--container`，不要为此退出容器：任务作为普通 shell 任务在容器内运行，`status`、`stop` 照常。此时容器内需有 bash、python3 ≥ 3.8 和 procps 的 `ps`（不支持 BusyBox `ps`）；容器可能被重建时，`--job-root` 放在挂载的持久目录。

`--container` 只用于 pane 在宿主机、要在容器里启停任务的情况，必须从**宿主机 shell** 调用 `launch --container NAME`：脚本通过容器运行时的 `exec` 启动命令，`--cwd` 是容器内目录，`--job-root` 是宿主机状态目录。后续 `status`、`stop`、`collect` 都在宿主机调用。`stop` 按启动时记录的容器 ID 和运行时停容器，再确认宿主机进程组已退出。旧版仅记录容器名的任务不能自动停止，应从宿主机核对后处理。

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

**失败后的动作。** 超时或驱动被终止后，会话保留未完成标记，后续脚本拒绝继续写入。先用 `tmux -S SOCK capture-pane -p -t SESSION` 只读查看现场，确认 shell 已空闲；必要时有针对性地退出读循环或交互程序。然后显式恢复并检查：

```bash
python3 scripts/tmux_exec.py --socket SOCK --sessions SESSION \
  --recover-session --command 'stty echo; pwd' --show-output
```

`--recover-session` 是对“已检查且 shell 空闲”的确认，不会自动中止任务或重发旧命令。恢复后再检查进程、文件和日志，决定是否重试。上传通过暂存文件校验后原子改名。

**下载与终端日志。** 下载需要独占 pane 的输出管道；已有 `pipe-pane` 日志时拒绝下载，保留原管道。换用没有输出管道的 pane，或在明确授权后调整原日志设置。

**并发边界。** 同一会话被两个驱动同时使用会交错污染回执，因此每个 `socket + 会话` 有独立文件锁。看到锁超时不要绕过，先查是谁在用。

更多细节按需查阅：

- 传输协议与故障处理：[references/transfer-protocol.md](references/transfer-protocol.md)
- 编排计划示例：[references/plan-examples.md](references/plan-examples.md)
- 常见故障与排查：[references/troubleshooting.md](references/troubleshooting.md)
