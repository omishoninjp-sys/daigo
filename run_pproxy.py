"""
簡易 HTTP proxy 轉發器（純 threading，不依賴 asyncio / pproxy）
用法: python run_pproxy.py <local_port> <upstream_host> <upstream_port> <username> <password>
"""
import sys, socket, threading, base64

def handle_client(client_sock, upstream_host, upstream_port, auth_header):
    try:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = client_sock.recv(4096)
            if not chunk:
                break
            request += chunk

        if not request:
            client_sock.close()
            return

        # 注入 Proxy-Authorization header
        if auth_header:
            header_end = request.index(b"\r\n\r\n")
            request = request[:header_end] + b"\r\nProxy-Authorization: Basic " + auth_header + request[header_end:]

        # 判斷 CONNECT（HTTPS）還是普通 HTTP
        first_line = request.split(b"\r\n")[0]
        method = first_line.split(b" ")[0]

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(30)
        upstream.connect((upstream_host, upstream_port))

        if method == b"CONNECT":
            # HTTPS tunnel
            upstream.sendall(request)
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                resp += chunk
            client_sock.sendall(resp)
            if b"200" in resp.split(b"\r\n")[0]:
                _tunnel(client_sock, upstream)
        else:
            # HTTP proxy
            upstream.sendall(request)
            while True:
                chunk = upstream.recv(8192)
                if not chunk:
                    break
                client_sock.sendall(chunk)

    except Exception:
        pass
    finally:
        try: client_sock.close()
        except: pass
        try: upstream.close()
        except: pass

def _tunnel(sock1, sock2):
    """雙向轉發"""
    def forward(src, dst):
        try:
            while True:
                data = src.recv(8192)
                if not data:
                    break
                dst.sendall(data)
        except:
            pass
        try: dst.shutdown(socket.SHUT_WR)
        except: pass

    t1 = threading.Thread(target=forward, args=(sock1, sock2), daemon=True)
    t2 = threading.Thread(target=forward, args=(sock2, sock1), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=120)
    t2.join(timeout=120)

def main():
    local_port = int(sys.argv[1])
    upstream_host = sys.argv[2]
    upstream_port = int(sys.argv[3])
    username = sys.argv[4] if len(sys.argv) > 4 else ""
    password = sys.argv[5] if len(sys.argv) > 5 else ""

    auth_header = None
    if username and password:
        cred = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header = cred.encode()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(5)

    while True:
        client, _ = server.accept()
        t = threading.Thread(target=handle_client, args=(client, upstream_host, upstream_port, auth_header), daemon=True)
        t.start()

if __name__ == "__main__":
    main()
