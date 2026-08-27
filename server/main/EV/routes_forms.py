# -*- coding: utf-8 -*-
"""表单的两个出入口：一个页面，一个提交。

页面跑在 url 窗口的子 webview 里，是普通网页，直接 fetch 回这里提交——
不经过桌面壳，所以壳一行都不用改。
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, JSONResponse

from control_plane import forms

router = APIRouter()


@router.post("/api/forms")
def form_declare(payload: dict = Body(default=None)):
    """声明一张表，默认顺手把窗口开出来。

    走 HTTP 而不是只留 Python 接口：发问的一方常常在另一个进程里——工作 Agent
    是独立的 CLI，它要问什么，只能通过接口告诉 EV。

    默认开窗，是因为「声明了一张没人看得见的表」没有任何用处：发问方会一直
    等答案，而用户屏幕上什么都没发生。要自己控制时机就传 open=false。
    """
    body = payload if isinstance(payload, dict) else {}
    try:
        created = forms.declare(
            str(body.get("title") or ""),
            body.get("fields") or [],
            owner_kind=str(body.get("owner_kind") or "voice"),
            owner_id=str(body.get("owner_id") or ""),
            intro=str(body.get("intro") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    if body.get("open", True):
        created["window"] = forms.open_window(
            created["form_id"], title=str(body.get("title") or ""),
        )
    return JSONResponse(dict(created, ok=True))


@router.get("/api/forms")
def form_answers(owner_kind: str = "voice", owner_id: str = ""):
    """发问的一方来取答案。"""
    return JSONResponse({"ok": True, "items": forms.answers_for(owner_kind, owner_id)})


@router.get("/forms/{form_id}", response_class=HTMLResponse)
def form_page(form_id: str):
    return HTMLResponse(forms.render_page(form_id))


@router.post("/api/forms/{form_id}/submit")
def form_submit(form_id: str, payload: dict = Body(default=None)):
    answers = (payload or {}).get("answers")
    result = forms.submit(form_id, answers if isinstance(answers, dict) else {})
    if result.get("ok"):
        # 填完就收走。留在屏幕上只会让人以为还没提交成功。
        # 关窗失败不影响这次提交——答案已经收下了，窗口是次要的。
        result["window_closed"] = bool(forms.close_window(form_id).get("ok"))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/forms/{form_id}")
def form_state(form_id: str):
    item = forms.get(form_id)
    if not item:
        return JSONResponse({"ok": False, "error": "不存在"}, status_code=404)
    return JSONResponse({"ok": True, "form": item})
