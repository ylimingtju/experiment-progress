"""Fifteen timed demo tasks in two groups; one fails, one succeeds on retry. No research computation."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from experiment_progress import task


def experiment(i, attempt=1):
    group = "小规模" if i <= 8 else "大规模"
    with task(f"demo-{i:02d}", group=group):
        print(f"开始 demo-{i:02d}（{group}，第 {attempt} 次）", flush=True)
        time.sleep((3 if i <= 8 else 5) + (i % 3) * 0.2)
        if i == 11:
            raise ValueError("故意失败，用于检查失败计数")
        if i == 6 and attempt == 1:
            raise TimeoutError("首次故意超时，随后用同一 ID 重试")
        print(f"完成 demo-{i:02d}", flush=True)


def run(i):
    try:
        experiment(i)
    except TimeoutError as exc:
        print("警告：" + str(exc), flush=True)
        experiment(i, attempt=2)


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(run, i) for i in range(1, 16)]):
            try:
                future.result()
            except ValueError as exc:
                print(str(exc), flush=True)
    print("模拟批次结束。预期为成功 14 项、失败 1 项，demo-06 重试 1 次。", flush=True)
