"""把 Windows 上只绑回环的代理端口，转发到 WSL 能访问的地址。

为什么需要它
------------
WSL2 是独立虚拟机（默认 NAT），Windows 的系统代理设置**不会**传进去；而代理软件
（FlClash / Clash 系）默认只监听 `127.0.0.1:7890`，从 WSL 的 `172.30.x.x` 发来的连接
到不了 —— 那个地址上根本没有 listen。

Linux 侧常见的解法是 `socat` 或改代理的 Allow LAN 开关。这里两者都不用：
- 不用 socat，因为 WSL 里可能没装，且多一个依赖；
- 不用 Allow LAN，因为改的是别人的软件配置，且 TUN 模式下这个开关可能压根不存在。

改成在 Windows 侧起一个纯标准库的 TCP 转发：`0.0.0.0:7891 -> 127.0.0.1:7890`。
绑 0.0.0.0 不需要管理员权限（netsh portproxy 才需要）。WSL 只要把
`http_proxy` 指向 `http://<网关IP>:7891` 即可。

⚠️ 这是 HTTP CONNECT 的**透明 TCP 转发**，不解析内容 —— 所以 HTTPS 的 CONNECT
隧道、SOCKS 握手都能原样过去。它不是"代理"，只是把连接接力到本地代理上。

用法
----
    python tools/win_proxy_forward.py            # 默认 7891 -> 7890
    python tools/win_proxy_forward.py -l 8899    # 换监听端口

用 WSL 侧验证：
    curl -x http://$(ip route show default | awk '{print $3}'):7891 https://www.google.com -sI
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading

BUF = 65536


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """单向搬运，直到任一端关闭。

    ⚠️ 必须两个方向各起一个线程：HTTP CONNECT 是**双向长连接**（客户端在等响应，
    同时还要往上传请求头）。只做一个方向会表现为"连上了但一直不出数据" ——
    看起来像代理慢，实际是单工。
    """
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # shutdown 而不 close：让另一端把残留数据读完再关，避免截断最后一个响应包。
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client: socket.socket, target_host: str, target_port: int) -> None:
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        upstream.connect((target_host, target_port))
    except OSError as exc:
        print(f"[forward] 连不上 {target_host}:{target_port} —— {exc}", file=sys.stderr)
        client.close()
        upstream.close()
        return

    t = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    t.start()
    _pipe(client, upstream)
    t.join(timeout=5)
    for s in (client, upstream):
        try:
            s.close()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="把回环代理转发到 WSL 可达的地址")
    ap.add_argument("-l", "--listen-port", type=int, default=7891, help="监听端口（默认 7891）")
    ap.add_argument("-t", "--target-port", type=int, default=7890, help="目标端口（默认 7890）")
    ap.add_argument("--target-host", default="127.0.0.1", help="目标地址（默认 127.0.0.1）")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", args.listen_port))
    except OSError as exc:
        print(f"绑定 0.0.0.0:{args.listen_port} 失败：{exc}", file=sys.stderr)
        return 1
    srv.listen(64)
    print(
        f"[forward] 0.0.0.0:{args.listen_port} -> {args.target_host}:{args.target_port}\n"
        f"[forward] WSL 侧设：export http_proxy=http://<网关IP>:{args.listen_port}",
        flush=True,
    )
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(
                target=_handle, args=(conn, args.target_host, args.target_port), daemon=True
            ).start()
    except KeyboardInterrupt:
        print("\n[forward] 停止")
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())