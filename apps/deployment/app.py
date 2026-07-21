import os
import ssl
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/healthcheckz"}:
            self.send_error(404)
            return

        body = b"OKAY\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        super().log_message(format, *args)


def create_server(address: tuple[str, int], *, tls: bool) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(address, RequestHandler)
    if not tls:
        return server

    certificate = os.environ.get("SSL_CERT_PEM")
    private_key = os.environ.get("SSL_KEY_PEM")
    if not certificate or not private_key:
        server.server_close()
        raise RuntimeError("SSL_CERT_PEM and SSL_KEY_PEM are required")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as directory:
        certificate_path = os.path.join(directory, "certificate.pem")
        private_key_path = os.path.join(directory, "private-key.pem")
        for path, content in (
            (certificate_path, certificate),
            (private_key_path, private_key),
        ):
            with open(path, "w", encoding="utf-8") as file:
                file.write(content)
            os.chmod(path, 0o600)
        context.load_cert_chain(certificate_path, private_key_path)

    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> None:
    port = int(os.environ.get("SSL_PORT", "8443"))
    server = create_server(("0.0.0.0", port), tls=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
