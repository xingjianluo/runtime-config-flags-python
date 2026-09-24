"""刷新失败回退与并发读写一致性。

覆盖：
- 刷新失败保留旧版本、原因可区分、版本号不递增；
- 高并发读写期间，每次读到的快照内部字段全部来自同一版本（无混合）；
- 高并发刷新不会丢版本、不损坏状态，最终版本与成功次数一致。
"""
from __future__ import annotations

import threading

import pytest

from dynconfig.errors import RefreshError
from dynconfig.sources import PRIORITY_API, PRIORITY_DEFAULT, RawSource

from .conftest import case_log


def _valid_values(port: int, ratio: float) -> dict:
    return {"log.level": "info", "http.port": port,
            "sample.ratio": ratio, "service.name": "svc-a"}


@pytest.fixture()
def manager(schema):
    # 初始 v1 即符合配对公式：port=8080+0, ratio=0/1000
    from dynconfig.manager import ConfigManager
    return ConfigManager(schema, sources=[
        RawSource("default", PRIORITY_DEFAULT,
                  _valid_values(8080, 0.0))])


def test_failed_refresh_keeps_previous(manager):
    before = manager.snapshot()
    assert before.version == 1
    case_log.info("触发输入: 非法刷新 http.port=99999（当前 v1）")
    with pytest.raises(RefreshError) as ei:
        manager.refresh(RawSource("api", PRIORITY_API, {"http.port": 99999}))
    reasons = ei.value.reasons
    case_log.info("失败原因=%r kept_version=%s",
                  [(r["code"], r["field"]) for r in reasons],
          reasons[0].get("kept_version"))
    after = manager.snapshot()
    assert after.version == 1
    assert after is before
    assert all(r.get("kept_version") == 1 for r in reasons)
    assert any(r["code"] == "out_of_range" and r["field"] == "http.port"
               for r in reasons)

    # 失败后仍可正常刷新
    snap = manager.refresh(RawSource("api", PRIORITY_API, {"http.port": 9090}))
    assert snap.version == 2
    assert manager.snapshot().values["http.port"] == 9090


def test_concurrent_reads_never_see_mixed_versions(manager):
    stop = threading.Event()
    violations: list[str] = []
    seen_versions: set[int] = set()
    lock = threading.Lock()

    def reader() -> None:
        while not stop.is_set():
            snap = manager.snapshot()  # 一次引用读取
            # 自洽性检查：port 与 ratio 总是同一版本成对写入
            # （port=8080+v-1 与 ratio=(v-1)/1000）。跨版本混合会破坏等式。
            port = snap.values["http.port"]
            ratio = snap.values["sample.ratio"]
            step = port - 8080
            expected_version = step + 1
            if not (0 <= step <= 100) or abs(ratio - step / 1000) > 1e-12:
                with lock:
                    violations.append(
                        f"字段混合/越界 port={port} ratio={ratio} v={snap.version}")
            elif snap.version != expected_version:
                with lock:
                    violations.append(
                        f"版本与字段不匹配 v={snap.version} port={port}")
            with lock:
                seen_versions.add(snap.version)

    readers = [threading.Thread(target=reader) for _ in range(8)]
    for t in readers:
        t.start()

    # 100 次成功刷新，字段成对变化，混合版本会破坏配对关系
    for i in range(1, 101):
        overlay = RawSource("api", PRIORITY_API,
                            {"http.port": 8080 + i, "sample.ratio": i / 1000})
        manager.refresh(overlay)

    stop.set()
    for t in readers:
        t.join()
    case_log.info("读到版本数=%d 最终版本=%d 违规=%r",
                  len(seen_versions), manager.version(), violations)
    assert not violations
    assert manager.version() == 101


def test_concurrent_refresh_failures_preserve_state(manager):
    """成功与失败刷新并发：失败绝不污染，成功次数 == 版本增量。"""
    success = {"n": 0}
    failure = {"n": 0}
    lock = threading.Lock()

    def good(i: int) -> None:
        try:
            manager.refresh(RawSource(
                f"api-good-{i}", PRIORITY_API,
                _valid_values(9000 + i, 0.1 + i / 10000)))
            with lock:
                success["n"] += 1
        except RefreshError:
            with lock:
                failure["n"] += 1

    def bad(i: int) -> None:
        try:
            manager.refresh(RawSource(
                f"api-bad-{i}", PRIORITY_API,
                {"http.port": -i}))  # 恒越界
        except RefreshError as e:
            assert all(r.get("kept_version") is not None for r in e.reasons)
            with lock:
                failure["n"] += 1

    threads = []
    for i in range(1, 41):
        threads.append(threading.Thread(target=good, args=(i,)))
        threads.append(threading.Thread(target=bad, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = manager.snapshot()
    case_log.info("成功刷新=%d 失败刷新=%d 最终版本=%d port=%s ratio=%s",
                  success["n"], failure["n"], final.version,
                  final.values["http.port"], final.values["sample.ratio"])
    assert success["n"] == 40
    assert failure["n"] == 40
    assert final.version == 41  # 初始 v1 + 40 次成功
    # 最终快照本身合法（失败刷新未污染字段）
    assert final.values["http.port"] >= 9001
    assert 0.0 < final.values["sample.ratio"] < 1.0
