# -*- coding: utf-8 -*-
"""通用表单：需要问用户几个问题时，开一扇窗让人填，答案回到发问的那一方。

为什么要有这层：语音适合问一两句，不适合问「工作目录、目标平台、要不要保留旧
数据」这种一次五六项的事——用嘴念一遍选项，用户记不住，模型也听不准。让工作
Agent 在开工前把要问的摆成一张表，人扫一眼填完，比来回对话快得多。

之前代码里有一条这样的路：app.py 认 source.type == "project-plan" 的 plan.update
/ plan.submit 事件，收到就写回工作单。但全仓库没有任何地方创建这种窗口——有接收
端、没生产端。这里把这条路补完，并且做成通用的：任何一方都能声明一张表。

渲染刻意不用 agent 手写的 HTML：那种内容跑在 sandbox iframe 里（没有
allow-same-origin），够不着桌面壳的回传桥，答案根本传不出来。改成 EV 自己按字段
声明生成页面、用 url 窗口打开——那条路挂的是子 webview，页面直接 fetch 回 EV，
一行前端都不用改。顺带这也意味着表单在任何能开网页的地方都成立。

答案的归属写在 owner 里：run 表示某次工作 Agent 运行，voice 表示这一轮对话。
发问的一方拿 answers 时才知道人填了什么，中间不经过模型转述。
"""
from __future__ import annotations

import html
import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

# 表单是短命的：问完就没用了。放内存，进程重启就丢——重启时那次提问的上下文
# 本来也没了，为它加一张表不值得。
_FORMS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_KEEP = 40

_FIELD_TYPES = {"text", "textarea", "choice", "bool"}


def _now() -> float:
    return time.time()


def declare(
    title: str,
    fields: List[Dict[str, Any]],
    *,
    owner_kind: str = "voice",
    owner_id: str = "",
    intro: str = "",
) -> Dict[str, Any]:
    """声明一张表。返回 form_id 与该开的页面地址。

    字段只支持四种：一行字、一段字、单选、是否。够用且都能一眼看懂——
    表单是为了「扫一眼填完」，不是为了把配置界面搬进来。
    """
    clean: List[Dict[str, Any]] = []
    for index, raw in enumerate(fields or []):
        raw = raw if isinstance(raw, dict) else {}
        kind = str(raw.get("type") or "text").strip().lower()
        if kind not in _FIELD_TYPES:
            kind = "text"
        key = str(raw.get("key") or "").strip() or "f%d" % (index + 1)
        options = [str(item) for item in (raw.get("options") or []) if str(item).strip()]
        if kind == "choice" and not options:
            kind = "text"
        clean.append({
            "key": key,
            "type": kind,
            "label": str(raw.get("label") or key)[:120],
            "hint": str(raw.get("hint") or "")[:160],
            "required": bool(raw.get("required")),
            "options": options[:12],
            "default": str(raw.get("default") or "")[:200],
        })
    if not clean:
        raise ValueError("表单至少要有一个字段")

    form_id = uuid.uuid4().hex[:12]
    item = {
        "form_id": form_id,
        "title": str(title or "请补充几项")[:80],
        "intro": str(intro or "")[:300],
        "fields": clean,
        "owner": {"kind": str(owner_kind or "voice"), "id": str(owner_id or "")},
        "created_at": _now(),
        "answers": None,
        "answered_at": 0.0,
    }
    with _LOCK:
        _FORMS[form_id] = item
        if len(_FORMS) > _KEEP:
            for stale in sorted(_FORMS, key=lambda k: _FORMS[k]["created_at"])[:-_KEEP]:
                _FORMS.pop(stale, None)
    return {
        "form_id": form_id,
        "path": "/forms/%s" % form_id,
        "fields": len(clean),
    }


