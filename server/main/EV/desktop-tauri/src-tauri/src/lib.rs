use std::{
    collections::{HashMap, HashSet},
    sync::Mutex,
    time::Duration,
};

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tauri::{
    webview::{PageLoadEvent, WebviewBuilder},
    window::{Effect, EffectState, EffectsBuilder},
    AppHandle, Emitter, EventTarget, LogicalPosition, LogicalSize, Manager, PhysicalPosition,
    PhysicalSize, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Window, WindowEvent,
};
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};

#[derive(Clone, Default)]
struct ReceiptState {
    content_status: String,
    content_url: String,
    content_error: String,
}

#[derive(Default)]
struct SceneCache {
    rev: u64,
    surfaces: HashMap<String, Value>,
    labels: HashMap<String, String>,
    geometry_keys: HashMap<String, String>,
    show_revisions: HashMap<String, u64>,
    receipts: HashMap<String, ReceiptState>,
    ready_generation: HashMap<String, u64>,
    remote_urls: HashMap<String, String>,
}

struct SceneRuntime {
    cache: Mutex<SceneCache>,
    outgoing: mpsc::UnboundedSender<Value>,
}

impl SceneRuntime {
    fn send(&self, message: Value) {
        let _ = self.outgoing.send(message);
    }
}

fn surface_label(surface_id: &str) -> String {
    let mut label = String::from("surface-");
    for byte in surface_id.as_bytes() {
        label.push_str(&format!("{byte:02x}"));
    }
    label
}

fn content_label(surface_id: &str) -> String {
    surface_label(surface_id).replacen("surface-", "content-", 1)
}

fn emit_surface(window: &Window, event: &str, payload: Value) {
    let _ = window.emit_to(EventTarget::webview(window.label()), event, payload);
}

fn number_at(value: &Value, path: &[&str]) -> Option<f64> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    current.as_f64()
}

fn bool_at(value: &Value, path: &[&str]) -> bool {
    let mut current = value;
    for key in path {
        let Some(next) = current.get(*key) else { return false };
        current = next;
    }
    current.as_bool().unwrap_or(false)
}

fn string_at(value: &Value, path: &[&str]) -> String {
    let mut current = value;
    for key in path {
        let Some(next) = current.get(*key) else { return String::new() };
        current = next;
    }
    current.as_str().unwrap_or_default().to_string()
}

fn requested_geometry(window: &Window, surface: &Value) -> (i32, i32, u32, u32, String) {
    let surface_id = string_at(surface, &["id"]);
    let minimum_width = if surface_id == "work-hud" { 120.0 } else { 320.0 };
    let width = number_at(surface, &["data", "window", "width"])
        .unwrap_or(520.0)
        .clamp(minimum_width, 2200.0)
        .round() as u32;
    let minimum_height = if surface_id == "status-timeline" {
        128.0
    } else if surface_id == "work-hud" {
        160.0
    } else {
        220.0
    };
    let height = number_at(surface, &["data", "window", "height"])
        .unwrap_or(420.0)
        .clamp(minimum_height, 1800.0)
        .round() as u32;

    let (area_x, area_y, area_width, area_height) = window
        .primary_monitor()
        .ok()
        .flatten()
        .map(|monitor| {
            let area = monitor.work_area();
            let scale = monitor.scale_factor();
            let position = area.position.to_logical::<f64>(scale);
            let size = area.size.to_logical::<f64>(scale);
            (
                position.x.round() as i32,
                position.y.round() as i32,
                size.width.round() as u32,
                size.height.round() as u32,
            )
        })
        .unwrap_or((0, 0, 1440, 900));
    let fallback_x = area_x + area_width as i32 - width as i32 - 48;
    let fallback_y = area_y + 52;
    let requested_x = number_at(surface, &["data", "window", "x"]).map(|value| value.round() as i32);
    let requested_y = number_at(surface, &["data", "window", "y"]).map(|value| value.round() as i32);
    let position = string_at(surface, &["data", "window", "position"]);
    let anchored_x = if position.contains("left") {
        area_x + 16
    } else if position.contains("right") {
        area_x + area_width as i32 - width as i32 - 16
    } else if !position.is_empty() {
        area_x + (area_width as i32 - width as i32) / 2
    } else {
        fallback_x
    };
    let anchored_y = if position.contains("top") {
        area_y + 16
    } else if position.contains("bottom") {
        area_y + area_height as i32 - height as i32 - 16
    } else if position == "center" {
        area_y + (area_height as i32 - height as i32) / 2
    } else {
        fallback_y
    };
    let max_x = area_x + area_width as i32 - width as i32;
    let max_y = area_y + area_height as i32 - height as i32;
    let x = requested_x.unwrap_or(anchored_x).clamp(area_x, max_x.max(area_x));
    let y = requested_y.unwrap_or(anchored_y).clamp(area_y, max_y.max(area_y));
    let key = json!({
        "x": requested_x,
        "y": requested_y,
        "position": position,
        "width": number_at(surface, &["data", "window", "width"]),
        "height": number_at(surface, &["data", "window", "height"]),
    })
    .to_string();
    (x, y, width, height, key)
}

