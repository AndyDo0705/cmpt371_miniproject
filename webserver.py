import socket
import os
import datetime

HOST = "127.0.0.1"
PORT = 8080

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((HOST, PORT))
server_socket.listen(5)

print(f"Serving HTTP on {HOST}:{PORT}")

try:
    while True:
        conn, addr = server_socket.accept()
        print(f"\nConnected by {addr}")

        request = conn.recv(1024).decode()
        if not request:
            conn.close()
            continue

        print(request.splitlines()[0])
        try:
            method, path, version = request.splitlines()[0].split()
        except ValueError:
            conn.close()
            continue

        filename = path.strip("/")

        # --- 505: HTTP Version Not Supported ---
        if version not in ["HTTP/1.0", "HTTP/1.1"]:
            response = "HTTP/1.1 505 HTTP Version Not Supported\r\n\r\n"
            conn.sendall(response.encode())
            print("→ Sent response: HTTP/1.1 505 HTTP Version Not Supported")
            conn.close()
            continue

        # --- 403: Method Not Allowed (only GET) ---
        if method != "GET":
            response = "HTTP/1.1 403 Forbidden\r\n\r\nMethod not allowed."
            conn.sendall(response.encode())
            print("→ Sent response: HTTP/1.1 403 Forbidden (invalid method)")
            conn.close()
            continue

        # --- 403: Forbidden file (hardcoded example) ---
        if filename == "secret.html":
            response = "HTTP/1.1 403 Forbidden\r\n\r\nAccess Denied."
            conn.sendall(response.encode())
            print("→ Sent response: HTTP/1.1 403 Forbidden (secret.html)")
            conn.close()
            continue

        # --- 404: File Not Found ---
        if not os.path.exists(filename):
            response = "HTTP/1.1 404 Not Found\r\n\r\nFile not found."
            conn.sendall(response.encode())
            print("→ Sent response: HTTP/1.1 404 Not Found")
            conn.close()
            continue

        # --- 304: Not Modified ---
        mtime = os.path.getmtime(filename)
        file_time = datetime.datetime.fromtimestamp(
            mtime, datetime.timezone.utc)

        if "If-Modified-Since:" in request:
            for line in request.splitlines():
                if line.startswith("If-Modified-Since:"):
                    since_str = line.split(":", 1)[1].strip()
                    try:
                        since_time = datetime.datetime.strptime(
                            since_str, "%a, %d %b %Y %H:%M:%S GMT"
                        ).replace(tzinfo=datetime.timezone.utc)

                        # Compare timestamps
                        if file_time.replace(microsecond=0) <= since_time:
                            response = "HTTP/1.1 304 Not Modified\r\n\r\n"
                            conn.sendall(response.encode())
                            print("→ Sent response: HTTP/1.1 304 Not Modified")
                            conn.close()
                            break
                    except Exception:
                        pass
            else:
                pass
            # If connection closed above (304), skip sending file
            if conn.fileno() == -1:
                continue

        # --- 200: OK (serve file) ---
        with open(filename, "r") as f:
            content = f.read()

        last_modified = file_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        headers = (
            "HTTP/1.1 200 OK\r\n"
            f"Last-Modified: {last_modified}\r\n"
            "Content-Type: text/html\r\n\r\n"
        )
        response = headers + content
        conn.sendall(response.encode())
        print("→ Sent response: HTTP/1.1 200 OK")
        conn.close()

except KeyboardInterrupt:
    print("\nServer shutting down...")
    server_socket.close()
