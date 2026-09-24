"""并发刷新 + 并发读取：读到的永远是某个完整版本，字段不得跨版本混合。"""
from __future__ import annotations

import threading

from app.errors import ConfigError
from app.flags import evaluate, request_scope
from app.sources import make_api_payload

# 两个“完整且不同”的版本：(app_name, max_connections) 必须成对出现
VERSION_A = {
    "values": {"app_name": "alpha-app", "db_password": "pa", "max_connections": 111},
    "flags": {"new_checkout": {"mode": "percentage", "percentage": 10}},
}
VERSION_B = {
    "values": {"app_name": "bravo-app", "db_password": "pb", "max_connections": 222},
    "flags": {"new_checkout": {"mode": "percentage", "percentage": 90}},
}
# 故意非法的刷新：越界 / 类型错误 / 缺项
BAD_PAYLOADS = [
    {"values": {"app_name": "alpha-app", "db_password": "pa", "max_connections": 10**9}},
    {"values": {"app_name": "alpha-app", "db_password": 99}},
    {"values": {"app_name": "alpha-app"}},
]

def test_concurrent_refresh_and_read_no_mixing(manager, spec):
    stop = threading.Event()
    errors: list[Exception] = []
    seen_pairs: set[tuple] = set()
    reads = {"count": 0}
    lock = threading.Lock()

    def reader():
        try:
            while not stop.is_set():
                snap = manager.current()
                # 1) 字段完整：任何时刻都必须有全部键（不丢失）
                assert set(snap.values) == {f.key for f in spec.fields}
                app_name = snap.values["app_name"]
                max_conn = snap.values["max_connections"]
                pct = snap.flags["new_checkout"].percentage
                # 2) 字段来自同一版本：组合必须是已知的某一整版（不混合）
                if app_name == "alpha-app":
                    assert (max_conn, pct) == (111, 10)
                elif app_name == "bravo-app":
                    assert (max_conn, pct) == (222, 90)
                else:
                    assert max_conn == 100 and pct == 0
                with lock:
                    seen_pairs.add((app_name, max_conn, pct, snap.version))
                    reads["count"] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def good_refresher(which):
        payload = VERSION_A if which == "A" else VERSION_B
        try:
            while not stop.is_set():
                r = manager.reload_from([make_api_payload(**payload)])
                assert r.ok
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def bad_refresher():
        i = 0
        try:
            while not stop.is_set():
                bad = BAD_PAYLOADS[i % len(BAD_PAYLOADS)]
                i += 1
                r = manager.reload_from([make_api_payload(**bad)])
                # 非法刷新必须失败，且旧快照仍然有效
                assert not r.ok
                assert r.error_type in {
                    "ConfigRangeError",
                    "ConfigTypeError",
                    "ConfigMissingError",
                }
                snap = manager.current()
                assert snap.values["app_name"] in {"alpha-app", "bravo-app", "my-app"}
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    threads += [
        threading.Thread(target=good_refresher, args=("A",)),
        threading.Thread(target=good_refresher, args=("B",)),
        threading.Thread(target=bad_refresher),
    ]
    for t in threads:
        t.start()
    stop.wait(0.8)
    stop.set()
    for t in threads:
        t.join(5)

    assert not errors, errors
    assert reads["count"] > 100
    print(
        f"[输入] 8 读线程 + 2 合法刷新 + 1 非法刷新，共读取 {reads['count']} 次\n"
        f"[依据] 观察到的 (app_name,max_conn,percentage,version) 组合: {sorted(seen_pairs)}"
    )
    # 至少观察到两个版本，且每种组合内部自洽
    app_names = {p[0] for p in seen_pairs}
    assert {"alpha-app", "bravo-app"} <= app_names


def test_concurrent_flag_evaluation_request_scoped_consistency(manager, spec):
    """同一请求（request_scope）内多次判定一致，即使并发判定不同用户。"""
    manager.reload_from(
        [
            make_api_payload(
                values={"app_name": "alpha-app", "db_password": "pa"},
                flags={"new_checkout": {"mode": "percentage", "percentage": 50}},
            )
        ]
    )
    snap = manager.current()
    results: dict[int, bool] = {}
    errors: list[Exception] = []

    def worker(uid: int):
        try:
            with request_scope():
                first = evaluate(
                    snap, spec, "new_checkout", user_id=f"user-{uid}"
                ).enabled
                for _ in range(20):
                    again = evaluate(
                        snap, spec, "new_checkout", user_id=f"user-{uid}"
                    ).enabled
                    assert again == first
                results[uid] = first
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors, errors
    print(f"[输入] 20 个并发请求各判定 21 次 => [依据] 每请求结果恒定: {results}")
