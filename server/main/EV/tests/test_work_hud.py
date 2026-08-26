# -*- coding: utf-8 -*-
from unittest.mock import patch

from devices.coding import orchestrator


def test_work_hud_is_small_and_separate_from_status_surface():
    with patch("devices.coding.orchestrator.project_fsm.load", return_value={"phase": "writing", "brief": {}, "active_run": {"run_id": "run-1"}}), patch(
        "devices.coding.orchestrator.agent_runtime.get_active_run",
        return_value={"run_id": "run-1", "alive": True, "files": ["app.py"], "checks": 1},
    ), patch("devices.coding.orchestrator.agent_runtime.get_events", return_value=[]), patch(
        "devices.coding.orchestrator.scene_store.get", return_value=None,
    ), patch("devices.coding.orchestrator.scene_store.upsert", return_value={"changed": True}) as upsert:
        orchestrator.push_studio(1, status="处理中", phase="writing", detail="修改文件")
    surface_id, = upsert.call_args.args
    data = upsert.call_args.kwargs["data"]
    assert surface_id == "work-hud"
    assert data["window"]["width"] == 152
    assert data["window"]["height"] == 224
    assert data["window"]["anchored_to"] == "status-timeline"
    assert upsert.call_args.kwargs["focus"] is False


def test_planning_does_not_open_work_hud():
    with patch("devices.coding.orchestrator.project_fsm.load", return_value={"phase": "awaiting_confirm", "brief": {}}), patch(
        "devices.coding.orchestrator.agent_runtime.get_active_run", return_value=None,
    ), patch("devices.coding.orchestrator.scene_store.get", return_value=None), patch(
        "devices.coding.orchestrator.scene_store.upsert", return_value={"changed": True},
    ) as upsert:
        orchestrator.push_studio(1, status="待确认", phase="awaiting_confirm")
    assert upsert.call_args.kwargs["visible"] is False