fn position_work_hud(app: &AppHandle) {
    let Some(hud) = app.get_window(&surface_label("work-hud")) else { return };
    let Some(status) = app.get_window(&surface_label("status-timeline")) else { return };
    let scale = status.scale_factor().unwrap_or(1.0);
    let Ok(status_position) = status.outer_position() else { return };
    let Ok(status_size) = status.outer_size() else { return };
    let hud_scale = hud.scale_factor().unwrap_or(scale);
    let hud_size = hud
        .outer_size()
        .unwrap_or(PhysicalSize::new(152, 224))
        .to_logical::<f64>(hud_scale);
    let status_position = status_position.to_logical::<f64>(scale);
    let status_size = status_size.to_logical::<f64>(scale);
    let area_x = status
        .current_monitor()
        .ok()
        .flatten()
        .map(|monitor| monitor.work_area().position.to_logical::<f64>(monitor.scale_factor()).x)
        .unwrap_or(0.0);
    let gap = 10.0;
    let left = status_position.x - hud_size.width - gap;
    let x = if left >= area_x {
        left
    } else {
        status_position.x + status_size.width + gap
    };
    let _ = hud.set_position(LogicalPosition::new(x.round(), status_position.y.round()));
}

fn ease_cinematic(progress: f64) -> f64 {
    1.0 - (1.0 - progress).powi(4)
}

fn animate_geometry(window: Window, x: i32, y: i32, width: u32, height: u32, animate: bool) {
    if !animate {
        let _ = window.set_position(LogicalPosition::new(x, y));
        let _ = window.set_size(LogicalSize::new(width, height));
        return;
    }
    tauri::async_runtime::spawn(async move {
        let scale = window.scale_factor().unwrap_or(1.0);
        let start_position = window
            .outer_position()
            .map(|position| position.to_logical::<f64>(scale))
            .unwrap_or(LogicalPosition::new(x as f64, y as f64));
        let start_size = window
            .inner_size()
            .map(|size| size.to_logical::<f64>(scale))
            .unwrap_or(LogicalSize::new(width as f64, height as f64));
        for frame in 1..=28 {
            let progress = ease_cinematic(frame as f64 / 28.0);
            let next_x = start_position.x + (x as f64 - start_position.x) * progress;
            let next_y = start_position.y + (y as f64 - start_position.y) * progress;
            let next_width = start_size.width + (width as f64 - start_size.width) * progress;
            let next_height = start_size.height + (height as f64 - start_size.height) * progress;
            let _ = window.set_position(LogicalPosition::new(next_x.round(), next_y.round()));
            let _ = window.set_size(LogicalSize::new(
                next_width.max(1.0).round(),
                next_height.max(1.0).round(),
            ));
            tokio::time::sleep(Duration::from_millis(16)).await;
        }
    });
}

fn send_intent(runtime: &SceneRuntime, surface_id: &str, name: &str, data: Value) {
    runtime.send(json!({
        "v": 1,
        "type": "intent",
        "surface": surface_id,
        "name": name,
        "data": data,
        "ts": chrono_like_millis(),
    }));
}

