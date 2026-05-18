import socket
import os

import uvicorn


def find_port(start_port: int = 8080, max_tries: int = 20) -> int:
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"无法找到可用端口，已尝试 {max_tries} 个端口。")


if __name__ == "__main__":
    host = os.getenv("CONTROL_BIND_HOST", "0.0.0.0")
    start_port = int(os.getenv("CONTROL_BIND_PORT", "8080"))
    port = find_port(start_port)
    print(f"Starting on {host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
