import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from laoer import closes
from laoer.altprices import NY

DATA = {"2026-11-05": 150.0, "2026-11-06": 152.5, "2026-11-09": 155.0}


class CloseTest(unittest.TestCase):
    def test_seed_then_append_only_new_dates(self):
        with tempfile.TemporaryDirectory() as d:
            folder, after_close = Path(d), datetime(2026, 11, 9, 17, 0, tzinfo=NY)
            seen = []
            fetcher = lambda s, rng: (seen.append(rng), DATA)[1]
            self.assertEqual(closes.update(folder, "TECL", after_close, fetcher), 3)
            self.assertEqual(closes.update(folder, "TECL", after_close, fetcher), 0)
            self.assertEqual(seen, ["max", "1mo"])
            self.assertEqual((folder / "TECL.csv").read_text().splitlines(),
                             ["date,close", "2026-11-05,150.0", "2026-11-06,152.5", "2026-11-09,155.0"])

    def test_skips_today_before_close(self):
        midday = datetime(2026, 11, 9, 12, 0, tzinfo=NY)
        self.assertEqual(closes.new_rows(DATA, "2026-11-05", midday), ["2026-11-06,152.5"])


if __name__ == "__main__":
    unittest.main()