fn chrono_like_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn send_ready(window: &Window, surface_id: &str, runtime: &SceneRuntime) {
    let scale = window.scale_factor().unwrap_or(1.0);
    let position = window
        .outer_position()
        .unwrap_or(PhysicalPosition::new(0, 0))
        .to_logical::<f64>(scale);
    let size = window
        .inner_size()
        .unwrap_or(PhysicalSize::new(0, 0))
        .to_logical::<f64>(scale);
    let (rev, receipt) = runtime
        .cache
        .lock()
        .ok()
        .map(|cache| {
            (
                cache.rev,
                cache.receipts.get(surface_id).cloned().unwrap_or_default(),
            )
        })
        .unwrap_or_default();
    send_intent(
        runtime,
        surface_id,
        "surface.ready",
        json!({
            "rev": rev,
            "visible": window.is_visible().unwrap_or(false),
            "focused": window.is_focused().unwrap_or(false),
            "bounds": {
                "x": position.x.round() as i32,
                "y": position.y.round() as i32,
                "width": size.width.round() as u32,
                "height": size.height.round() as u32,
            },
            "contentStatus": if receipt.content_status.is_empty() { "unknown" } else { &receipt.content_status },
            "contentUrl": receipt.content_url,
            "contentError": receipt.content_error,
        }),
    );
}

fn schedule_ready(app: AppHandle, label: String, surface_id: String) {
    let generation = {
        let runtime = app.state::<SceneRuntime>();
        let Ok(mut cache) = runtime.cache.lock() else { return };
        let next = cache.ready_generation.get(&surface_id).copied().unwrap_or(0) + 1;
        cache.ready_generation.insert(surface_id.clone(), next);
        next
    };
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        let runtime = app.state::<SceneRuntime>();
        let is_latest = runtime
            .cache
            .lock()
            .ok()
            .and_then(|cache| cache.ready_generation.get(&surface_id).copied())
            == Some(generation);
        if !is_latest {
            return;
        }
        if let Some(window) = app.get_window(&label) {
            send_ready(&window, &surface_id, &runtime);
        }
    });
}

const CONTENT_TOP: f64 = 46.0;

fn remote_content_url(surface: &Value) -> Option<tauri::Url> {
    if surface.get("visible").and_then(Value::as_bool) != Some(true)
        || string_at(surface, &["id"]) == "status-timeline"
        || string_at(surface, &["data", "content", "type"]) != "url"
    {
        return None;
    }
    let raw = string_at(surface, &["data", "content", "url"]);
    let url = raw.parse::<tauri::Url>().ok()?;
    if matches!(url.scheme(), "http" | "https") {
        Some(url)
    } else {
        None
    }
}

fn record_content_state(
    app: &AppHandle,
    surface_id: &str,
    status: &str,
    url: &str,
    error: &str,
) {
    let runtime = app.state::<SceneRuntime>();
    if let Ok(mut cache) = runtime.cache.lock() {
        let receipt = cache.receipts.entry(surface_id.to_string()).or_default();
        receipt.content_status = status.to_string();
        receipt.content_url = url.to_string();
        receipt.content_error = error.to_string();
    }
    send_intent(
        &runtime,
        surface_id,
        &format!("surface.content_{status}"),
        json!({"url": url, "error": error}),
    );
    if let Some(window) = app.get_window(&surface_label(surface_id)) {
        send_ready(&window, surface_id, &runtime);
    }
}

fn resize_remote_content(app: &AppHandle, surface_id: &str) {
    let Some(window) = app.get_window(&surface_label(surface_id)) else {
        return;
    };
    let Some(webview) = app.get_webview(&content_label(surface_id)) else {
        return;
    };
    let scale = window.scale_factor().unwrap_or(1.0);
    let size = window
        .inner_size()
        .unwrap_or(PhysicalSize::new(1, 1))
        .to_logical::<f64>(scale);
    let _ = webview.set_position(LogicalPosition::new(0.0, CONTENT_TOP));
    let _ = webview.set_size(LogicalSize::new(
        size.width.max(1.0),
        (size.height - CONTENT_TOP).max(1.0),
    ));
}

fn close_remote_content(app: &AppHandle, surface_id: &str) {
    if let Some(webview) = app.get_webview(&content_label(surface_id)) {
        let _ = webview.close();
    }
    if let Ok(mut cache) = app.state::<SceneRuntime>().cache.lock() {
        cache.remote_urls.remove(surface_id);
    }
}

