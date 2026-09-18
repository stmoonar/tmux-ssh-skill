# 编排计划示例

目录：计划结构 / 步骤类型 / 容器重启 / 部署与运行 / 执行语义

## 计划结构

```json
{
  "sessions": ["n1", "n2", "n3"],
  "steps": [
    {"name": "步骤名", "command": "远程命令", "expect": "marker"}
  ]
}
```

`--sessions` 可在命令行覆盖计划里的会话列表。每个步骤在所有会话上并行执行，**全部会话完成当前步骤后才进入下一步**。这个屏障是批量操作能安全推进的前提。

## 步骤类型

| `expect` | 用途 | 必需字段 | 判定方式 |
|---|---|---|---|
| `marker` | 普通命令 | `command` | `rc` 落在 `allow_rc`（默认 `[0]`）内 |
| `disconnect` | 预期 shell 消失 | 可选 `command` | 检测到 shell 身份变化或 pane 消失 |
| `ready` | 等待恢复可用 | `ready_command` | 反复探测直到返回 0 |
| `json` | 结构化检查 | `python` | Python 体正常返回 |

可选字段：`timeout_seconds`、`allow_rc`、`settle_seconds`（`ready` 步骤发送命令后的静置时间）、`poll_seconds`（探测间隔）。

## 容器重启

这是本 skill 最典型的用途：多台机器同时退出容器、重启、再重新进入。

```json
{
  "sessions": ["n1", "n2", "n3", "n4"],
  "steps": [
    {
      "name": "exit-container",
      "command": "exit",
      "expect": "disconnect",
      "timeout_seconds": 30
    },
    {
      "name": "restart-container",
      "command": "sudo docker restart myctr",
      "expect": "marker",
      "timeout_seconds": 300
    },
    {
      "name": "reenter-container",
      "command": "sudo docker exec -it myctr /bin/bash",
      "expect": "ready",
      "ready_command": "test -d /workspace",
      "timeout_seconds": 180,
      "settle_seconds": 3
    },
    {
      "name": "verify-env",
      "python": "import os\nreturn {'cwd': os.getcwd(), 'has_workspace': os.path.isdir('/workspace')}",
      "expect": "json"
    }
  ]
}
```

三个关键点：

- 退出容器用 `disconnect`。shell 本来就要消失，用 `marker` 等回执必然超时。
- 重启命令在宿主机执行，此时 pane 已回到宿主机 shell，所以用 `marker`。
- 重进用 `ready`。容器内服务未必立刻就绪，反复探测比固定 `sleep` 可靠。

`disconnect` 的检测方式是让 shell 报告自己的 PID 并比对变化。tmux 只跟踪最外层 pane 进程，嵌套 shell 或容器 shell 退出时 `pane_pid` 不变，因此必须问 shell 本身。

## 部署与运行

```json
{
  "sessions": ["n1", "n2"],
  "steps": [
    {"name": "clean", "command": "rm -rf /remote/project/build", "expect": "marker"},
    {"name": "unpack", "command": "cd /remote/project && unzip -o bundle.zip", "expect": "marker"},
    {"name": "deps", "command": "cd /remote/project && python3 -m pip install -q -r requirements.txt", "expect": "marker", "timeout_seconds": 600},
    {"name": "smoke", "command": "cd /remote/project && python3 -c 'import app'", "expect": "marker"},
    {
      "name": "record",
      "python": "import hashlib,os\nroot='/remote/project'\nfiles=sorted(f for f in os.listdir(root) if f.endswith('.py'))\nreturn {'file_count': len(files)}",
      "expect": "json"
    }
  ]
}
```

归档本身用 `transfer.py put` 事先送到各台机器，计划只负责解包与验证。

## 执行语义

**先干跑。** `--dry-run` 只打印将要执行的步骤，不接触任何会话。改动过计划就先干跑一次。

**失败即止。** 默认某会话某步骤失败就停止整个计划，避免在不一致状态上继续。加 `--continue-on-failure` 会剔除失败会话、让其余会话继续，适合允许部分机器掉队的场景。

**返回结构。** 输出按步骤分组，每组含状态、失败会话列表和每会话明细，可直接判断是哪台机器的哪一步出了问题。

**破坏性命令。** 计划里的命令会真实执行。涉及 `rm -rf`、重启、停服务时，先确认路径与目标，并优先跑一次 `--dry-run` 复核。
