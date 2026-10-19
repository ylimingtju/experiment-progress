# 实验进度 Skill

[English](README.en.md) · [MIT 许可](LICENSE)

为本机后台实验提供自动刷新的网页面板，显示任务矩阵、完成曲线、分组耗时，以及条件满足时的剩余时间估计。可由 Codex 接入，也可直接通过命令行与 Python 使用。

![v1.0.0 面板：15 个模拟任务、任务矩阵和完成曲线](docs/dashboard.jpg)

截图来自新版面板的 15 个模拟任务，分为两组，包含一次同 ID 重试和一次故意失败；结束时应为成功 14 项、失败 1 项。

任务矩阵支持按 ID 搜索、悬停和方向键查看。面板还提供完成曲线、启发式预测范围、分组耗时分位数、亮暗主题和动效开关；浏览器结束提醒可按需开启。

## 环境

- Python 3.9+，仅使用标准库，不需要 pip 安装。
- 已在 macOS 上验证。Linux 待验证；Windows 暂不支持，代码使用 `fcntl` 和 `ps`。
- 需要支持 `fetch` 和 `AbortSignal.timeout` 的现代浏览器。界面和运行消息目前为中文；英文用法见英文 README。
- 实验在本机运行。不提供远程服务器接入、网页终止或重试按钮。

## 在 Codex 安装和使用

```text
$skill-installer 从 https://github.com/ylimingtju/experiment-progress/tree/v1.0.0/skills/experiment-progress 安装这个 skill。
```

```text
$experiment-progress 为这轮后台实验接入网页进度。先检查现有任务清单和日志，保留模型、算法、随机种子、求解时限与实验计时定义，并沿用已有通知偏好。
```

安装 skill 后，Codex 在任务匹配时可以主动选择它。它不是操作系统级自动探测器，不会给任意后台程序自动算出实验次数。完整计数需要稳定任务 ID 和明确的任务状态事件；有可靠日志时可只读适配已有日志。不得为了接入重复运行实验。

