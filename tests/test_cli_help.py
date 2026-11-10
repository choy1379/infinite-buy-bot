"""모든 명령의 --help 가 에러 없이 도는지 (도움말에 단독 '%' 가 있으면 Python 3.14 는 시작부터 크래시)."""

import contextlib
import io
import re
import unittest
from pathlib import Path

from laoer import __main__ as cli

CMDS = re.findall(r'add_parser\("([^"]+)"', Path(cli.__file__).read_text(encoding="utf-8"))


class HelpTest(unittest.TestCase):
    def test_every_help(self):
        self.assertIn("attrib", CMDS)
        for argv in [["--help"]] + [[c, "--help"] for c in CMDS]:
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as e:
                    cli.main(argv)
                self.assertEqual(e.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