fn sync_remote_content(app: &AppHandle, surface_id: &str, surface: &Value) {
    let next_url = remote_content_url(surface);
    let next_url_text = next_url.as_ref().map(ToString::to_string);
    let current_url = app
        .state::<SceneRuntime>()
        .cache
        .lock()
        .ok()
        .and_then(|cache| cache.remote_urls.get(surface_id).cloned());

    if current_url == next_url_text {
        resize_remote_content(app, surface_id);
        return;
    }

    close_remote_content(app, surface_id);
    let Some(url) = next_url else { return };
    let url_text = url.to_string();
    let parent_label = surface_label(surface_id);
    let child_label = content_label(surface_id);
    let id_for_load = surface_id.to_string();
    let app_for_load = app.clone();

    record_content_state(app, surface_id, "loading", &url_text, "");
    let builder = WebviewBuilder::new(child_label, WebviewUrl::External(url))
        .focused(false)
        .on_page_load(move |_webview, payload| {
            let status = match payload.event() {
                PageLoadEvent::Started => "loading",
                PageLoadEvent::Finished => "ready",
            };
            record_content_state(
                &app_for_load,
                &id_for_load,
                status,
                payload.url().as_str(),
                "",
            );
        });
    let Some(parent) = app.get_window(&parent_label) else {
        record_content_state(app, surface_id, "error", &url_text, "parent window unavailable");
        return;
    };
    let scale = parent.scale_factor().unwrap_or(1.0);
    let size = parent
        .inner_size()
        .unwrap_or(PhysicalSize::new(520, 420))
        .to_logical::<f64>(scale);
    match parent.add_child(
        builder,
        LogicalPosition::new(0.0, CONTENT_TOP),
        LogicalSize::new(size.width.max(1.0), (size.height - CONTENT_TOP).max(1.0)),
    ) {
        Ok(_) => {
            if let Ok(mut cache) = app.state::<SceneRuntime>().cache.lock() {
                cache.remote_urls.insert(surface_id.to_string(), url_text);
            }
        }
        Err(error) => {
            record_content_state(app, surface_id, "error", &url_text, &error.to_string());
        }
    }
}

fn register_window_events(app: &AppHandle, window: &WebviewWindow, surface_id: &str) {
    let app_handle = app.clone();
    let label = window.label().to_string();
    let id = surface_id.to_string();
    window.on_window_event(move |event| match event {
        WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            if let Some(current) = app_handle.get_window(&label) {
                emit_surface(&current, "surface:hide", json!({"reason": "user"}));
            }
        }
        WindowEvent::Resized(_) => {
            resize_remote_content(&app_handle, &id);
            schedule_ready(app_handle.clone(), label.clone(), id.clone());
            if id == "status-timeline" {
                position_work_hud(&app_handle);
            }
        }
        WindowEvent::Moved(_) | WindowEvent::Focused(_) => {
            schedule_ready(app_handle.clone(), label.clone(), id.clone());
            if id == "status-timeline" {
                position_work_hud(&app_handle);
            }
        }
        _ => {}
    });
}

