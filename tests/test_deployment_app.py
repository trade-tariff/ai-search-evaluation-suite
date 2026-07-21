import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from deployment.app import create_server


class DeploymentAppTest(unittest.TestCase):
    def test_root_returns_okay_as_plain_text(self):
        server = create_server(("127.0.0.1", 0), tls=False)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "text/plain")
                self.assertEqual(response.read(), b"OKAY\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_healthcheck_returns_okay(self):
        server = create_server(("127.0.0.1", 0), tls=False)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/healthcheckz") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"OKAY\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_tls_uses_certificate_and_key_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "key.pem"
            certificate_path = Path(directory) / "certificate.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key_path),
                    "-out",
                    str(certificate_path),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                ],
                check=True,
                capture_output=True,
            )

            with patch.dict(
                os.environ,
                {
                    "SSL_CERT_PEM": certificate_path.read_text(),
                    "SSL_KEY_PEM": key_path.read_text(),
                },
            ):
                server = create_server(("127.0.0.1", 0), tls=True)

            thread = threading.Thread(target=server.serve_forever)
            thread.start()

            try:
                context = ssl._create_unverified_context()
                with urlopen(
                    f"https://127.0.0.1:{server.server_port}/",
                    context=context,
                ) as response:
                    self.assertEqual(response.read(), b"OKAY\n")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
