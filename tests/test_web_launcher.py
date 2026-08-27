"""M26 统一 Web 启动命令的无进程单测。"""
from __future__ import annotations

import socket
import unittest
from unittest import mock

from papermine import cli, web_launcher


class WebLauncherTest(unittest.TestCase):
    def test_commands_preserve_frozen_ports(self):
        self.assertEqual(
            web_launcher._frontend_command("npm", False, "127.0.0.1", 3100),
            ["npm", "run", "start", "--", "--hostname", "127.0.0.1", "--port", "3100"],
        )
        self.assertEqual(web_launcher._frontend_command("npm", True, "127.0.0.1", 3100)[-1], "--dev")
        backend = web_launcher._backend_command("127.0.0.1", 8100)
        self.assertIn("web.app:create_app", backend)
        self.assertEqual(backend[-1], "8100")

    def test_cli_dispatches_web_options(self):
        with mock.patch.object(web_launcher, "run_web", return_value=0) as run:
            code = cli.main([
                "web", "--host", "127.0.0.1", "--api-port", "8100",
                "--web-port", "3100", "--no-browser", "--dev",
            ])
        self.assertEqual(code, 0)
        run.assert_called_once_with(
            host="127.0.0.1", api_port=8100, web_port=3100,
            open_browser=False, dev=True,
        )

    def test_cli_rejects_same_port(self):
        self.assertEqual(cli.main(["web", "--api-port", "3000", "--web-port", "3000"]), 2)

    def test_port_guard_rejects_occupied_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with self.assertRaisesRegex(RuntimeError, "后端端口 .* 已被占用"):
                web_launcher._ensure_port_available("127.0.0.1", port, "后端")
        finally:
            listener.close()

    def test_wait_ready_rejects_process_that_exits_after_old_service_response(self):
        process = mock.Mock()
        process.poll.side_effect = [None, 1]
        process.returncode = 1
        response = mock.Mock(status_code=200)
        with mock.patch.object(web_launcher.httpx, "get", return_value=response), \
                mock.patch.object(web_launcher.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "服务启动失败"):
                web_launcher._wait_ready("http://127.0.0.1:8000/health", process)


if __name__ == "__main__":
    unittest.main()