安装和发现方式见 [OpenAI 官方说明](https://learn.chatgpt.com/docs/build-skills)。可安装到宿主支持的 skill 目录；不要覆盖已有同名 skill 的修改。

## 命令行快速演示

```sh
git clone https://github.com/ylimingtju/experiment-progress.git
cd experiment-progress
EP="$PWD/skills/experiment-progress/scripts/experiment_progress.py"
export EXPERIMENT_PROGRESS_HOME="$(mktemp -d)"
python3 "$EP" launch --demo --name "模拟实验" --total 15 --open -- python3 "$PWD/skills/experiment-progress/scripts/demo.py"
```

演示约 25 秒，使用临时状态目录，不运行科研计算；分两组，包含一次故意失败和一次同 ID 重试。启动器打印 `run_id`、本机 `url` 和 `data_dir`。`--open` 打开浏览器；打开失败时使用返回地址，不要重复启动。退出此 shell 后可通过打印的数据目录找回演示记录。

要用于真实实验，去掉 `--demo`，替换工作目录和实验命令，并采用实际任务清单。示例：

```sh
python3 "$EP" launch --name "参数对比" --cwd /absolute/project --plan /absolute/project/work/plan.json -- python3 -u driver.py
```

计划文件是 JSON 数组：

```json
[
  {"id":"bo01/30-10-2/seed0","group":"n=30","params":{"algo":"bo01","inst":"30-10-2","seed":0}},
  {"id":"bo02/60-10-2/seed0","group":"n=60","params":{"algo":"bo02","inst":"60-10-2","seed":0}}
]
```

总数取清单长度。`params` 可省略，至多 20 项，值为字符串或数值，仅用于 ETA；省略时从 ID 的分隔段提取参数，建议用 `/` 分隔。没有逐项清单时可改用 `--total 120`，未报告任务按当前唯一组或本轮总体样本粗估；已知分组时仍应提供完整计划。总量未知时省略。启动命令必须等待全部 worker 结束，不能提交工作后就退出，也不要再加 `nohup` 或 `&`。

## 接入任务事件

把本 skill 的 `scripts/` 目录加入实验解释器的模块搜索路径，例如在启动器前设置 `PYTHONPATH`，再在原任务边界记录：

```python
from experiment_progress import task, report

with task("algorithm-instance-parameter-seed", group="small"):
    run_existing_experiment()
```

`task()` 正常退出记成功，异常记失败并继续抛出原异常；监控记录错误仅警告，不改变实验结果。若原程序以返回值表示成功或失败，使用 `report()` 显式遵循原判定规则。求解器达到时限可以是有效实验结果，不应一律视为执行失败。进度记录不要改变求解计时区间。

启动器向子进程传递 `EXPERIMENT_PROGRESS_RUN` 和 `EXPERIMENT_PROGRESS_HOME`。重试沿用同一任务 ID，先报告 `running`；重复结束事件不会重复计数。多进程写入由 SQLite 事务合并。

## 常用命令

```sh
python3 "$EP" dashboard --open
python3 "$EP" dashboard --restart --open
python3 "$EP" status --run RUN_ID --brief
python3 "$EP" list --limit 20
python3 "$EP" backtest --run RUN_ID
python3 "$EP" event running --run RUN_ID --task case-001 --group small
python3 "$EP" event succeeded --run RUN_ID --task case-001
python3 "$EP" attach --name "已有实验" --pid PID --cwd /absolute/project --total 120 --log /absolute/project/run.log
```

`status` 去掉 `--brief` 可获取完整 JSON。`dashboard --restart` 仅重新载入面板服务，保留原链接，不重启实验。`backtest` 只读回放有至少 10 个计时任务的已结束轮次，报告范围覆盖比例、点估计中位倍数偏差和范围宽度；省略 `--run` 时检查最近 20 个已结束轮次。回放依赖记录的起止时间及当前保存的参数和预估，不能保证未来 ETA 准确。

`attach` 只观察已有进程，不重启。它不能获取原进程退出码，也不会自动解析任意日志来推断任务结果；需要另接事件或只读适配器。进程退出显示退出结果未知，不能据此声称全部实验成功。

## 提供初始耗时预估

有可比的历史记录、试跑记录或用户预估时，可在启动或观察进程时提供单次实验的耗时与依据。参数须放在实验命令分隔符 `--` 前；以下 20 秒只是参数示例，实际使用需替换为有依据的值：

```sh
python3 "$EP" launch --name "参数对比" --cwd /absolute/project --plan /absolute/project/work/plan.json --estimate-seconds 20 --estimate-source "请替换为当前项目相同设置的真实依据" -- python3 -u driver.py
python3 "$EP" set-prior --run RUN_ID --seconds 20 --source "请替换为真实依据"
python3 "$EP" set-prior --run RUN_ID --group small --seconds 20 --source "请替换为同组真实依据"
```

`set-prior` 可在已启动轮次中补入或更新预估，不重启实验。省略 `--group` 时作为尚无同组样本的组的共同预估；出现同组有效样本后优先采用样本。`--estimate-seconds` 与 `--estimate-source` 必须成对使用，`attach` 同样支持。不要把整批耗时或求解时限当作单次平均耗时，也不要为估时额外运行实验或自动混入其他项目记录。

## ETA、记录与访问

- 已结束等于成功加失败；退出码为 0 不足以证明全部任务完成。等待或未报告数量会保留。
- ETA 同时给出点估计和启发式范围。同组有 1–2 个计时成功样本时显示“初步估计”，范围放宽到 0.5–2 倍；单项耗时采用最近至多 120 个同组成功样本的近期加权平均，半衰期为 30 个样本。失败任务按其自身耗时与失败比例计入，不混入成功耗时。
- 计划参数或 ID 分隔段用于对数加性耗时校正。相同取值的已完成任务偏快或偏慢时，相应调整剩余任务；少见取值向零收缩，未见取值使用该参数近期任务的效应。
- 没有同组样本时，依次使用有来源的分组或全局预估、规模外推、相近组借用、跨组平均。规模外推要求组名仅相差一个数值，且同族至少两组有样本；这些方法都可能有较大偏差，出现同组样本后自动替换。
- 在跑任务按比其已耗时更长的同组样本估计剩余；超过几乎所有样本时使用已耗时的一半并标注“动态粗估”。按当前并发数和等待清单贪心排队，范围结合耗时漂移、单项波动与推算方式。范围不是置信区间、准确度保证或完成时限。
- 未知总量、没有运行中任务、心跳中断，或既无有效计时成功样本也无预估时仍显示未知。缺起止时间或零耗时事件不作为耗时样本。
- 当前估计假设任务独立、同组耗时可比较、并发不变。依赖关系、动态 worker、长尾求解时间和计算机休眠均可能使估计不适用。完成数量比例不等于耗时比例。
- 网页每 2 秒读取本机服务，刷新不调用模型。心跳中断或读取失败会保留最后数据并提示，不据此把实验判为失败。
- 默认状态保存在 `~/.codex/experiment-progress/`；可用 `EXPERIMENT_PROGRESS_HOME` 指定隔离目录。网页显示最近 50 轮，任务矩阵包含全部已登记任务；运行中与失败明细各限 30 项，日志限最后约 8 KB，完整状态仍在本地数据库。
- 服务仅监听 `127.0.0.1`，使用临时访问令牌和 Host/Origin 校验。不要公开面板 URL、`server.json`、真实实验日志或数据库。仓库不需要这些运行文件。

## 验证与反馈

```sh
python3 -B skills/experiment-progress/scripts/test_monitor.py
```

现有测试覆盖计数、重试、并发写入、旧版 SQL 兼容、任务排序、分组统计、参数与规模 ETA、模拟数据回放、面板重启与地址保留、心跳过期、HTTP 访问、后台启动、失败退出和已有进程观察；使用隔离临时目录并结束测试服务。模拟数据检查不代表真实实验的 ETA 准确度，也不验证科研结论或 Linux、Windows 兼容性。

## 从旧版更新

当前版本为 `v1.0.0`；`v0.1.0` 与 `v0.2.0` 标签保留。更新 skill 前保留自定义改动，不需要删除状态目录。读取数据库时按需新增 `task_params` 表，保留已有 `runs`、`tasks` 与 `eta_priors`；旧版本实验进程可继续写入。

已经启动的服务不会因替换文件自动更新。安装后运行 `python3 "$EP" dashboard --restart --open` 载入新页面；这只重启网页服务，保留链接和访问令牌，不影响实验或其监测进程。

反馈请附操作系统、Python 版本、最小模拟任务、预期和实际状态，去掉访问令牌、个人路径及未公开数据。

作者：[Liming Yang](https://github.com/ylimingtju)。[MIT 许可](LICENSE)。
