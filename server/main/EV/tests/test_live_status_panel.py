from pathlib import Path

from control_plane import live_hub
from devices.coding.surface_layout import normalize_web_surface_definition


ROOT = Path(__file__).resolve().parents[1]


def test_local_voice_status_exposes_standby_and_can_stop_immediately():
    agent_id = 987321
    live_hub.heartbeat(
        agent_id,
        pid=4321,
        listening=False,
        standby=True,
    )
    status = live_hub.local_voice_status(agent_id)
    assert status["running"] is True
    assert status["pid"] == 4321
    assert status["listening"] is False
    assert status["standby"] is True

    live_hub.mark_voice_stopped(agent_id)
    stopped = live_hub.local_voice_status(agent_id)
    snapshot = live_hub.snapshot(agent_id)
    assert stopped["running"] is False
    assert stopped["pid"] == 0
    assert snapshot["speaking"] is False
    assert snapshot["listening"] is False
    assert snapshot["level"] == 0.0
    assert snapshot["events"][-1]["type"] == "stage"


def test_status_panel_is_compact_live_transcript_with_real_controls():
    page = (ROOT / "ui" / "status_timeline.html").read_text(encoding="utf-8")
    assert "随时说话" in page
    assert 'id="transcript-value"' in page
    assert "transcript-char is-new" in page
    assert "function compactTranscript(text)" in page
    assert "transcript-arrive" in page
    assert "signal-canvas" not in page
    assert "drawFluidMark" not in page
    assert "bezierCurveTo" not in page
    assert "event.final===false" in page
    assert "latest_user_utterance" in page
    assert "state.transcriptSeq" in page
    assert "live.stream_id" in page
    assert "nextStreamId!==state.streamId" in page
    assert "/voice/control" in page
    assert "pause" in page
    assert "resume" in page
    assert 'id="primary-control"' in page
    assert 'id="stop-control"' not in page
    assert 'id="result-control"' not in page
    assert "EV AGENT" not in page
    assert "PID" not in page
    assert "clear-control" not in page
    assert "health-strip" not in page
    assert "首响 P50" not in page
    assert "theme-control" not in page
    assert "prefers-color-scheme:light" in page
    assert "ev-status-theme" not in page
    assert "status_theme" in page
    assert "applyStatusTheme" in page
    assert "user-select:none" in page
    assert "clip-path:inset(0 round var(--radius))" in page
    assert "#000" not in page


def test_status_panel_guards_dynamic_content_against_visual_overflow():
    page = (ROOT / "ui" / "status_timeline.html").read_text(encoding="utf-8")
    assert "overflow-wrap:anywhere" in page
    assert "word-break:break-word" in page
    assert "-webkit-line-clamp:2" in page
    assert "chars.length>96" in page
    assert ".table-scroll { width:100%; overflow:auto }" in page
    assert "max-width:260px" in page
    assert "text-overflow:ellipsis" in page


def test_info_push_preserves_status_layout_and_uses_compact_result_space():
    page = (ROOT / "ui" / "status_timeline.html").read_text(encoding="utf-8")
    assert "height:132px; padding:12px 16px 14px" in page
    assert 'body[data-panel="open"] .statusbar' not in page
    assert 'body[data-panel="open"] .status-copy' not in page
    assert "height:calc(100% - 132px)" in page
    # 画布不再写死两栏网格：排版由服务端按用户意图组装的 layout 树给出，
    # 渲染器遍历容器（axis/columns/gap/weights）落地。固定模板正是
    #「要图给两张配图、要清单给两条链接」的根因。
    assert "grid-template-columns:minmax(0,1.05fr) minmax(126px,.95fr)" not in page
    assert ".canvas { width:100%; display:block;" in page
    assert 'canvas-group' in page and "buildLayout(documentState.layout" in page
    assert '.canvas-group[data-axis="grid"]' in page
    assert "-webkit-line-clamp:4" in page
    assert 'id="fullscreen-control"' not in page
    assert 'class="insight-head"' not in page
    assert 'class="collapse-handle"' in page
    assert "/info_panel/measure" in page
    assert "132+Math.ceil(ui.canvas.scrollHeight)+1" in page
    # 结果清单改为常显的编号条目（名称/描述/链接），不再折叠成「参考来源 ＋」：
    # 折叠状态下用户既看不到项目内容也拿不到链接，结论区又只是口头回答的副本。
    # 面板高度仍由 renderCanvas 末尾的 schedulePanelMeasure 负责测量。
    assert 'class=\'result-list\'' in page or "className='result-list'" in page
    assert "result-index" in page and "result-desc" in page


