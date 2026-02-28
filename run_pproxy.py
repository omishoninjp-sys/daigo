"""
HTTP proxy 轉發器（純 threading）
用法: python run_pproxy.py <local_port> <upstream_host> <upstream_port> [username] [password]
"""
import sys, socket, threading, base64, time as _time

def log(msg):
    print(f"[PROXY] {msg}", flush=True)

def handle_client(client_sock, upstream_host, upstream_port, proxy_auth):
    upstream = None
    try:
        request = b""
        client_sock.settimeout(10)
        while b"\r\n\r\n" not in request:
            chunk = client_sock.recv(4096)
            if not chunk:
                return
            request += chunk

        first_line = request.split(b"\r\n")[0].decode(errors='replace')
        parts = first_line.split(" ")
        method = parts[0]
        target = parts[1] if len(parts) > 1 else "?"
        log(f"{method} {target[:60]}")

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(15)
        try:
            upstream.connect((upstream_host, upstream_port))
        except Exception as e:
            log(f"❌ 連線失敗: {e}")
            try: client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except: pass
            return

        if proxy_auth:
            idx = request.index(b"\r\n\r\n")
            request = request[:idx] + b"\r\nProxy-Authorization: Basic " + proxy_auth + request[idx:]

        if method == "CONNECT":
            upstream.sendall(request)
            resp = b""
            upstream.settimeout(15)
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    log(f"❌ 上游斷線")
                    try: client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    except: pass
                    return
                resp += chunk

            status_line = resp.split(b"\r\n")[0].decode(errors='replace')
            log(f"回應: {status_line}")

            if b"200" not in resp.split(b"\r\n")[0]:
                try: client_sock.sendall(resp)
                except: pass
                return

            sep = resp.index(b"\r\n\r\n") + 4
            http_resp = resp[:sep]
            extra_data = resp[sep:]

            client_sock.sendall(http_resp)
            if extra_data:
                client_sock.sendall(extra_data)

            _do_tunnel(client_sock, upstream, target[:40])
        else:
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


def _do_tunnel(client, upstream, label=""):
    done = threading.Event()
    stats = {"cu": 0, "uc": 0, "cu_reason": "?", "uc_reason": "?"}

    def forward(src, dst, counter_key, reason_key, name):
        try:
            src.settimeout(30)  # 30 秒無資料才超時
            while not done.is_set():
                try:
                    data = src.recv(65536)
                    if not data:
                        stats[reason_key] = "EOF(recv empty)"
                        break
                    dst.sendall(data)
                    stats[counter_key] += len(data)
                except socket.timeout:
                    # 30 秒沒資料，繼續等
                    if done.is_set():
                        stats[reason_key] = "done_flag"
                        break
                    continue
                except ConnectionResetError:
                    stats[reason_key] = "ConnectionReset"
                    break
                except BrokenPipeError:
                    stats[reason_key] = "BrokenPipe"
                    break
                except OSError as e:
                    stats[reason_key] = f"OSError({e.errno})"
                    break
        except Exception as e:
            stats[reason_key] = f"Exception({e})"
        finally:
            done.set()

    t1 = threading.Thread(target=forward, args=(client, upstream, "cu", "cu_reason", "C→U"), daemon=True)
    t2 = threading.Thread(target=forward, args=(upstream, client, "uc", "uc_reason", "U→C"), daemon=True)
    t1.start()
    t2.start()

    done.wait(timeout=180)

    log(f"隧道 {label}: ↑{stats['cu']} ↓{stats['uc']} | C→U:{stats['cu_reason']} U→C:{stats['uc_reason']}")

    try: client.close()
    except: pass
    try: upstream.close()
    except: pass
    t1.join(timeout=3)
    t2.join(timeout=3)


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
            log(f"accept: {e}")

if __name__ == "__main__":
    main()
