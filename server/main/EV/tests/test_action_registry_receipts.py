# -*- coding: utf-8 -*-

import threading
import time

from devices.coding.action_registry import ActionRegistry


def test_missing_explicit_ok_is_a_failed_receipt():
    registry = ActionRegistry()
    registry.register("unsafe", lambda args, ctx: ("done", {"action": "unsafe"}))
    result = registry.exec_action("unsafe", {}, {})
    assert result["ok"] is False
    assert result["error"] == "invalid_action_receipt"


def test_explicit_success_receipt_is_preserved():
    registry = ActionRegistry()
    registry.register("safe", lambda args, ctx: ("done", {"ok": True}))
    result = registry.exec_action("safe", {}, {})
    assert result["ok"] is True


def test_independent_domains_execute_in_parallel():
    registry = ActionRegistry()
    barrier = threading.Barrier(2)

    def execute(args, ctx):
        barrier.wait(timeout=0.4)
        time.sleep(0.04)
        return "done", {"ok": True}

    registry.register("device", execute, conflicts="device_id")
    registry.register("surface", execute, conflicts="surface_id")
    started = time.perf_counter()
    results = registry.run_batch([
        {"action": "device", "args": {"device_id": "light"}},
        {"action": "surface", "args": {"surface_id": "canvas"}},
    ])
    elapsed = time.perf_counter() - started
    assert all(result["ok"] for result in results)
    assert elapsed < 0.25


def test_same_device_is_serialized_in_stream_order():
    registry = ActionRegistry()
    active = 0
    max_active = 0
    order = []
    lock = threading.Lock()

    def execute(args, ctx):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            order.append(args["value"])
        time.sleep(0.02)
        with lock:
            active -= 1
        return "done", {"ok": True}

    registry.register("device", execute, conflicts="device_id")
    results = registry.run_batch([
        {"action": "device", "args": {"device_id": "light", "value": 1}},
        {"action": "device", "args": {"device_id": "light", "value": 2}},
    ])
    assert all(result["ok"] for result in results)
    assert max_active == 1
    assert order == [1, 2]
