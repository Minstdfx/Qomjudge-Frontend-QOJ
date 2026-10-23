"""Startup validation does not read accounts, start servers or create bad stores."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import app


class CCPCStartupTests(unittest.TestCase):
    def test_cli_passes_explicit_ccpc_configuration(self):
        flags = ["--scoreboard-style", "ccpc", "--ccpc-unlock", "50",
                 "--ccpc-unlock-overrides", "A=10,C=0", "--ccpc-reveal-after", "240"]
        server = Mock(config={})
        with patch("sys.argv", ["app.py", "--contest", "1802", *flags]), \
                patch("app.create_app", return_value=server) as factory, \
                patch("waitress.serve") as serve, redirect_stdout(StringIO()):
            app.main()
        self.assertEqual(factory.call_args.kwargs["scoreboard_style"], "ccpc")
        self.assertEqual(factory.call_args.kwargs["ccpc_unlock"], 50)
        self.assertEqual(factory.call_args.kwargs["ccpc_unlock_overrides"], "A=10,C=0")
        self.assertEqual(factory.call_args.kwargs["ccpc_reveal_after"], 240)
        serve.assert_called_once()

    def test_invalid_cli_fails_before_creating_application(self):
        combinations = [
            ["--scoreboard-style", "ccpc"], ["--ccpc-unlock", "1"],
            ["--scoreboard-style", "ccpc", "--ccpc-unlock", "-1"],
            ["--scoreboard-style", "ccpc", "--ccpc-unlock", "1", "--ccpc-unlock-overrides", "A=2,A=3"],
            ["--scoreboard-style", "ccpc", "--ccpc-unlock", "1", "--ccpc-reveal-after", "-1"],
        ]
        for flags in combinations:
            with self.subTest(flags=flags), patch("sys.argv", ["app.py", "--contest", "9", *flags]), \
                    patch("app.create_app") as factory, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    app.main()
                self.assertEqual(raised.exception.code, 2)
                factory.assert_not_called()

    def test_invalid_factory_options_do_not_create_storage(self):
        invalid = [{"scoreboard_style": "other"}, {"scoreboard_style": "ccpc"},
                   {"ccpc_unlock": 0}, {"scoreboard_style": "ccpc", "ccpc_unlock": True},
                   {"scoreboard_style": "ccpc", "ccpc_unlock": 2, "ccpc_unlock_overrides": "A=1,A=2"}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unused"
            for options in invalid:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    app.create_app(data_dir=path, **options)
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
