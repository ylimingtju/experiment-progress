---
name: experiment-progress
description: 为本机长时间实验、批量计算、参数扫描和基准测试自动接入实时进度网页，显示实验总数、成功失败计数、运行时间和 ETA；支持后台启动、观察已有进程和查看历史记录。准备启动预计持续数分钟以上的实验时使用，即使用户没有再次要求进度条。短小单元测试、普通文件操作和远程服务器实验不默认接入。
---

# 实验进度

将长实验接入本机面板，并保留已有静默跟进。使用本 skill 目录中的 `scripts/experiment_progress.py`。Python 3.9+，无第三方依赖。首版以 macOS 实测为准；Linux 待验证，Windows 暂不支持（使用 `fcntl` 和 `ps`）。面板及数据仅在本机，服务按需启动；不是操作系统级进程自动探测器。

## 启动前

- 先检查实验入口、任务清单、并发方式和日志格式。复用已有状态输出；不改模型、随机种子、算法、求解时限、调度顺序或结果判定。
- 定义一个实验 ID 对应的工作单元，如“算法×实例×参数×种子”。生成已知任务清单，重试沿用同一 ID，避免重复计数。总量未知时省略，不能从完成文件数量猜总量。
- 优先让已有驱动程序调用下面的 `task()`；若日志已经明确记载每个实例的开始、结束及结果，可写一个只读日志适配器，调用 `report()`。只根据原日志明确状态上报，支持 `started_at` / `ended_at` Unix 秒时间戳。日志重复读取不会重复计数；任务结束事件缺开始时间时不纳入 ETA 样本。
- 仅接入无任务事件的命令也可显示进程、日志和计时，但必须说明实验计数尚未接入。不能将整个批次登记为“一次实验”冒充内部实例计数。

## Python 驱动程序接入

将 skill 的 `scripts/` 目录加入该驱动的模块搜索路径，然后在原有任务边界加入以下记录；原先已有可靠结果判断时优先直接调用 `report()`。

```python
from experiment_progress import task, report

with task("algorithm-instance-parameter-seed", group="同类规模组"):
    result = run_existing_experiment()
    # 如果原驱动把失败编码为返回值，应沿用其判定并报告 failed，不能仅按无异常认定成功。
```

`task()` 在进入时记录 running，正常退出时记录 succeeded，出现异常时记录 failed 并重新抛出原异常。监控写入失败只输出警告，不改变实验的异常传播。对返回状态码、超时结果等情况用 `report(task_id, "running")` 和 `report(task_id, "succeeded"或"failed")`，由原实验判定规则决定结果；求解器达到时限可能是有效实验结果，不能一律归为运行失败。进度记录包在整个单次试验外，保持原求解计时区间。

多进程中每个 worker 可以独立报告；SQLite 事务负责合并。`EXPERIMENT_PROGRESS_RUN` 和 `EXPERIMENT_PROGRESS_HOME` 由启动器传给子进程。不支持通过网页启动、终止或重试实验。

## 命令

以下 `EP` 按会话技能目录中的实际安装位置填写，不假设固定用户目录。保持完整引用；不要修改 HOME 或 CODEX_HOME。

```sh
EP="/absolute/path/to/experiment-progress/scripts/experiment_progress.py"
python3 "$EP" launch --name "参数对比实验" --cwd "/absolute/project" --plan "/absolute/project/work/plan.json" -- python3 -u driver.py
```

计划格式：`[{"id":"case-001","group":"small"},{"id":"case-002","group":"large"}]`。总数取计划长度；没有逐项清单时可改用 `--total 120`，但多组 ETA 需要完整分组计划。

`launch` 后台启动前台实验命令，返回 `run_id`、本机 `url` 和数据位置。必须让被启动的驱动等待全部 worker 结束，不能再用 `nohup`、`&` 或只提交工作后就退出。保留原工作目录、解释器、环境及参数；不要为展示进度另跑一份实验。标准输出和错误写入该轮日志，父命令退出后实验继续运行。计算机休眠会影响实际运行及 ETA。

启动后用 `open_in_codex` 打开返回 URL；工具不可用时用浏览器打开，或 `dashboard --open`。向用户提供面板链接，检查一次 `status` 确认已接入。自动打开失败不等于实验启动失败，不要重复 launch。

```sh
python3 "$EP" dashboard --open
python3 "$EP" status --run RUN_ID
python3 "$EP" event running --run RUN_ID --task case-001 --group small
python3 "$EP" event succeeded --run RUN_ID --task case-001
python3 "$EP" attach --name "已有实验" --pid PID --cwd "/absolute/project" --total 120 --log "/absolute/project/run.log"
```

`attach` 只观察已有 PID，以其原始启动时间计时；它无法取得原进程退出码，不自动把退出判为成功。不会读取或解析任意日志中的实验次数，必须另接明确的任务事件或只读适配器。显示日志限最后约 8 KB，任务明细各限 30 项，轮次列表限最近 50 轮；完整状态仍保存在数据库。

## 数值与异常

- 成功和失败分开，已结束包含两者。重复结束事件幂等；同一任务重试要先重新报告 running。计划内未报告任务保留为等待/未报告。退出码 0 不足以证明所有实验完成。
- ETA 按每个剩余组最近至多 30 个成功样本的 25%–75% 分位耗时和当前并发数估算，扣除在跑任务已耗时。假定剩余任务与分组样本相似且并发不变；该区间不是置信区间或理论保证。有依赖、动态增减 worker、求解耗时长尾时可能不适用。
- 每个剩余组少于 3 个有计时样本、未知总量、缺少剩余分组清单或任务超出估算范围时不显示 ETA。不要把实例完成比例解释为耗时比例；也不要把最优性 gap 转成时间进度。
- 独立监测进程每 2 秒写心跳；12 秒无信号标记状态待核验。网页读取失败保留最后数据并提示。观察不到更新不等于实验失败。
- 保留已有静默跟进，仍按用户原有通知偏好处理完成、失败及需要操作的状态；不为刷新数字增加模型轮询或重复的自动跟进。

默认状态目录为 `~/.codex/experiment-progress/`，包含数据库、轮次日志和服务信息。测试可设置 `EXPERIMENT_PROGRESS_HOME` 为隔离目录。全局安装只改变进度接入工作流，不扩大科研代码修改或对外操作的授权。

验证：`python3 scripts/test_monitor.py`。演示任务：用 `launch --demo --total 15 --name "面板验证" -- python3 /absolute/skill/scripts/demo.py`；演示包含一个故意失败的任务。