def get(form_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        item = _FORMS.get(str(form_id or ""))
        return json.loads(json.dumps(item)) if item else None


def submit(form_id: str, answers: Dict[str, Any]) -> Dict[str, Any]:
    """收下答案。缺必填项就退回去，不半截收下。"""
    with _LOCK:
        item = _FORMS.get(str(form_id or ""))
        if not item:
            return {"ok": False, "error": "这张表已经过期或不存在"}
        if item.get("answers") is not None:
            return {"ok": False, "error": "这张表已经提交过了", "answers": item["answers"]}
        given = answers if isinstance(answers, dict) else {}
        collected, missing = {}, []
        for field in item["fields"]:
            key = field["key"]
            value = given.get(key)
            if field["type"] == "bool":
                collected[key] = bool(value) and str(value).lower() not in ("false", "0", "")
                continue
            text = str(value or "").strip()
            if not text and field["required"]:
                missing.append(field["label"])
            collected[key] = text[:2000]
        if missing:
            return {"ok": False, "error": "还差：%s" % "、".join(missing[:6]), "missing": missing}
        item["answers"] = collected
        item["answered_at"] = _now()
        owner = dict(item["owner"])
    return {"ok": True, "form_id": form_id, "answers": collected, "owner": owner}


def answers_for(owner_kind: str, owner_id: str = "") -> List[Dict[str, Any]]:
    """发问的一方来取答案：只拿已经填完的，按时间先后。"""
    kind, ident = str(owner_kind or ""), str(owner_id or "")
    with _LOCK:
        items = [
            item for item in _FORMS.values()
            if item.get("answers") is not None
            and item["owner"].get("kind") == kind
            and (not ident or item["owner"].get("id") == ident)
        ]
    items.sort(key=lambda i: i["answered_at"])
    return [
        {"form_id": i["form_id"], "title": i["title"], "answers": dict(i["answers"])}
        for i in items
    ]


def forget_all() -> None:
    with _LOCK:
        _FORMS.clear()


def surface_id_of(form_id: str) -> str:
    """每张表一扇自己的窗。

    不能让它按网址推导 id——那样所有表都落在同一个 host 上，第二张表会顶掉
    第一张，两次运行同时提问就互相覆盖。
    """
    return "form-%s" % str(form_id or "")[:12]


def open_window(form_id: str, *, title: str = "", base_url: str = "") -> Dict[str, Any]:
    """把表开成一扇窗。

    在这里发起而不是让调用方自己开：声明一张没人看得见的表没有任何用处——
    发问方会一直等答案，用户屏幕上却什么都没发生。

    走 url 窗口是有意的：那条挂子 webview，页面是普通网页，能直接 fetch 回
    EV 提交。EV 自己生成的 html 内容窗跑在 sandbox iframe 里，答案传不出来。
    """
    import os

    item = get(form_id)
    if not item:
        return {"ok": False, "error": "这张表不存在"}
    root = base_url or os.environ.get("MUSE_URL") or "http://127.0.0.1:%s" % os.environ.get("MUSE_PORT", "8002")
    try:
        from tools import surface_control

        _, meta = surface_control.execute({
            "action": "create",
            "surface_id": surface_id_of(form_id),
            "url": "%s/forms/%s" % (root.rstrip("/"), form_id),
            "title": title or item["title"],
            "width": 680,
            "height": 560,
            "position": "center",
        })
        return {
            "ok": bool(meta.get("ok")),
            "surface_id": surface_id_of(form_id),
            "detail": str(meta.get("reason") or meta.get("error") or "")[:120],
        }
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def close_window(form_id: str) -> Dict[str, Any]:
    """填完就把窗收走，别留在屏幕上。"""
    try:
        from tools import surface_control

        _, meta = surface_control.execute({
            "action": "close", "surface_id": surface_id_of(form_id),
        })
        return {"ok": bool(meta.get("ok"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


def render_page(form_id: str) -> str:
    """按字段声明生成页面。

    页面自己 fetch 回 EV 提交——它跑在 url 窗口的子 webview 里，是一个普通网页，
    不受 sandbox 限制，所以不需要桌面壳做任何转发。
    """
    item = get(form_id)
    if not item:
        return "<!doctype html><meta charset=utf-8><body>这张表已经过期。</body>"

    def esc(text):
        return html.escape(str(text or ""), quote=True)

    rows = []
    for field in item["fields"]:
        key, kind = esc(field["key"]), field["type"]
        label = esc(field["label"]) + ("<i>必填</i>" if field["required"] else "")
        hint = "<p class=hint>%s</p>" % esc(field["hint"]) if field["hint"] else ""
        if kind == "textarea":
            control = '<textarea name="%s" rows="4">%s</textarea>' % (key, esc(field["default"]))
        elif kind == "choice":
            opts = "".join(
                '<label class=opt><input type=radio name="%s" value="%s"%s><span>%s</span></label>'
                % (key, esc(o), " checked" if o == field["default"] else "", esc(o))
                for o in field["options"]
            )
            control = '<div class=opts>%s</div>' % opts
        elif kind == "bool":
            control = ('<label class=opt><input type=checkbox name="%s"%s><span>是</span></label>'
                       % (key, " checked" if field["default"] else ""))
        else:
            control = '<input type="text" name="%s" value="%s">' % (key, esc(field["default"]))
        rows.append(
            '<div class=row><label class=lab for="%s">%s</label>%s%s</div>'
            % (key, label, hint, control)
        )

    intro = "<p class=intro>%s</p>" % esc(item["intro"]) if item["intro"] else ""
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title><style>
:root{color-scheme:light dark;--ink:#16161f;--sub:#65657a;--line:#dcdce4;--bg:#f6f6f9;--card:#fff;--acc:#4f47a8}
@media (prefers-color-scheme:dark){:root{--ink:#ececf3;--sub:#8f8fa4;--line:#33333f;--bg:#131319;--card:#1c1c25;--acc:#a49cf0}}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--ink);
 font:15px/1.7 "PingFang SC","Hiragino Sans GB",system-ui,sans-serif}
h1{font-size:1.15rem;margin:0 0 6px}
.intro{margin:0 0 18px;color:var(--sub);font-size:.92rem}
form{display:flex;flex-direction:column;gap:16px;max-width:620px}
.row{display:flex;flex-direction:column;gap:6px;background:var(--card);
 border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.lab{font-weight:600}
.lab i{font-style:normal;font-size:.72rem;color:var(--acc);margin-left:6px}
.hint{margin:0;color:var(--sub);font-size:.85rem}
input[type=text],textarea{font:inherit;color:inherit;background:transparent;
 border:1px solid var(--line);border-radius:6px;padding:8px 10px;width:100%%}
textarea{resize:vertical}
.opts{display:flex;flex-wrap:wrap;gap:14px}
.opt{display:inline-flex;align-items:center;gap:6px;cursor:pointer}
button{font:inherit;font-weight:600;color:#fff;background:var(--acc);border:0;
 border-radius:8px;padding:11px 20px;cursor:pointer;align-self:flex-start}
button:disabled{opacity:.5;cursor:default}
#say{min-height:1.5em;color:var(--sub);font-size:.9rem}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
</style></head><body>
<h1>%(title)s</h1>%(intro)s
<form id="f">%(rows)s
<button type=submit>提交</button><div id=say></div></form>
<script>
const f=document.getElementById('f'),say=document.getElementById('say');
f.addEventListener('submit',async e=>{
  e.preventDefault();
  const btn=f.querySelector('button');btn.disabled=true;say.textContent='正在提交…';
  const data={};
  for(const el of f.elements){
    if(!el.name)continue;
    if(el.type==='checkbox')data[el.name]=el.checked;
    else if(el.type==='radio'){if(el.checked)data[el.name]=el.value}
    else data[el.name]=el.value;
  }
  try{
    const r=await fetch('/api/forms/%(fid)s/submit',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:data})});
    const j=await r.json();
    if(j.ok){say.textContent='提交好了，可以关掉这扇窗。';f.querySelectorAll('input,textarea').forEach(x=>x.disabled=true)}
    else{say.textContent=j.error||'没提交成功';btn.disabled=false}
  }catch(err){say.textContent='连不上 EV，先别关窗，稍后再试。';btn.disabled=false}
});
</script></body></html>""" % {
        "title": esc(item["title"]),
        "intro": intro,
        "rows": "".join(rows),
        "fid": esc(form_id),
    }