fn ensure_surface(app: &AppHandle, surface: Value) {
    let surface_id = string_at(&surface, &["id"]);
    if surface_id.is_empty() {
        return;
    }
    let label = surface_label(&surface_id);
    let title = string_at(&surface, &["data", "title"]);
    // 用 get_window（窗口容器）而非 get_webview_window（单 webview 便捷句柄）判定
    // 存在性与取句柄：url 窗口用 add_child 挂了第二个 webview（内容页），此后该
    // 窗口不再是「单 webview 窗口」，get_webview_window 会返回 None，会被误判为
    // 「窗口不存在」→ 用同名 label 重建 → build() 在主线程阻塞 → scene 循环整体
    // 死锁（后续 patch/回执/reconcile 全部停摆，窗口永远关不掉）。get_window 对
    // 多 webview 窗口依然有效，是这里唯一正确的句柄来源。
    let existed = app.get_window(&label).is_some();
    if !existed {
        let is_status = surface_id == "status-timeline";
        let is_hud = surface_id == "work-hud";
        let entrypoint = format!("index.html?surface={label}");
        let mut builder = WebviewWindowBuilder::new(app, &label, WebviewUrl::App(entrypoint.into()))
            .title(if title.is_empty() { &surface_id } else { &title })
            .inner_size(if is_hud { 152.0 } else { 520.0 }, if is_status { 160.0 } else if is_hud { 224.0 } else { 420.0 })
            .min_inner_size(if is_hud { 120.0 } else { 320.0 }, if is_status { 128.0 } else if is_hud { 160.0 } else { 220.0 })
            .decorations(!is_status && !is_hud)
            .transparent(true)
            .shadow(!is_status && !is_hud)
            .visible(false)
            .resizable(!is_status && !is_hud)
            .accept_first_mouse(true);
        #[cfg(target_os = "macos")]
        if !is_status && !is_hud {
            builder = builder
                .title_bar_style(tauri::TitleBarStyle::Overlay)
                .hidden_title(true)
                .traffic_light_position(LogicalPosition::new(14.0, 15.0))
                .effects(
                    EffectsBuilder::new()
                        .effect(Effect::UnderWindowBackground)
                        .state(EffectState::Active)
                        .radius(12.0)
                        .build(),
                );
        }
        let Ok(webview_window) = builder.build() else {
            return;
        };
        register_window_events(app, &webview_window, &surface_id);
    }
    let Some(window) = app.get_window(&label) else {
        return;
    };

    let (x, y, width, height, geometry_key) = requested_geometry(&window, &surface);
    let show_rev = surface
        .get("data")
        .and_then(|data| data.get("show_rev"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let (should_apply_geometry, show_changed) = {
        let runtime = app.state::<SceneRuntime>();
        let Ok(mut cache) = runtime.cache.lock() else { return };
        let changed = cache.geometry_keys.get(&surface_id) != Some(&geometry_key);
        let show_changed = cache.show_revisions.get(&surface_id).copied() != Some(show_rev);
        cache.geometry_keys.insert(surface_id.clone(), geometry_key);
        cache.show_revisions.insert(surface_id.clone(), show_rev);
        cache.labels.insert(label.clone(), surface_id.clone());
        cache.surfaces.insert(surface_id.clone(), surface.clone());
        (changed, show_changed)
    };
    if should_apply_geometry {
        if surface_id == "work-hud" {
            let _ = window.set_size(LogicalSize::new(width, height));
        } else {
            animate_geometry(window.clone(), x, y, width, height, existed);
        }
    }
    if surface_id == "work-hud" {
        position_work_hud(app);
    }
    if !title.is_empty() {
        let _ = window.set_title(&title);
    }
    let _ = window.set_always_on_top(bool_at(&surface, &["data", "window", "always_on_top"]));
    let _ = window.set_visible_on_all_workspaces(
        surface_id == "status-timeline"
            || bool_at(&surface, &["data", "window", "visible_on_all_workspaces"]),
    );
    emit_surface(&window, "surface:update", surface.clone());

    let wants_visible = surface.get("visible").and_then(Value::as_bool).unwrap_or(false);
    let was_visible = window.is_visible().unwrap_or(false);
    if wants_visible {
        if !was_visible {
            emit_surface(&window, "surface:show", json!({"reason": "scene"}));
            let _ = window.show();
        }
        if surface.get("focus").and_then(Value::as_bool).unwrap_or(false)
            && (!was_visible || show_changed)
        {
            let _ = window.set_focus();
        }
        schedule_ready(app.clone(), label.clone(), surface_id.clone());
    } else if was_visible {
        emit_surface(&window, "surface:hide", json!({"reason": "scene"}));
        let app_handle = app.clone();
        let id_for_check = surface_id.clone();
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(Duration::from_millis(240)).await;
            let still_hidden = app_handle
                .state::<SceneRuntime>()
                .cache
                .lock()
                .ok()
                .and_then(|cache| cache.surfaces.get(&id_for_check).cloned())
                .and_then(|current| current.get("visible").and_then(Value::as_bool))
                != Some(true);
            if still_hidden {
                if let Some(current) = app_handle.get_window(&label) {
                    let _ = current.hide();
                }
            }
        });
    }
    if existed {
        sync_remote_content(app, &surface_id, &surface);
    }
}

fn remove_surface(app: &AppHandle, surface_id: &str) {
    let label = surface_label(surface_id);
    close_remote_content(app, surface_id);
    if let Some(window) = app.get_window(&label) {
        let _ = window.destroy();
    }
    let runtime = app.state::<SceneRuntime>();
    if let Ok(mut cache) = runtime.cache.lock() {
        cache.surfaces.remove(surface_id);
        cache.labels.remove(&label);
        cache.geometry_keys.remove(surface_id);
        cache.show_revisions.remove(surface_id);
        cache.receipts.remove(surface_id);
        cache.ready_generation.remove(surface_id);
    };
}

