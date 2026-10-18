# 实验进度 Skill

[English](README.en.md) · [MIT 许可](LICENSE)

为本机后台实验提供自动刷新的网页面板，显示成功、失败、运行中、等待或未报告数量，以及耗时和条件满足时的剩余时间估计。可由 Codex 接入，也可直接通过命令行与 Python 使用。

![使用 15 个模拟任务生成的面板](docs/dashboard.png)

截图仅含模拟任务。演示批次结束时应为成功 14 项、失败 1 项。

## 环境

- Python 3.9+，仅使用标准库，不需要 pip 安装。
- 首版在 macOS 上验证。Linux 待验证；Windows 暂不支持，代码使用 `fcntl` 和 `ps`。
- 需要支持 `fetch` 和 `AbortSignal.timeout` 的现代浏览器。界面和运行消息目前为中文；英文用法见英文 README。
- 实验在本机运行。首版不提供远程服务器接入、网页终止或重试按钮。

## 在 Codex 安装和使用

```text
$skill-installer 从 https://github.com/ylimingtju/experiment-progress/tree/v0.1.0/skills/experiment-progress 安装这个 skill。
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

演示约 20 秒，使用临时状态目录，不运行科研计算；包含一次故意失败。启动器打印 `run_id`、本机 `url` 和 `data_dir`。`--open` 打开浏览器；打开失败时使用返回地址，不要重复启动。退出此 shell 后可通过打印的数据目录找回演示记录。

要用于真实实验，去掉 `--demo`，替换工作目录和实验命令，并采用实际任务清单。示例：

```sh
python3 "$EP" launch --name "参数对比" --cwd /absolute/project --plan /absolute/project/work/plan.json -- python3 -u driver.py
```

计划文件是 JSON 数组：

```json
[{"id":"case-001","group":"small"},{"id":"case-002","group":"large"}]
```

总数取清单长度。没有逐项清单时可改用 `--total 120`，总量未知时省略。启动命令必须等待全部 worker 结束，不能提交工作后就退出，也不要再加 `nohup` 或 `&`。

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
python3 "$EP" status --run RUN_ID
python3 "$EP" event running --run RUN_ID --task case-001 --group small
python3 "$EP" event succeeded --run RUN_ID --task case-001
python3 "$EP" attach --name "已有实验" --pid PID --cwd /absolute/project --total 120 --log /absolute/project/run.log
```

`attach` 只观察已有进程，不重启。它不能获取原进程退出码，也不会自动解析任意日志来推断任务结果；需要另接事件或只读适配器。进程退出显示退出结果未知，不能据此声称全部实验成功。

## ETA、记录与访问

- 已结束等于成功加失败；退出码为 0 不足以证明全部任务完成。等待或未报告数量会保留。
- ETA 使用剩余各组最近至多 30 个成功样本的 25%–75% 分位耗时，并按当前并发数估算。需要每个剩余组至少 3 个有计时样本；总量或分组信息不足、任务耗时超出估算范围时显示未知。该区间不是置信区间或理论保证。
- 当前估计假设任务独立、同组耗时可比较、并发不变。依赖关系、动态 worker、长尾求解时间和计算机休眠均可能使估计不适用。完成数量比例不等于耗时比例。
- 网页每 2 秒读取本机服务，刷新不调用模型。心跳中断或读取失败会保留最后数据并提示，不据此把实验判为失败。
- 默认状态保存在 `~/.codex/experiment-progress/`；可用 `EXPERIMENT_PROGRESS_HOME` 指定隔离目录。网页仅显示最近 50 轮、每类最多 30 个任务明细、最后约 8 KB 日志，完整状态仍在本地数据库。
- 服务仅监听 `127.0.0.1`，使用临时访问令牌和 Host/Origin 校验。不要公开面板 URL、`server.json`、真实实验日志或数据库。仓库不需要这些运行文件。

## 验证与反馈

```sh
python3 -B skills/experiment-progress/scripts/test_monitor.py
```

现有测试覆盖计数、重试、并发写入、ETA、心跳过期、HTTP 访问、后台启动、失败退出和已有进程观察；使用隔离临时目录并结束测试服务。它不验证具体科研实验的科学正确性，也不代表 Linux 或 Windows 兼容性已获验证。

首发标签为 `v0.1.0`。反馈请附操作系统、Python 版本、最小模拟任务、预期和实际状态，去掉访问令牌、个人路径及未公开数据。

作者：[Liming Yang](https://github.com/ylimingtju)。[MIT 许可](LICENSE)。
