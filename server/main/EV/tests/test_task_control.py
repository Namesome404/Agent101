# -*- coding: utf-8 -*-
from tools import task_control


def test_task_control_is_one_typed_low_frequency_entry():
    function = task_control.tool_definition()["function"]
    assert function["name"] == "task_control"
    parameters = function["parameters"]
    assert parameters["required"] == ["kind", "request", "continue_after"]
    kinds = parameters["properties"]["kind"]["enum"]
    assert {"current_time", "weather", "web_search", "web_extract", "coding_plan"} <= set(kinds)
    assert "info_present" not in kinds
    assert "time" not in kinds


def test_task_control_exposes_short_workflow_controls():
    properties = task_control.tool_definition()["function"]["parameters"]["properties"]
    assert properties["speak_while"]["type"] == "boolean"
    assert properties["progress_reply"]["type"] == "string"
    assert properties["continue_after"]["type"] == "boolean"
    assert properties["research_depth"]["enum"] == ["quick", "thorough"]
    assert properties["search_queries"]["maxItems"] == 3
    assert properties["include_visuals"]["type"] == "boolean"
    assert "present" not in properties
    assert "focus_type" not in properties
    assert "focus_index" not in properties
