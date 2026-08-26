import React, { useEffect, useMemo, useState } from "react";
import { Streamdown } from "streamdown";
import { prepare, layout } from "@chenglou/pretext";

function usePretextHeight(text, width = 560, font = "16px IBM Plex Sans") {
  return useMemo(() => {
    try {
      const prepared = prepare(String(text || ""), font);
      const result = layout(prepared, width, 24);
      return (result && (result.height || result.lineCount * 24)) || 24;
    } catch {
      const lines = Math.max(1, String(text || "").split("\n").length);
      return lines * 24;
    }
  }, [text, width, font]);
}

function StreamBlock({ text }) {
  const height = usePretextHeight(text);
  return (
    <div className="desk-md" style={{ minHeight: height }}>
      <Streamdown>{text || ""}</Streamdown>
    </div>
  );
}

export function DeskApp({ windowId }) {
  const [win, setWin] = useState(null);
  const [buffers, setBuffers] = useState({});

  useEffect(() => {
    let es;
    fetch("/api/desk/windows/" + encodeURIComponent(windowId))
      .then((r) => r.json())
      .then((d) => d.window && setWin(d.window))
      .catch(() => {});
    es = new EventSource("/api/desk/" + encodeURIComponent(windowId) + "/events");
    es.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data);
        if (ev.type === "schema" || ev.type === "state") {
          if (ev.window) setWin(ev.window);
        } else if (ev.type === "preview" && ev.window_id === windowId) {
          setWin((w) =>
            w
              ? {
                  ...w,
                  preview_url: ev.preview_url || w.preview_url,
                  preview_locked: !!ev.preview_locked,
                }
              : w
          );
        } else if (ev.type === "text_delta") {
          setBuffers((b) => ({ ...b, [ev.block_id]: ev.text || "" }));
        } else if (ev.type === "log_event") {
          setBuffers((b) => ({
            ...b,
            activity: ((b.activity || "") + "\n" + (ev.entry?.text || "")).trim(),
          }));
        }
      } catch {}
    };
    return () => es && es.close();
  }, [windowId]);

  useEffect(() => {
    if (!win?.css_vars) return;
    Object.entries(win.css_vars).forEach(([k, v]) =>
      document.documentElement.style.setProperty(k, v)
    );
  }, [win]);

  if (!win) {
    return (
      <div className="desk-shell">
        <div className="desk-body">
          <p className="desk-md">窗口加载中或尚未创建…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="desk-shell">
      <header className="desk-head">
        <h1>{win.title || "EV Desk"}</h1>
      </header>
      <main className="desk-body">
        {(win.sections || []).map((sec, i) => (
          <section className="desk-section" key={i}>
            {sec.title ? <h2>{sec.title}</h2> : null}
            {(sec.blocks || []).map((blk, j) => {
              if (blk.type === "markdown") {
                const text = buffers[blk.id] ?? blk.text ?? "";
                return <StreamBlock key={j} text={text} />;
              }
              if (blk.type === "iframe") {
                const src = blk.src || win.preview_url || "";
                return (
                  <div
                    key={j}
                    className={"desk-preview-wrap" + (win.preview_locked ? " locked" : "")}
                  >
                    <iframe title="preview" src={src} />
                    <div className="desk-lock">正在改…</div>
                  </div>
                );
              }
              if (blk.type === "log") {
                const text = buffers[blk.id] ?? buffers.activity ?? blk.text ?? "";
                return (
                  <pre className="desk-log" key={j}>
                    {text}
                  </pre>
                );
              }
              return (
                <div key={j} className="desk-md">
                  {blk.type}: {blk.text || blk.label || ""}
                </div>
              );
            })}
          </section>
        ))}
      </main>
    </div>
  );
}
