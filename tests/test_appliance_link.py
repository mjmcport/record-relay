import json
from pathlib import Path
import shutil
import ssl
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener

from integrations.appliance_link import serve
from providers import tls_transport


@unittest.skipUnless(shutil.which("openssl"), "openssl required for TLS rehearsal")
class ApplianceLinkTests(unittest.TestCase):
    def test_direct_tls_projects_metadata_and_limits_playback_access(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)

            def openssl(*args):
                subprocess.run(["openssl", *args], cwd=root, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-subj", "/CN=Record Relay Test CA", "-days", "1",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-keyout", "ca.key", "-out", "ca.pem")
            for name, extensions in (("server", "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n"),
                                     ("client", "extendedKeyUsage=clientAuth\n")):
                openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-subj", f"/CN={name}", "-keyout", name + ".key", "-out", name + ".csr")
                (root / "extensions").write_text("authorityKeyIdentifier=keyid,issuer\n" + extensions)
                openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.pem", "-CAkey",
                        "ca.key", "-CAcreateserial", "-days", "1", "-extfile", "extensions",
                        "-out", name + ".pem")
            seen = []

            def fetch(token):
                seen.append(token)
                return [{"player_id": "one", "name": "private label", "volume_level": 35},
                        {"player_id": "another", "volume_level": 99}]

            snapshot = root / "track.json"
            with serve("127.0.0.1", 0, root / "server.pem", root / "server.key",
                       root / "ca.pem", "one", snapshot, fetch) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"https://127.0.0.1:{server.server_port}"
                    unauthenticated = build_opener(HTTPSHandler(context=ssl.create_default_context(
                        cafile=str(root / "ca.pem")))).open
                    client = tls_transport(root / "ca.pem", root / "client.pem", root / "client.key")

                    def post(path, value, transport=client):
                        request = Request(url + path, json.dumps(value).encode(),
                                          {"Authorization": "Bearer demo"})
                        with transport(request, timeout=4) as response:
                            return json.load(response)

                    with self.assertRaises((URLError, OSError)):
                        post("/api", {"command": "players/all"}, unauthenticated)
                    with self.assertRaises(HTTPError) as error:
                        post("/api", {"command": "players/cmd/stop"})
                    error.exception.close()
                    self.assertEqual(seen, [])
                    result = post("/api", {"command": "players/all", "args": {"extra": True}})
                    self.assertEqual([item["player_id"] for item in result], ["one"])
                    self.assertNotIn("name", result[0])
                    self.assertEqual(seen, ["Bearer demo"])
                    post("/metadata", {"identify": True, "signal": True,
                                       "track": {"title": "Fixture", "secret": "ignored"}})
                    self.assertEqual(json.loads(snapshot.read_text())["track"], {"title": "Fixture"})
                finally:
                    server.shutdown()
                    thread.join()
