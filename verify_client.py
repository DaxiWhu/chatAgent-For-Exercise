"""仅用标准库验证 serve.py 的 /chat 与 /review（SSE 流式）。
已避开 git-bash 中文编码问题：请求体用 UTF-8 字节，逐行解析 SSE。
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def post_sse(path, payload):
    """发送 POST，返回解析后的 SSE 事件列表 [(event, data_dict_or_str), ...]"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    events = []
    cur_event = None
    cur_data = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
            if line.startswith("event:"):
                cur_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                cur_data.append(line[len("data:"):].strip())
            elif line == "":  # 事件分隔
                if cur_event is not None:
                    blob = "\n".join(cur_data)
                    try:
                        parsed = json.loads(blob)
                    except Exception:
                        parsed = blob
                    events.append((cur_event, parsed))
                cur_event, cur_data = None, []
    return events


def main():
    tid = "verify-%d" % int(time.time())
    msg = "请写一段关于梧州的三句话介绍。"
    print(">>> POST /chat  thread_id=%s" % tid)
    print(">>> message=%s" % msg)
    chat_events = post_sse("/chat", {"thread_id": tid, "message": msg})

    tokens, last = [], None
    for ev, data in chat_events:
        if ev == "token":
            tokens.append(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
        else:
            last = (ev, data)
        print("  [event=%-10s] %s" % (ev, data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)))

    full = "".join(tokens)
    print("\n<<< 生成的草稿 (%d 字符): %s" % (len(full), full[:200]))
    print("<<< 终态事件: %s" % (last,))

    if last and last[0] == "need_review":
        print("\n>>> 触发人工审核，POST /review action=accept")
        rev_events = post_sse("/review", {
            "thread_id": tid, "action": "accept", "feedback": "",
        })
        rev_tokens = []
        for ev, data in rev_events:
            if ev == "token":
                rev_tokens.append(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
            else:
                print("  [event=%-10s] %s" % (ev, data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)))
        print("<<< 审核后输出: %s" % "".join(rev_tokens)[:200])
    else:
        print("\n>>> （未触发人工审核，跳过 /review 真实调用；单独测 /review 非法状态返回）")
        try:
            post_sse("/review", {"thread_id": tid, "action": "accept", "feedback": ""})
        except urllib.error.HTTPError as e:
            print("<<< /review 非审核态返回 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")))


if __name__ == "__main__":
    main()
