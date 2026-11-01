import os
import shutil
import tempfile
import unittest

from dpf.backed_dupes import BOTH, find_backed_dupes, format_backed
from dpf.index import Index
from dpf.match_name import find_name_duplicates
from dpf.models import FileRecord

T = 1_600_000_000
A1, A2, B1 = "/d/Storage 1A", "/d/Storage 2A", "/d/Storage 1B"


def files(dir, names, size=100):
    return [FileRecord(dir, n, size, T, "image") for n in names]


class BackedDupesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.idx = Index(os.path.join(tmp, "index.db"))
        self.addCleanup(self.idx.close)
        seq = [f"IMG_{i}.JPG" for i in range(1, 6)]
        # IMG_1..5 twice in A and twice in B; IMG_1..2 also once more in A2/x.
        self.idx.replace_root(A1, files(A1 + "/p", seq + ["solo.JPG"]) + files(A1 + "/q", seq))
        self.idx.replace_root(A2, files(A2 + "/x", seq[:2]) + files(A2 + "/y", ["only_a.JPG"])
                              + files(A2 + "/z", ["one.JPG", "one (1).JPG"]))  # fmt: skip
        self.idx.replace_root(B1, files(B1 + "/p", seq + ["solo.JPG", "one.JPG"])
                              + files(B1 + "/r", seq) + files(B1 + "/s", ["only_a.JPG"]))  # fmt: skip

    def find(self):
        groups = list(find_name_duplicates(self.idx, [A1, A2, B1]))

        def pool_of(d):
            return "B" if d.startswith(B1) else "A"

        return find_backed_dupes(self.idx, groups, pool_of, "A", "B", "name")

    def test_rows_split_by_folder_set_and_sorted_by_saving(self):
        found = self.find()
        rows = [(r.first, r.last, r.files, r.copies_a, r.copies_b, r.saved, r.duplicated_in)
                for r in found.runs]  # fmt: skip
        self.assertEqual(rows, [
            ("IMG_1.JPG", "IMG_2.JPG", 2, 3, 2, 600, BOTH),  # 2 x 100 x (2 + 1)
            ("IMG_3.JPG", "IMG_5.JPG", 3, 2, 2, 600, BOTH),  # 3 files x 100 B x (1 + 1)
            ("one.JPG", "one.JPG", 1, 2, 1, 100, "A"),  # a copy mark in A, once in B
        ])  # fmt: skip
        first = found.runs[0]
        self.assertEqual([(s.pool, s.dir) for s in first.spots], [
            ("A", A1 + "/p"), ("A", A1 + "/q"), ("A", A2 + "/x"),
            ("B", B1 + "/p"), ("B", B1 + "/r"),
        ])  # fmt: skip
        # Backed up once per pool (only_a, solo) or not backed up at all: no row.
        self.assertFalse(any(r.first in ("only_a.JPG", "solo.JPG") for r in found.runs))
        self.assertEqual(found.totals(), (3, 6, 1300))
        self.assertEqual(found.totals("B"), (0, 0, 0))

    def test_format(self):
        text = format_backed(self.find(), top=2)
        self.assertIn("Extra copies in both:", text)
        self.assertIn("IMG_3.JPG … IMG_5.JPG", text)
        self.assertIn(f"B: {B1}/r", text)
        self.assertIn("… and 1 more sequences", text)


if __name__ == "__main__":
    unittest.main()
