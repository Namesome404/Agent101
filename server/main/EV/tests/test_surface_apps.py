from unittest import mock

from tools import surface_apps, surface_control


def _scene_mock():
    scene = mock.Mock()
    scene.get.return_value = None
    scene.rev = 10
    scene.upsert.return_value = {"changed": True, "rev": 11}
    scene.wait_surface_ready.return_value = True
    return scene


def test_timer_start_is_typed_state_not_generated_page_logic():
    scene = _scene_mock()
    with mock.patch.object(surface_apps, "scene_store", scene), mock.patch.object(
        surface_apps.time, "time", return_value=1_700_000_000.0,
    ):
        _text, meta = surface_control.execute({
            "action": "app",
            "app_id": "timer",
            "command": "start",
            "duration_seconds": 600,
            "continue_after": False,
        })

    assert meta["ok"] is True
    assert meta["surface_id"] == "app-timer"
    assert meta["state"]["duration_seconds"] == 600
    assert meta["state"]["ends_at"] == 1_700_000_600.0
    submitted = scene.upsert.call_args.kwargs["data"]
    assert submitted["app"] == {
        "id": "timer", "version": 1, "state": meta["state"],
    }
    assert submitted["content"]["type"] == "app"


def test_notes_append_updates_stable_notes_surface():
    scene = _scene_mock()
    scene.get.return_value = {
        "data": {"app": {"state": {"items": ["第一条"]}}},
    }
    with mock.patch.object(surface_apps, "scene_store", scene):
        _text, meta = surface_apps.execute({
            "app_id": "notes",
            "command": "append",
            "text": "第二条",
        })

    assert meta["ok"] is True
    assert meta["surface_id"] == "app-notes"
    assert meta["state"]["items"] == ["第一条", "第二条"]
    assert scene.upsert.call_args.kwargs["focus"] is True


def test_timer_rejects_missing_duration_without_opening_window():
    scene = _scene_mock()
    with mock.patch.object(surface_apps, "scene_store", scene):
        _text, meta = surface_apps.execute({
            "app_id": "timer",
            "command": "start",
        })

    assert meta["ok"] is False
    assert meta["reason"] == "invalid_app_state"
    scene.upsert.assert_not_called()


def test_surface_schema_exposes_timer_as_app_not_current_time():
    function = surface_control.tool_definition()["function"]
    properties = function["parameters"]["properties"]
    assert "app" in properties["action"]["enum"]
    assert properties["app_id"]["enum"] == ["timer", "notes"]
    assert properties["duration_seconds"]["description"].endswith("十分钟=600。")


def test_builtin_timer_event_is_typed_and_surface_scoped():
    assert surface_apps.command_from_event("app-timer", {
        "app_id": "timer", "command": "add", "seconds": 300,
    }) == {"app_id": "timer", "command": "add", "duration_seconds": 300}
    assert surface_apps.command_from_event("another-surface", {
        "app_id": "timer", "command": "pause",
    }) is None
    assert surface_apps.command_from_event("app-notes", {
        "app_id": "notes", "command": "organize",
    }) is None