def test_status_panel_dynamic_height_is_bounded_and_preserves_measurement():
    from devices.coding import surface_tools

    target = surface_tools._status_timeline_target_height
    assert target(False) == surface_tools.STATUS_COLLAPSED_HEIGHT
    assert target(True, current_height=surface_tools.STATUS_COLLAPSED_HEIGHT) == 252
    assert target(True, current_height=238) == 238
    assert target(True, measured_height=236) == 236
    assert target(True, measured_height=1) == surface_tools.STATUS_EXPANDED_MIN_HEIGHT
    assert target(True, measured_height=9999) == surface_tools.STATUS_EXPANDED_HEIGHT
    assert target(True, immersive=True, measured_height=236) == 760


def test_tauri_status_shell_does_not_paint_a_second_panel_or_drag_strip():
    shell = (ROOT / "desktop-tauri" / "ui" / "styles.css").read_text(encoding="utf-8")
    overlay_window = shell.split(
        'body[data-window-kind="overlay"] .surface-window {', 1,
    )[1].split("}", 1)[0]
    overlay_bar = shell.split(
        'body[data-window-kind="overlay"] .surface-bar {', 1,
    )[1].split("}", 1)[0]
    overlay_content = shell.split(
        'body[data-window-kind="overlay"] .surface-content {', 1,
    )[1].split("}", 1)[0]
    assert "background: transparent" in overlay_window
    assert "border: 0" in overlay_window
    assert "position: absolute" in overlay_bar
    assert "background: transparent" in overlay_bar
    assert "height: 100%" in overlay_content
    assert "calc(100% - var(--ev-overlay-bar-h))" not in overlay_content
    assert "clip-path: inset(0 round var(--ev-radius))" in overlay_window
    assert "user-select: none" in shell


def test_info_panel_supports_ai_composition_zoom_tables_and_3d():
    page = (ROOT / "ui" / "status_timeline.html").read_text(encoding="utf-8")
    assert "AI 编排" not in page
    assert "搜索结果标签页" not in page
    assert "信息摘要" in page
    assert "applyCanvas(changes,tabId)" in page
    assert "focus_id:nodeId" in page
    assert "patchView('zoom'" in page
    assert "source-collection" in page
    assert "table-node" in page
    assert "model-viewer" in page
    assert "'/static/vendor/model-viewer.min.js'" in page
    assert "viewer.addEventListener('load'" in page
    assert "viewer.addEventListener('error'" in page
    assert "documentState.view?.focus_id" in page
    assert "/info_panel/canvas" in page
    assert "/api/ui/status-version" in page
    assert "setInterval(checkUiRevision,2000)" in page
    assert " 个来源" not in page
    assert "正在整理结果" not in page
    assert "参考来源" in page


def test_search_pending_canvas_does_not_show_result_count_as_content():
    source = (ROOT / "control_plane" / "panel_contract.py").read_text(encoding="utf-8")
    assert "条相关结果" not in source
    assert "正在整理搜索结果" not in source


def test_compact_surface_can_use_status_bar_height_without_affecting_default_windows():
    compact = normalize_web_surface_definition({
        "window": {"width": 420, "height": 136, "compact": True},
    })
    regular = normalize_web_surface_definition({
        "window": {"width": 420, "height": 136},
    })
    assert compact["window"]["height"] == 136
    assert regular["window"]["height"] == 220


def test_voice_terminal_publishes_streaming_asr_partials_without_regex_routing():
    source = (ROOT / "devices" / "voice" / "terminal.py").read_text(encoding="utf-8")
    assert "partial_snapshot()" in source
    assert '"final": False' in source
    assert "if not _voice_feature_enabled_fast()" in source


def test_live_snapshot_keeps_latest_user_transcript_for_cursor_recovery():
    agent_id = 987322
    live_hub.push_utterance(
        agent_id,
        "user",
        "打开个记事本",
        turn_id="turn-live",
        final=False,
    )
    partial = live_hub.snapshot(agent_id, after_seq=999999)
    assert partial["events"] == []
    assert partial["stream_id"]
    assert partial["latest_user_utterance"]["text"] == "打开个记事本"
    assert partial["latest_user_utterance"]["final"] is False

    live_hub.push_utterance(
        agent_id,
        "user",
        "打开个记事本。",
        turn_id="turn-live",
        final=True,
    )
    final = live_hub.snapshot(agent_id, after_seq=999999)
    assert final["latest_user_utterance"]["text"] == "打开个记事本。"
    assert final["latest_user_utterance"]["final"] is True
