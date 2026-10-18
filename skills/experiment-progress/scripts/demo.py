"""Fifteen timed demo tasks; one intentionally fails. No research computation."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from experiment_progress import task


def experiment(i):
    with task(f"demo-{i:02d}", group="示例组"):
        print(f"开始 demo-{i:02d}", flush=True)
        time.sleep(4 + (i % 3) * 0.1)
        if i == 11:
            raise ValueError("故意失败，用于检查失败计数")
        print(f"完成 demo-{i:02d}", flush=True)


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(experiment, i) for i in range(1, 16)]):
            try:
                future.result()
            except ValueError as exc:
                print(str(exc), flush=True)
    print("模拟批次结束。预期为成功 14 项、失败 1 项。", flush=True)