fn reconcile_visibility(app: &AppHandle) {
    let surfaces = app
        .state::<SceneRuntime>()
        .cache
        .lock()
        .ok()
        .map(|cache| cache.surfaces.values().cloned().collect::<Vec<_>>())
        .unwrap_or_default();
    for surface in surfaces {
        if surface.get("visible").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let surface_id = string_at(&surface, &["id"]);
        let label = surface_label(&surface_id);
        let Some(window) = app.get_window(&label) else { continue };
        if window.is_visible().unwrap_or(false) {
            continue;
        }
        emit_surface(
            &window,
            "surface:show",
            json!({"reason": "reconcile", "surface_id": surface_id}),
        );
        let _ = window.show();
        if surface.get("focus").and_then(Value::as_bool) == Some(true) {
            let _ = window.set_focus();
        }
        schedule_ready(app.clone(), label, surface_id);
    }
}

fn apply_full_scene(app: &AppHandle, message: &Value) {
    let surfaces = message
        .get("surfaces")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let next_ids: HashSet<String> = surfaces
        .iter()
        .map(|surface| string_at(surface, &["id"]))
        .filter(|id| !id.is_empty())
        .collect();
    let previous_ids = {
        let runtime = app.state::<SceneRuntime>();
        let Ok(mut cache) = runtime.cache.lock() else { return };
        cache.rev = message.get("rev").and_then(Value::as_u64).unwrap_or(cache.rev);
        cache.surfaces.keys().cloned().collect::<Vec<_>>()
    };
    for id in previous_ids {
        if !next_ids.contains(&id) {
            remove_surface(app, &id);
        }
    }
    for surface in surfaces {
        ensure_surface(app, surface);
    }
}

fn apply_patch(app: &AppHandle, message: &Value) {
    let (base, current_rev) = {
        let runtime = app.state::<SceneRuntime>();
        let Ok(cache) = runtime.cache.lock() else { return };
        (
            message.get("base").and_then(Value::as_u64).unwrap_or(0),
            cache.rev,
        )
    };
    if base != current_rev {
        app.state::<SceneRuntime>()
            .send(json!({"v": 1, "type": "resync", "reason": "gap"}));
        return;
    }
    if let Ok(mut cache) = app.state::<SceneRuntime>().cache.lock() {
        cache.rev = message.get("rev").and_then(Value::as_u64).unwrap_or(cache.rev);
    }
    for operation in message
        .get("ops")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        match operation.get("op").and_then(Value::as_str).unwrap_or_default() {
            "upsert" => {
                if let Some(surface) = operation.get("surface") {
                    ensure_surface(app, surface.clone());
                }
            }
            "remove" => {
                if let Some(id) = operation.get("id").and_then(Value::as_str) {
                    remove_surface(app, id);
                }
            }
            _ => {}
        }
    }
}

async fn scene_loop(
    app: AppHandle,
    mut outgoing: mpsc::UnboundedReceiver<Value>,
    scene_url: String,
) {
    loop {
        match connect_async(scene_url.as_str()).await {
            Ok((socket, _)) => {
                let (mut writer, mut reader) = socket.split();
                let mut visibility_tick = tokio::time::interval(Duration::from_secs(2));
                visibility_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                let hello = json!({
                    "v": 1,
                    "type": "hello",
                    "shell": "ev-tauri",
                    "shellVersion": "0.1.0",
                    "caps": ["scene", "patch", "animated-bounds", "surface-apps"],
                });
                let _ = writer.send(Message::Text(hello.to_string().into())).await;
                loop {
                    tokio::select! {
                        _ = visibility_tick.tick() => {
                            reconcile_visibility(&app);
                        }
                        Some(message) = outgoing.recv() => {
                            if writer.send(Message::Text(message.to_string().into())).await.is_err() {
                                break;
                            }
                        }
                        incoming = reader.next() => {
                            match incoming {
                                Some(Ok(Message::Text(text))) => {
                                    let Ok(message) = serde_json::from_str::<Value>(&text) else { continue };
                                    match message.get("type").and_then(Value::as_str).unwrap_or_default() {
                                        "scene" => apply_full_scene(&app, &message),
                                        "scene.patch" => apply_patch(&app, &message),
                                        "ping" => {
                                            let pong = json!({"v": 1, "type": "pong"});
                                            let _ = writer.send(Message::Text(pong.to_string().into())).await;
                                        }
                                        _ => {}
                                    }
                                }
                                Some(Ok(Message::Ping(payload))) => {
                                    let _ = writer.send(Message::Pong(payload)).await;
                                }
                                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                                _ => {}
                            }
                        }
                    }
                }
            }
            Err(_) => {}
        }
        tokio::time::sleep(Duration::from_millis(1200)).await;
    }
}

