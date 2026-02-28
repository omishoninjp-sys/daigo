"""
HTTP proxy 轉發器（純 threading，零 asyncio 依賴）
用法: python run_pproxy.py <local_port> <upstream_host> <upstream_port> [username] [password]

Chrome → 本地 127.0.0.1:local_port → 上游 proxy（帶認證）→ 目標網站
"""
import sys, socket, threading, base64, select, time

DEBUG = True

def log(msg):
    if DEBUG:
        print(f"[PROXY] {msg}", flush=True)

def handle_client(client_sock, upstream_host, upstream_port, proxy_auth):
    upstream = None
    try:
        # 讀取客戶端請求（Chrome 發來的）
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
        log(f"請求: {method} {target}")

        # 連線到上游 proxy
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(15)
        try:
            upstream.connect((upstream_host, upstream_port))
        except Exception as e:
            log(f"❌ 無法連線上游 proxy {upstream_host}:{upstream_port}: {e}")
            client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        # 注入 Proxy-Authorization header
        if proxy_auth:
            header_end = request.index(b"\r\n\r\n")
            auth_line = b"\r\nProxy-Authorization: Basic " + proxy_auth
            request = request[:header_end] + auth_line + request[header_end:]

        if method == "CONNECT":
            # HTTPS 隧道
            upstream.sendall(request)

            # 讀取上游 proxy 的回應
            resp = b""
            upstream.settimeout(15)
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    log(f"❌ 上游 proxy 關閉連線（CONNECT）")
                    client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                resp += chunk

            status_line = resp.split(b"\r\n")[0].decode(errors='replace')
            log(f"上游回應: {status_line}")

            if b"200" in resp.split(b"\r\n")[0]:
                # 告訴 Chrome 隧道已建立
                client_sock.sendall(resp)
                # 雙向轉發
                _tunnel_bidirectional(client_sock, upstream)
            else:
                log(f"❌ CONNECT 失敗: {status_line}")
                client_sock.sendall(resp)
        else:
            # 普通 HTTP proxy 請求
            upstream.sendall(request)
            upstream.settimeout(30)
            while True:
                try:
                    chunk = upstream.recv(8192)
                    if not chunk:
                        break
                    client_sock.sendall(chunk)
                except socket.timeout:
                    break

    except Exception as e:
        log(f"處理錯誤: {e}")
    finally:
        try: client_sock.close()
        except: pass
        try:
            if upstream: upstream.close()
        except: pass

def _tunnel_bidirectional(sock1, sock2):
    """用 select 做雙向轉發（比 threading 更可靠）"""
    sockets = [sock1, sock2]
    sock1.setblocking(False)
    sock2.setblocking(False)
    timeout_count = 0

    while True:
        try:
            readable, _, exceptional = select.select(sockets, [], sockets, 1.0)
        except:
            break

        if exceptional:
            break

        if not readable:
            timeout_count += 1
            if timeout_count > 120:  # 2 分鐘無資料
                break
            continue

        timeout_count = 0
        closed = False
        for sock in readable:
            other = sock2 if sock is sock1 else sock1
            try:
                data = sock.recv(16384)
                if not data:
                    closed = True
                    break
                other.sendall(data)
            except (BlockingIOError, socket.error):
                pass
            except:
                closed = True
                break

        if closed:
            break

def main():
    local_port = int(sys.argv[1])
    upstream_host = sys.argv[2]
    upstream_port = int(sys.argv[3])
    username = sys.argv[4] if len(sys.argv) > 4 else ""
    password = sys.argv[5] if len(sys.argv) > 5 else ""

    proxy_auth = None
    if username and password:
        cred = base64.b64encode(f"{username}:{password}".encode())
        proxy_auth = cred

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(10)
    log(f"✅ 監聽 127.0.0.1:{local_port} → {upstream_host}:{upstream_port}")

    while True:
        try:
            client, addr = server.accept()
            t = threading.Thread(
                target=handle_client,
                args=(client, upstream_host, upstream_port, proxy_auth),
                daemon=True,
            )
            t.start()
        except Exception as e:
            log(f"accept 錯誤: {e}")

if __name__ == "__main__":
    main()
