# tmux SSH Workflow Skill · 复用已建立 tmux 通道的远程工作流

![GitHub stars](https://img.shields.io/github/stars/stmoonar/tmux-ssh-skill?style=flat-square)
![Skill](https://img.shields.io/badge/Skill-Agent-111111?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)
![tmux](https://img.shields.io/badge/tmux-supported-1f425f?style=flat-square)

一个面向 Claude Code、Codex 及其他本地 Agent 的 tmux 远程工作流 Skill。

它不负责新建 SSH 连接，而是复用本机 tmux 中已经建立并完成鉴权的 SSH 或容器 shell，在远程环境中可靠地执行命令、传输文件、启动和监控长期任务，并支持容器重启后的重新进入与断线接管。

```text
本机 tmux socket → tmux 会话 → 已鉴权的远程 shell → 命令 / 文件 / 任务 / 容器
```

## 30 秒开始

### 安装

推荐使用 `skills` 安装：

```bash
npx skills add https://github.com/stmoonar/tmux-ssh-skill --skill tmux-ssh-workflow
```

也可以手动安装到 Claude Code 的 Skill 目录：

```bash
git clone https://github.com/stmoonar/tmux-ssh-skill.git \
  ~/.claude/skills/tmux-ssh-workflow
```

Windows 上 Claude Code 默认跑在 PowerShell 侧，在 PowerShell 中执行（若 Claude Code 跑在 WSL 里，则在 WSL 中执行上面的命令）：

```powershell
git clone https://github.com/stmoonar/tmux-ssh-skill.git "$env:USERPROFILE\.claude\skills\tmux-ssh-workflow"
```

脚本本身仍需在 WSL 中运行，见下文 [Windows（WSL）](#windowswsl)。

安装完成后，确认目录中包含：

```text
SKILL.md
README.md
references/
scripts/
```

### 触发方式

当本机已经有 tmux socket 和会话，并且需要操作会话中的远程 shell 时，可以直接告诉 Agent：

```text
使用 tmux socket /tmp/team.sock 的 remote-a 会话，检查远程环境并运行训练任务。
```

```text
通过 tmux 会话 node-01 把 ./bundle.zip 传到远程 /workspace/bundle.zip，传完后校验哈希。
```

```text
在 remote-a、remote-b、remote-c 三个会话上执行同一套容器重启、重进和环境检查流程，先 dry-run。
```

## 适合与不适合

### 适合

- 已经存在 tmux socket 和会话，需要复用其中的 SSH 或容器 shell。
- 在一台或多台远程机器上执行命令，并获取结构化结果。
- 通过现有 PTY 通道上传或下载文件，且不希望再次处理 SSH 凭据。
- 启动可能超过本地命令超时的训练、构建、部署或数据处理任务。
- 容器重启、SSH 重连后重新发现 pane，并继续接管远程工作流。
- 多台机器执行有先后依赖的同一组操作。

### 不适合

- 新建 SSH 连接，或管理 host、user、port、跳板机等连接配置。
- 本机没有可用 tmux 通道的远程操作。
- 需要原生 `scp`、`rsync` 或高吞吐文件传输的场景。
- 仅需要管理本地 tmux 会话本身的场景。

## 核心能力

| 能力 | 脚本 | 说明 |
|------|------|------|
| 会话预检 | `scripts/session_preflight.py` | 检查会话、活动 pane、远端 host、用户、cwd 和必需命令 |
| 远程执行 | `scripts/tmux_exec.py` | 在一个或多个会话中并行执行 shell 命令或 Python 结构化检查 |
| 文件传输 | `scripts/transfer.py` | 通过 tmux PTY 上传和下载，支持压缩、分块、SHA256 和原子改名 |
| 长任务管理 | `scripts/remote_job.py` | 启动、查看状态、停止和收集远程长期任务 |
| 批量编排 | `scripts/batch_sessions.py` | 以阶段屏障驱动多会话流程，支持 dry-run、重连等待和结构化检查 |

所有脚本输出 JSON，成功返回退出码 `0`，失败返回非零退出码。多会话操作会在不同会话之间并行，同一会话内通过锁串行执行，避免同一条 PTY 的回执互相污染。

## 设计原则

1. 每次操作都重新发现活动 pane。容器重启或 SSH 重连后，旧 pane 不能继续假定有效。
2. 超时不等于失败。超时只表示回执尚未出现，任务可能仍在运行，禁止自动重发。
3. 长任务状态写在远程 job 目录中，不依赖本地进程或 tmux 回滚缓冲，因此断线后仍可接管。
4. 停止操作只针对远程任务或容器，绝不执行 `kill-session`、`kill-window` 或 `kill-pane`。
5. 批量操作按阶段设置屏障，当前步骤的所有会话完成后才进入下一步。
6. 正式文件路径只在完整校验通过后更新，失败时不留下可用但内容错误的半成品。

## 标准工作流

推荐按以下顺序执行：

1. 使用 `session_preflight.py` 确认会话可用、远端 shell 层级正确、cwd 和命令满足预期。
2. 使用 `transfer.py put` 上传代码、配置或输入数据。
3. 使用 `tmux_exec.py` 执行解包、依赖检查和环境准备。
4. 使用 `remote_job.py launch` 启动长期任务，保存返回的 `pid` 和 `pgid`。
5. 使用 `remote_job.py status` 轮询任务状态，直到 `SUCCEEDED` 或 `FAILED`。
6. 使用 `remote_job.py collect` 在远端打包结果，再用 `transfer.py get` 下载并校验。
7. 需要中止时使用 `remote_job.py stop`，不要直接杀 tmux 会话。

## 环境要求

### 本地

- Python `3.10+`。
- 已安装 `tmux`，并且目标 socket 可访问。
- 至少存在一个已经建立并完成鉴权的 SSH 或容器 shell。
- 不需要额外的 Python 第三方依赖，脚本使用标准库。

### 远端

- 可被当前 pane 使用的类 Unix shell，脚本默认通过 `bash` 执行命令。
- Python `3.8+` 的 `python3`，用于结构化检查、任务状态和文件分片处理；长期任务管理还需 `ps`。
- `base64`、`stty` 等基础命令，用于 PTY 文件传输协议。
- 如果使用容器流程，需要对应的容器运行时命令和权限。

### Windows（WSL）

只支持在 WSL 中运行，tmux socket 和 SSH 会话都要建在 WSL 里；原生 Windows Python 运行脚本会直接退出并提示 `wsl` 命令。

- Agent 跑在 Windows 侧时，从 PowerShell 调用 `wsl -d <发行版> -e python3 "/mnt/c/<skill 路径>/scripts/X.py" ...`；发行版和用户必须与创建 tmux 的环境一致，必要时加 `-u <用户>`。在 Git Bash 中调用时需加 `MSYS_NO_PATHCONV=1`，否则 POSIX 参数会被改写成 `C:/...`，脚本会报错拒绝。
- Claude Code 跑在 WSL 里时，与 Linux 完全相同。
- 本地文件参数可以写 `C:\...`，会自动转成 `/mnt/c/...`；`--socket` 和远程路径必须是 POSIX 路径。
- PowerShell 不展开 `scripts/*.py`、`$((...))` 等 bash 语法，含这些写法的示例请放进 `wsl -e bash -lc '...'` 执行。
- 长传输注意 Agent 工具的默认超时与中断后的检查方式，见 [`SKILL.md`](./SKILL.md) 的 Windows 一节。

## 脚本用法

以下示例假定：

```bash
SOCKET=/tmp/team.sock
SESSIONS=remote-a,remote-b
```

### 1. 会话预检

```bash
python3 scripts/session_preflight.py \
  --socket "$SOCKET" \
  --sessions "$SESSIONS" \
  --expect-cwd-prefix /workspace \
  --require-command python3
```

可以额外使用 `--expect-host-contains` 检查远端主机标识。预检只有在所有会话都通过时才返回成功。

### 2. 执行远程命令

在多个会话上并行执行普通命令：

```bash
python3 scripts/tmux_exec.py \
  --socket "$SOCKET" \
  --sessions "$SESSIONS" \
  --command 'nvidia-smi -L' \
  --show-output
```

需要结构化结果时，把 Python 函数体写入文件。函数体必须以 `return` 结束，返回值必须可以 JSON 序列化：

```python
import os

return {
    "cwd": os.getcwd(),
    "has_workspace": os.path.isdir("/workspace"),
}
```

```bash
python3 scripts/tmux_exec.py \
  --socket "$SOCKET" \
  --sessions remote-a \
  --python-file check_env.py
```

`--command` 和 `--python-file` 必须二选一。使用 `--show-output` 时，脚本会读取并返回远端输出尾部，避免把大量日志全部塞入回执。

### 3. 上传和下载文件

上传单个文件：

```bash
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --sessions "$SESSIONS" \
  --source ./bundle.zip \
  --remote-path /workspace/bundle.zip
```

源码、日志和 JSON 等文本内容通常适合压缩传输：

```bash
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --session remote-a \
  --source ./source.tar \
  --remote-path /workspace/source.tar \
  --compress
```

下载结果：

```bash
python3 scripts/transfer.py get \
  --socket "$SOCKET" \
  --session remote-a \
  --remote-path /workspace/result.tar.gz \
  --dest ./returns/result.tar.gz
```

如果多个会话同时下载到同一个目标路径，脚本会自动按会话名生成前缀，避免覆盖。覆盖已有文件必须显式添加 `--overwrite`。

目录建议先在本地打包成一个归档，再通过 `transfer.py` 传输：

```bash
tar -czf bundle.tar.gz project/
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --session remote-a \
  --source ./bundle.tar.gz \
  --remote-path /workspace/bundle.tar.gz
```

### 4. 启动和管理长期任务

启动任务：

```bash
python3 scripts/remote_job.py launch \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state \
  --cwd /workspace/project \
  --command './train.sh'
```

查询状态和日志尾部：

```bash
python3 scripts/remote_job.py status \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state
```

停止远程任务：

```bash
python3 scripts/remote_job.py stop \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state
```

收集任务目录：

```bash
python3 scripts/remote_job.py collect \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-001 \
  --job-root /workspace/job-state
```

`collect` 会在远端生成任务归档并返回归档路径、字节数和 SHA256。拿到归档路径后，再使用 `transfer.py get` 下载。

任务状态包括：

| 状态 | 含义 |
|------|------|
| `RUNNING` | 进程组内仍有运行的进程，即使主进程已写入退出码 |
| `SUCCEEDED` | 任务退出码为 `0` |
| `FAILED` | 任务退出码非 `0` |
| `LOST` | 进程已不在，但没有可靠退出码，通常需要先查日志 |
| `MISSING` | job 目录不存在，通常是 `job-root` 或 `job-id` 不正确 |

任务重名时，`launch` 默认拒绝覆盖已有 job 目录。只有明确需要复用时才添加 `--reuse`。

如果 pane 本身已经在容器里（例如常驻一个 `docker exec -it <ctr> bash`），直接在这个 pane 上调用 `remote_job.py`，**不要**加 `--container`，也不要为此退出容器：任务会作为普通 shell 任务在容器内运行，`status`、`stop`、`collect` 用法不变。此模式要求容器内有 bash、Python 3.8+ 和 procps 提供的 `ps`（不支持 BusyBox 的 `ps`）；缺少时 `launch` 直接失败且不会启动任务。容器可能被删除重建时，`--job-root` 应放在挂载的持久目录上。

`--container` 只用于 pane 在宿主机、需要在容器里启动和停止任务的场景。此时容器任务从宿主机 shell 启动；`--container` 会通过容器运行时的 `exec` 真正进入指定容器执行命令。`--cwd` 是容器内目录，`--job-root` 是宿主机上的状态目录：

```bash
python3 scripts/remote_job.py launch \
  --socket "$SOCKET" \
  --sessions remote-a \
  --job-id train-in-container \
  --job-root /var/tmp/tmux-job-state \
  --container trainer \
  --cwd /workspace/project \
  --command './train.sh'
```

该任务的 `status`、`stop`、`collect` 也从宿主机调用，并使用同一个宿主机状态目录。停止时脚本先按启动时保存的运行时和容器 ID 停容器，再确认进程组内没有仍运行的子进程。默认运行时为 `docker`，在 `launch` 时可用 `--container-runtime` 修改。旧版只有容器名的任务需在宿主机核对后处理。

## 多会话批量编排

`batch_sessions.py` 适合容器重启、批量部署和多机器环境检查等有先后依赖的场景。

每个步骤声明预期行为：

| `expect` | 用途 | 关键字段 |
|----------|------|----------|
| `marker` | 普通命令，等待回执并检查退出码 | `command`、可选 `allow_rc` |
| `disconnect` | 预期 shell 消失，例如退出容器 | 可选 `command` |
| `ready` | 反复探测直到环境恢复可用 | `command`、`ready_command` |
| `json` | 执行 Python 体并保留结构化结果 | `python` |

容器重启计划示例：

```json
{
  "sessions": ["remote-a", "remote-b", "remote-c"],
  "steps": [
    {
      "name": "exit-container",
      "command": "exit",
      "expect": "disconnect",
      "timeout_seconds": 30
    },
    {
      "name": "restart-container",
      "command": "docker restart trainer",
      "expect": "marker",
      "timeout_seconds": 300
    },
    {
      "name": "reenter-container",
      "command": "docker exec -it trainer /bin/bash",
      "expect": "ready",
      "ready_command": "test -d /workspace",
      "timeout_seconds": 180,
      "settle_seconds": 3
    },
    {
      "name": "verify-environment",
      "expect": "json",
      "python": "import os\nreturn {\"cwd\": os.getcwd(), \"has_workspace\": os.path.isdir(\"/workspace\")}"
    }
  ]
}
```

先干跑，确认步骤和目标会话无误：

```bash
python3 scripts/batch_sessions.py \
  --socket "$SOCKET" \
  --plan restart-plan.json \
  --dry-run
```

确认后执行：

```bash
python3 scripts/batch_sessions.py \
  --socket "$SOCKET" \
  --plan restart-plan.json
```

默认失败即止，避免在部分机器已经变化、部分机器尚未变化的状态上继续。需要允许健康会话继续时，显式使用 `--continue-on-failure`；失败会话会从后续阶段中剔除。

## 文件传输协议

传输只经过 tmux 会话中已经鉴权的 shell，因此不依赖 `scp`、`rsync` 或再次登录。

上传时：

1. 本地将文件按固定大小分块并进行 Base64 编码，按 76 列折行。
2. 远端先关闭回显并发送 `READY`，本地确认读循环已经就绪后才粘贴数据。
3. 每块写入远端临时文件，解码完成后返回分块回执。
4. 全部数据传完后，远端校验 SHA256 和字节数。
5. 校验通过后才将暂存文件原子改名为正式路径。

下载时：

1. 确认没有已有 `pipe-pane` 日志管道，再取得输出管道并触发远端读取。已有管道时拒绝下载并保留原日志。
2. 远端按字节范围切片，逐片计算 SHA256 并编码输出。
3. 本地只接受合法的 Base64 行，并校验每个分片。
4. 全部分片合并后再次校验整文件 SHA256 和字节数。
5. 校验通过后才改名到目标路径。

默认分块大小为 `3 MiB`。可根据链路质量调整：

```bash
python3 scripts/transfer.py put \
  --socket "$SOCKET" \
  --session remote-a \
  --source ./large.json \
  --remote-path /workspace/large.json \
  --compress \
  --chunk-bytes $((1024 * 1024))
```

Base64 会带来约 33% 的体积膨胀，PTY 逐行处理也会限制吞吐。因此：

- 目录和大量小文件先打包成单个归档。
- 源码、日志和 JSON 通常使用 `--compress`。
- 已压缩归档、图片和模型权重不要重复压缩。
- 调参时参考返回结果中的 `throughput_mib_s` 和 `elapsed_seconds`。
- 同一会话内不要并行驱动多个操作。

更完整的时序、参数和失败语义见 [`references/transfer-protocol.md`](./references/transfer-protocol.md)。

## 超时与失败处理

### `TIMEOUT` 不代表命令失败

超时只表示在限定时间内没有看到回执。命令可能仍然在远端运行。正确做法是：

1. 使用 `tmux -S SOCK capture-pane -p -t SESSION` 只读查看 pane，确认 shell 已空闲；必要时有针对性地退出传输读循环或交互程序。
2. 超时或驱动被终止后，未完成标记会阻止后续写入。确认现场后，用 `tmux_exec.py --socket SOCK --sessions SESSION --recover-session --command 'stty echo; pwd' --show-output` 显式恢复。
3. 恢复后用 `tmux_exec.py` 检查进程、文件或日志，或用 `remote_job.py status` 查看任务状态，再决定是否重试。

不要在没有检查远端状态时直接重发，尤其是训练、部署和数据库变更类命令。

### 常见问题

| 现象 | 可能原因 | 首要处理 |
|------|----------|----------|
| `session not found on socket` | socket 或会话名错误 | 执行 `tmux -S SOCK list-sessions` 核对实际名称 |
| `session has no panes` | 会话残留但 pane 已销毁 | 在目标会话中人工重建 shell |
| `remote reader not ready` | shell 已退出或卡在交互程序 | 退出分页器、编辑器或密码提示后再试 |
| 下载没有合法 marker | pane 有持续后台输出干扰 | 先让 pane 安静，再重试下载 |
| `wire mismatch` | 传输中的字节或链路损坏 | 临时文件会清理，确认链路后重新传输 |
| `payload mismatch` | 解压后内容校验失败 | 正式路径未被触碰，检查源文件后重新传输 |
| `LOST` | 进程被外部终止或机器重启 | 先查看完整日志，不要直接重跑 |
| 锁等待超时 | 同一会话已有其他驱动占用 | 查清占用者，不要绕过锁 |

故障排查顺序和更多案例见 [`references/troubleshooting.md`](./references/troubleshooting.md)。

## 目录结构

```text
tmux-ssh-skill/
├── SKILL.md                         # Skill 主文件：触发规则、原则和标准工作流
├── README.md                        # 项目说明和使用手册
├── references/
│   ├── plan-examples.md             # 批量编排计划、容器重启和部署示例
│   ├── transfer-protocol.md         # Base64 分块传输协议与性能说明
│   └── troubleshooting.md           # 会话、执行、传输和任务故障排查
├── scripts/
│   ├── batch_sessions.py            # 多会话阶段编排器
│   ├── remote_job.py                # 远程长期任务管理器
│   ├── session_preflight.py         # 会话预检器
│   ├── tmux_exec.py                 # 远程命令和 Python 结构化执行器
│   ├── tmuxlib.py                   # tmux、回执、锁和远程执行基础库
│   └── transfer.py                  # 基于 tmux PTY 的文件传输器
└── tests/
    └── test_workflow.py             # 基于一次性 tmux server 的端到端回归测试
```

## 常用场景

### 远程代码运行

```text
1. preflight 检查 cwd 和 python3
2. transfer put 上传归档
3. tmux_exec 解包并检查依赖
4. remote_job launch 启动任务
5. remote_job status 轮询状态
6. remote_job collect 和 transfer get 收集结果
```

### 容器重启后接管

```text
1. batch_sessions 以 disconnect 退出容器
2. 在宿主机执行容器重启
3. 以 ready 轮询容器重新进入后的可用状态
4. 重新发现 pane 并执行环境检查
```

### 多机器一致性检查

```text
1. 使用 sessions 指定多个目标
2. 使用 tmux_exec 执行同一条检查命令
3. 使用 --show-output 获取每台机器的输出尾部
4. 根据 JSON 中的 problem_sessions 定位异常会话
```

## 参考文档

- [`SKILL.md`](./SKILL.md)：触发条件、核心规则和标准工作流。
- [`references/plan-examples.md`](./references/plan-examples.md)：批量计划结构、容器重启和部署示例。
- [`references/transfer-protocol.md`](./references/transfer-protocol.md)：传输时序、性能参数和失败语义。
- [`references/troubleshooting.md`](./references/troubleshooting.md)：常见故障和排查顺序。

## 开发与验证

修改脚本或文档后，可以先执行 Python 语法检查：

```bash
python3 -m py_compile scripts/*.py
```

### 测试

回归测试只用标准库 `unittest`，在 Linux、macOS 或 WSL 中从仓库根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

每个用例在临时目录的 socket 上启动一次性 tmux server，用 `bash --norc --noprofile` 模拟远端 shell，调用真实脚本并断言 JSON 输出，结束时只关闭该 server 并清理临时文件。本机没有 tmux 时整套测试自动跳过；完整运行约需 1～2 分钟。

批量计划修改后，先使用 `--dry-run` 检查步骤，不要直接把包含重启、退出容器或删除文件的计划发送到远端。

本项目的命令会在远程环境中真实执行。使用前请确认 socket、会话名、远程 cwd、容器名和目标路径均正确；尤其要审查包含 `rm`、重启、停止服务或覆盖文件的命令。

## 贡献

欢迎通过 Issue 或 Pull Request 改进文档、传输稳定性、任务状态处理和批量编排能力。

提交改动时建议同步检查：

- `SKILL.md` 中的行为描述是否与脚本实现一致。
- README 中的命令参数是否仍然有效。
- 新增脚本是否只依赖 Python 标准库，或明确补充依赖说明。
- 传输协议和故障语义变化时，是否同步更新 `references/transfer-protocol.md` 与 `references/troubleshooting.md`。
- 执行 `python3 -m py_compile scripts/*.py`，并对远程破坏性计划先执行 `--dry-run`。
