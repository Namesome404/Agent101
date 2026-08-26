"""护栏体检报告：把每层补偿层的「在场」「触发」「后果」摊开。

背景：app.py 里有一整套只为纠正模型行为而存在的东西——路由卡、回执提示、
世界快照、回炉判定、抓到吹牛后强制调工具、绕圈上限。它们是护栏不是功能，
但谁真的管用、谁只是心理安慰，一直没人答得上来：命中只有 print，进程一重启
就没了。

现在每轮语音都往 trace 里写一份 guards 体检表，这个脚本按层统计：

    python -m diagnostics.guard_report            # 全部
    python -m diagnostics.guard_report --last 200 # 只看最近 200 轮

读法（重要，别看错）：
  * 触发类的层（回炉判定、强制调工具、绕圈纠正、硬上限）能直接算命中率，
    命中就是它干活了。
  * 注入类的层（路由卡、世界快照）每轮都在，光看在场率说明不了任何事。
    只有回执提示是有条件注入的，所以「有它」和「没它」两组能对比——
    这是唯一一层能从日常流量里读出效果的注入层。
"""

import argparse
import collections
import json

from devices.coding.turn_trace import TRACE_PATH


def _turns(last=0):
    """按 turn_id 归并成一轮一条：用户说了什么、护栏体检表是什么。"""
    if not TRACE_PATH.exists():
        return []
    rows = collections.OrderedDict()
    text = TRACE_PATH.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        tid = item.get("turn_id")
        if not tid:
            continue
        data = item.get("data") or {}
        row = rows.setdefault(tid, {"user": "", "guards": None, "retry": None})
        event = item.get("event")
        if event == "user":
            row["user"] = str(data.get("text") or "")
        elif event == "assistant" and isinstance(data.get("guards"), dict):
            row["guards"] = data["guards"]
        elif event == "answer_retry":
            row["retry"] = data
    out = [r for r in rows.values() if r["guards"]]
    return out[-last:] if last else out


def _pct(hit, total):
    return "%d/%d (%.0f%%)" % (hit, total, (100.0 * hit / total) if total else 0)


def report(last=0):
    turns = _turns(last)
    if not turns:
        print("还没有带体检表的轮次。先正常说几句话，再回来跑这个脚本。")
        return
    n = len(turns)
    print("样本：%d 轮语音\n" % n)

    print("== 触发类：命中就是它干了活 ==")
    reasons = collections.Counter(
        t["guards"].get("retry_reason") for t in turns if t["guards"].get("retry_reason")
    )
    fired = sum(reasons.values())
    print("  回炉判定 _voice_answer_retry   %s" % _pct(fired, n))
    for reason, count in reasons.most_common():
        print("      %-22s %d" % (reason, count))
    print("  抓到吹牛后强制调工具            %s"
          % _pct(sum(1 for t in turns if t["guards"].get("forced_required")), n))
    print("  空转纠正 spin_corrected        %s"
          % _pct(sum(1 for t in turns if t["guards"].get("spin_corrected")), n))
    print("  绕圈硬上限 ACTION_HARD_LIMIT   %s"
          % _pct(sum(1 for t in turns if t["guards"].get("hard_limit_hit")), n))

    print("\n== 回炉之后到底救回来没有 ==")
    retried = [t for t in turns if t["guards"].get("retry_reason")]
    if not retried:
        print("  这批样本里一次都没回炉。")
    else:
        saved = sum(1 for t in retried if t["guards"].get("had_mutation_receipt"))
        print("  回炉的 %d 轮里，最终真拿到变更回执的：%s"
              % (len(retried), _pct(saved, len(retried))))
        print("  （没拿到回执的那些，回炉只是让它把话重说了一遍，没解决问题）")

    print("\n== 注入类：每轮都在，从日常流量读不出效果 ==")
    print("  这两层每轮无条件注入，没有对照组，命中率恒等于 100%——")
    print("  在场率说明不了任何事，要判断效果只能做 A/B。")
    print("  （原先还有第三层「回执提示」是有条件注入的，本可用来做对比；")
    print("    实测它恒返回空字符串，是个空壳，已删。）")

    always = collections.Counter()
    for t in turns:
        for key in ("routing_card", "world_snapshot"):
            if t["guards"].get(key):
                always[key] += 1
    print("\n  路由卡在场    %s（每轮都注入，在场率说明不了效果）"
          % _pct(always["routing_card"], n))
    print("  世界快照在场  %s（同上）" % _pct(always["world_snapshot"], n))

    print("\n== 这批样本的总体表现 ==")
    print("  调了工具的轮次      %s"
          % _pct(sum(1 for t in turns if t["guards"].get("had_tool_call")), n))
    print("  拿到变更回执的轮次  %s"
          % _pct(sum(1 for t in turns if t["guards"].get("had_mutation_receipt")), n))
    rounds = [t["guards"].get("action_rounds") or 0 for t in turns]
    print("  平均绕圈数          %.1f（最多 %d）"
          % (sum(rounds) / float(len(rounds)), max(rounds)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last", type=int, default=0, help="只看最近 N 轮")
    args = parser.parse_args()
    report(args.last)


if __name__ == "__main__":
    main()
