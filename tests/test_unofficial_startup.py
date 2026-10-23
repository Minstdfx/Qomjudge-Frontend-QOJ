"""Offline checks for the scoreboard's server-wide unofficial entry option."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import app
from qoj import QOJClient


class UnofficialStartupTests(unittest.TestCase):
    def test_cli_passes_each_selection_to_the_served_application(self):
        for flags, expected in (([], None), (["--show-unofficial"], True),
                                (["--no-show-unofficial"], False)):
            with self.subTest(flags=flags), TemporaryDirectory() as directory:
                configured = Mock(config={})
                argv = ["app.py", "--contest", "1802", "--data-dir", directory, *flags]
                with patch("sys.argv", argv), \
                        patch("app.create_app", return_value=configured) as factory, \
                        patch("waitress.serve") as serve, redirect_stdout(StringIO()):
                    app.main()
                factory.assert_called_once_with(
                    1802, False, None, directory, mode="vp", scoreboard="auto",
                    show_unofficial=expected,
                )
                serve.assert_called_once()
                self.assertIs(serve.call_args.args[0], configured)

    def test_new_browser_clients_inherit_the_server_selection(self):
        for selection in (None, True, False):
            with self.subTest(selection=selection), TemporaryDirectory() as directory:
                with patch("requests.sessions.Session.request", side_effect=AssertionError("Offline test")):
                    server = app.create_app(contest_id=1802, data_dir=Path(directory),
                                            show_unofficial=selection)
                    server.config["TESTING"] = True
                    # Separate browser sessions must receive the same startup choice.
                    for _ in range(2):
                        self.assertEqual(server.test_client().get("/healthz").status_code, 200)
                clients = server.extensions["clients"]
                self.assertEqual(len(clients), 2)
                for runtime in clients.values():
                    client = runtime["client"]
                    self.assertIsInstance(client, QOJClient)
                    self.assertIs(client.show_unofficial, selection)
                    client.session.close()

    def test_factory_rejects_non_boolean_selections_before_creating_storage(self):
        with TemporaryDirectory() as directory:
            data_dir = Path(directory) / "unused"
            for invalid in (0, 1, "true", "false", [], {}):
                with self.subTest(selection=invalid):
                    with self.assertRaisesRegex(ValueError, "show_unofficial"):
                        app.create_app(data_dir=data_dir, show_unofficial=invalid)
                    self.assertFalse(data_dir.exists())


if __name__ == "__main__":
    unittest.main()
