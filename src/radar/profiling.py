import threading
import time
from pathlib import Path

import psutil


class ResourceSample:
    def __init__(self):
        self.process = psutil.Process()
        self.peak_rss = 0
        self.stop = threading.Event()

    def __enter__(self):
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.available_before = psutil.virtual_memory().available
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()
        return self

    def _sample(self):
        while not self.stop.is_set():
            rss = self.process.memory_info().rss
            for child in self.process.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except psutil.Error:
                    pass
            self.peak_rss = max(self.peak_rss, rss)
            self.stop.wait(0.05)

    def __exit__(self, *args):
        self.finish()

    def finish(self):
        if hasattr(self, "result"):
            return self.result
        self.stop.set()
        self.thread.join(timeout=2)
        self.result = {
            "wall_seconds": round(time.monotonic() - self.started, 3),
            "cpu_seconds": round(time.process_time() - self.cpu_started, 3),
            "cpu_scope": "current_process_only_children_not_measured",
            "peak_process_tree_rss_bytes": self.peak_rss,
            "host_available_before_bytes": self.available_before,
            "host_available_after_bytes": psutil.virtual_memory().available,
            "sample_interval_ms": 50,
            "scope": "current_process_and_children",
        }
        return self.result


def disk_bytes(root):
    return sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())