#[tauri::command]
fn surface_get(surface_id: String, runtime: State<'_, SceneRuntime>) -> Option<Value> {
    runtime.cache.lock().ok()?.surfaces.get(&surface_id).cloned()
}

#[tauri::command]
fn surface_present(
    surface_id: String,
    app: AppHandle,
    runtime: State<'_, SceneRuntime>,
) -> Result<(), String> {
    let label = surface_label(&surface_id);
    let window = app.get_window(&label).ok_or("surface not registered")?;
    let surface = runtime
        .cache
        .lock()
        .ok()
        .and_then(|cache| cache.surfaces.get(&surface_id).cloned())
        .ok_or("surface state unavailable")?;
    if surface.get("visible").and_then(Value::as_bool) != Some(true) {
        send_ready(&window, &surface_id, &runtime);
        return Ok(());
    }
    window.show().map_err(|error| error.to_string())?;
    if surface.get("focus").and_then(Value::as_bool) == Some(true) {
        let _ = window.set_focus();
    }
    sync_remote_content(&app, &surface_id, &surface);
    send_ready(&window, &surface_id, &runtime);
    Ok(())
}

#[tauri::command]
fn surface_hidden(
    surface_id: String,
    app: AppHandle,
    runtime: State<'_, SceneRuntime>,
) -> Result<(), String> {
    let window = app
        .get_window(&surface_label(&surface_id))
        .ok_or("surface not registered")?;
    window.hide().map_err(|error| error.to_string())?;
    send_ready(&window, &surface_id, &runtime);
    Ok(())
}

#[tauri::command]
fn surface_close(
    surface_id: String,
    app: AppHandle,
    runtime: State<'_, SceneRuntime>,
) -> Result<(), String> {
    let window = app
        .get_window(&surface_label(&surface_id))
        .ok_or("surface not registered")?;
    if let Ok(mut cache) = runtime.cache.lock() {
        if let Some(surface) = cache.surfaces.get_mut(&surface_id) {
            surface["visible"] = Value::Bool(false);
            surface["focus"] = Value::Bool(false);
        }
    }
    send_intent(&runtime, &surface_id, "surface.close", json!({}));
    window.hide().map_err(|error| error.to_string())?;
    send_ready(&window, &surface_id, &runtime);
    Ok(())
}

#[tauri::command]
fn surface_event(
    surface_id: String,
    app: AppHandle,
    runtime: State<'_, SceneRuntime>,
    name: String,
    data: Value,
) -> Result<(), String> {
    let window = app
        .get_window(&surface_label(&surface_id))
        .ok_or("surface not registered")?;
    if matches!(name.as_str(), "content.loading" | "content.ready" | "content.error") {
        if let Ok(mut cache) = runtime.cache.lock() {
            let receipt = cache.receipts.entry(surface_id.clone()).or_default();
            receipt.content_status = name.split('.').nth(1).unwrap_or("unknown").to_string();
            receipt.content_url = data.get("url").and_then(Value::as_str).unwrap_or_default().to_string();
            receipt.content_error = data.get("error").and_then(Value::as_str).unwrap_or_default().to_string();
        }
        let event_name = format!("surface.{}", name.replace('.', "_"));
        send_intent(&runtime, &surface_id, &event_name, data);
        send_ready(&window, &surface_id, &runtime);
    } else if name == "content.size" {
        send_intent(&runtime, &surface_id, "surface.content_size", data);
    } else {
        send_intent(
            &runtime,
            &surface_id,
            "surface.event",
            json!({"name": name, "payload": data}),
        );
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let (outgoing_tx, outgoing_rx) = mpsc::unbounded_channel();
    let scene_url = std::env::var("MUSE_SCENE_WS")
        .unwrap_or_else(|_| "ws://127.0.0.1:8002/api/scene".to_string());
    tauri::Builder::default()
        .manage(SceneRuntime {
            cache: Mutex::new(SceneCache::default()),
            outgoing: outgoing_tx,
        })
        .invoke_handler(tauri::generate_handler![
            surface_get,
            surface_present,
            surface_hidden,
            surface_close,
            surface_event,
        ])
        .setup(move |app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            tauri::async_runtime::spawn(scene_loop(
                app.handle().clone(),
                outgoing_rx,
                scene_url,
            ));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run EV Tauri shell");
}
