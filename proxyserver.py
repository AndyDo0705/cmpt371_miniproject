
import socket, threading, sys, time, re
from urllib.parse import urlsplit

BUF = 1024
CACHE = {}

# helper functions to retrieve data from headers
def parse_headers(data: bytes):
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines: return None, None, None #if empty
    try:
        method, target, version = lines[0].split(b" ", 2)
    except ValueError:
        return None, None, None
    headers = []
    for ln in lines[1:]:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            headers.append((k.strip(), v.strip()))
    return (method, target, version), headers, rest

def read_until_headers(sock):
    sock.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(BUF)
        if not chunk: break
        data += chunk
    return data

def get_header(headers, name: bytes):
    n = name.lower()
    for k, v in headers:
        if k.lower() == n:
            return v
    return None

def set_header(headers, k: bytes, v: bytes):
    lk = k.lower()
    for i, (hk, hv) in enumerate(headers):
        if hk.lower() == lk:
            headers[i] = (k, v); return
    headers.append((k, v))

def build_headers_line(start: bytes, headers):
    blob = start + b"\r\n"
    for k, v in headers:
        blob += k + b": " + v + b"\r\n"
    blob += b"\r\n"
    return blob

def connect_and_forward(host, port, req_head: bytes, client_conn, body=b""):
    with socket.create_connection((host, port), timeout=10) as origin:
        origin.settimeout(10)
        origin.sendall(req_head)
        if body:
            origin.sendall(body)
        # relay response (headers + body) back to client
        while True:
            chunk = origin.recv(BUF)
            if not chunk: break
            client_conn.sendall(chunk)

def handle_client(conn, addr):
    try:
        data = read_until_headers(conn)
        if not data:
            conn.close(); return
        reqline, headers, after = parse_headers(data)
        if not reqline:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            conn.close(); return
        method, target, version = reqline
        method_u = method.upper()
        if method_u not in (b"GET", b"HEAD"):
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            conn.close(); return

        # parse target connection
        tgt = target.decode("ascii", "ignore")
        if re.match(r"^https?://", tgt):
            u = urlsplit(tgt)
            host = u.hostname
            port = u.port or (80 if u.scheme == "http" else 443)
            path = u.path or "/"
            if u.query: path += "?" + u.query
            url_key = f"{u.scheme}://{u.netloc}{path}"
        else:
            host_hdr = get_header(headers, b"Host")
            if not host_hdr:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                conn.close(); return
            host_port = host_hdr.decode("ascii", "ignore")
            if ":" in host_port:
                host, p = host_port.rsplit(":", 1)
                try: port = int(p)
                except: port = 80
            else:
                host, port = host_port, 80
            path = tgt or "/"
            url_key = f"http://{host_port}{path}"

        # ensure host is correct
        host_value = host.encode()
        if port != 80:
            host_value += b":" + str(port).encode()
        set_header(headers, b"Host", host_value)
        set_header(headers, b"Connection", b"close")

        # if we have a cached copy, send If-Modified-Since
        cached = CACHE.get(url_key)
        if cached and cached["lm"]:
            set_header(headers, b"If-Modified-Since", cached["lm"])

        start = method + b" " + path.encode() + b" " + b"HTTP/1.1"
        out_head = build_headers_line(start, headers)

        # contact origin and read full response (to possibly cache)
        with socket.create_connection((host, port), timeout=10) as origin:
            origin.settimeout(10)
            origin.sendall(out_head)
            # (GET/HEAD have no body here)
            # read response headers
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = origin.recv(BUF)
                if not chunk: break
                resp += chunk
            if not resp:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                conn.close(); return

            r_head, _, r_rest = resp.partition(b"\r\n\r\n")
            # forward headers as-is to client
            conn.sendall(r_head + b"\r\n\r\n")

            # parse status + headers to decide cache/update
            lines = r_head.split(b"\r\n")
            status = lines[0] if lines else b"HTTP/1.1 502 Bad Gateway"
            r_headers = []
            for ln in lines[1:]:
                if b":" in ln:
                    k, v = ln.split(b":", 1)
                    r_headers.append((k.strip(), v.strip()))

            # extract Last-Modified if present
            last_mod = get_header(r_headers, b"Last-Modified")

            # stream/collect body
            body_chunks = [r_rest]
            # if HEAD, no body to read
            if method_u != b"HEAD":
                # if Content-Length exists, read exactly that many more bytes
                clen = get_header(r_headers, b"Content-Length")
                if clen is not None:
                    need = int(clen) - len(r_rest)
                    while need > 0:
                        chunk = origin.recv(min(BUF, need))
                        if not chunk: break
                        body_chunks.append(chunk); need -= len(chunk)
                else:
                    # otherwise, read until close
                    while True:
                        chunk = origin.recv(BUF)
                        if not chunk: break
                        body_chunks.append(chunk)

            body_bytes = b"".join(body_chunks)
            # forward body to client
            if body_bytes:
                conn.sendall(body_bytes)

            # if 200 OK and body size is moderate then  store {lm, headers, body}
            try:
                status_code = int(status.split(b" ")[1])
            except Exception:
                status_code = 0
            if method_u == b"GET":
                if status_code == 200:
                    CACHE[url_key] = {"lm": last_mod, "body": body_bytes, "headers": r_headers, "ts": time.time()}
                elif status_code == 304 and cached:
                    pass

    except socket.timeout:
        try: conn.sendall(b"HTTP/1.1 504 Gateway Timeout\r\nConnection: close\r\n\r\n")
        except: pass
    except Exception:
        try: conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except: pass
    finally:
        try: conn.close()
        except: pass

def proxy(port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(10)
        print(f"[proxy] listening on 0.0.0.0:{port}")
        while True:
            c, a = s.accept()
            threading.Thread(target=handle_client, args=(c, a)).start()

if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8888 # use port 8888 if none if provided
    proxy(p)


