#!/usr/bin/env python3
"""rmq-auth-shim: passwordless service identities are limited to configured source networks."""
import importlib
import os
import pathlib
import sys
import threading
import unittest
import urllib.parse
import urllib.request

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("WORLD_UNIQUE_NAME", "sh-test-world")
os.environ["DUNE_RMQ_MANAGEMENT_USER"] = "bgd.sh-test-world.duneadmin.admin"
os.environ["TEXT_ROUTER_AUTH_BASE"] = "http://127.0.0.1:9"
SERVICE = "sg.sh-test-world.ABC.game"


class ShimTest(unittest.TestCase):
    def start(self, cidrs):
        if cidrs is None:
            os.environ.pop("DUNE_RMQ_SERVICE_USER_CIDRS", None)
        else:
            os.environ["DUNE_RMQ_SERVICE_USER_CIDRS"] = cidrs
        import rmq_auth_shim
        self.shim = importlib.reload(rmq_auth_shim)
        self.server = self.shim.AuthServer(("127.0.0.1", 0), self.shim.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def ask(self, path, **form):
        url = f"http://127.0.0.1:{self.server.server_address[1]}/v0/auth/{path}"
        return urllib.request.urlopen(url, data=urllib.parse.urlencode(form).encode(), timeout=5).read().decode()

    def test_service_vhost_limited_to_configured_networks(self):
        self.start("172.31.240.0/24")
        self.assertEqual(self.ask("user", username=SERVICE, password="x"), "allow administrator")
        for ip in ("172.31.240.10", "172.31.240.1", "::ffff:172.31.240.5"):
            self.assertEqual(self.ask("vhost", username=SERVICE, vhost="/", ip=ip), "allow", ip)
        for ip in ("192.168.2.173", "8.8.8.8", "not-an-ip", ""):
            self.assertEqual(self.ask("vhost", username=SERVICE, vhost="/", ip=ip), "deny", ip)

    def test_players_and_management_user_unchanged(self):
        self.start("172.31.240.0/24")
        self.assertEqual(self.ask("vhost", username="0123456789ABCDEF", vhost="/", ip="8.8.8.8"), "allow")
        self.assertEqual(self.ask("vhost", username="bgd.sh-test-world.duneadmin.admin", vhost="/", ip="8.8.8.8"), "allow")

    def test_unset_keeps_previous_behavior(self):
        self.start(None)
        self.assertEqual(self.ask("vhost", username=SERVICE, vhost="/", ip="8.8.8.8"), "allow")


    def test_player_shadow_mode_keeps_allow_and_never_logs_password(self):
        import contextlib, io
        os.environ["DUNE_RMQ_PLAYER_AUTH_SHADOW"] = "true"
        self.addCleanup(os.environ.pop, "DUNE_RMQ_PLAYER_AUTH_SHADOW", None)
        self.start("172.31.240.0/24")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.ask("user", username="0123456789ABCDEF", password="s3cret-pw"), "allow")
            import time
            time.sleep(0.5)
        log = err.getvalue()
        self.assertIn("player-auth-shadow", log)
        self.assertIn("user=0123456789ABCDEF", log)
        self.assertNotIn("s3cret-pw", log)


if __name__ == "__main__":
    unittest.main()
