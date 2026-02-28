"""
HTTP proxy 轉發器（純 threading，零 asyncio 依賴）
用法: python run_pproxy.py <local_port> <upstream_host> <upstream_port> [username] [password]
"""
import sys, socket, threading, base64, time

DEBUG = True

def log(msg):
    if DEBUG:
        print(f"[PROXY] {msg}", flush=True)

def handle_client(client_sock, upstream_host, upstream_port, proxy_auth):
    upstream = None
    try:
        # 讀取客戶端請求
        request = b""
        client_sock.settimeout(10)
        while b"\r\n\r\n" not in request:
            chunk = client_sock.recv(4096)
            if not chunk:
                return
            request += chunk

        first_line = request.split(b"\r\n")[0].decode(errors='replace')
        method = first_line.split(" ")[0]
        target = first_line.split(" ")[1] if " " in first_line else "?"
        log(f"{method} {target[:60]}")

        # 連線上游 proxy
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(15)
        try:
            upstream.connect((upstream_host, upstream_port))
        except Exception as e:
            log(f"❌ 連線失敗 {upstream_host}:{upstream_port}: {e}")
            client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        # 注入 Proxy-Authorization
        if proxy_auth:
            header_end = request.index(b"\r\n\r\n")
            auth_line = b"\r\nProxy-Authorization: Basic " + proxy_auth
            request = request[:header_end] + auth_line + request[header_end:]

        if method == "CONNECT":
            upstream.sendall(request)
            # 讀取上游回應
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                resp += chunk

            status_line = resp.split(b"\r\n")[0].decode(errors='replace')
            if b"200" not in resp.split(b"\r\n")[0]:
                log(f"❌ CONNECT 失敗: {status_line}")
                client_sock.sendall(resp)
                return

            # 隧道建立成功
            client_sock.sendall(resp)

            # 設定 blocking + 長超時（TLS 需要）
            client_sock.settimeout(120)
            upstream.settimeout(120)

            # 用兩個 thread 做雙向轉發
            closed = threading.Event()

            def forward(src, dst, label):
                try:
                    while not closed.is_set():
                        try:
                            data = src.recv(32768)
                            if not data:
                                break
                            dst.sendall(data)
                        except socket.timeout:
                            continue
                        except:
                            break
                except:
                    pass
                finally:
                    closed.set()

            t1 = threading.Thread(target=forward, args=(client_sock, upstream, "C→U"), daemon=True)
            t2 = threading.Thread(target=forward, args=(upstream, client_sock, "U→C"), daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=180)
            t2.join(timeout=5)
        else:
            # 普通 HTTP
            upstream.sendall(request)
            upstream.settimeout(30)
            while True:
                try:
                    chunk = upstream.recv(8192)
                    if not chunk:
                        break
                    client_sock.sendall(chunk)
                except:
                    break

    except Exception as e:
        log(f"錯誤: {e}")
    finally:
        try: client_sock.close()
        except: pass
        try:
            if upstream: upstream.close()
        except: pass

def main():
    local_port = int(sys.argv[1])
    upstream_host = sys.argv[2]
    upstream_port = int(sys.argv[3])
    username = sys.argv[4] if len(sys.argv) > 4 else ""
    password = sys.argv[5] if len(sys.argv) > 5 else ""

    proxy_auth = None
    if username and password:
        proxy_auth = base64.b64encode(f"{username}:{password}".encode())

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(20)
    log(f"✅ 127.0.0.1:{local_port} → {upstream_host}:{upstream_port}")

    while True:
        try:
            client, _ = server.accept()
            threading.Thread(target=handle_client, args=(client, upstream_host, upstream_port, proxy_auth), daemon=True).start()
        except Exception as e:
            log(f"accept 錯誤: {e}")

if __name__ == "__main__":
    main()
