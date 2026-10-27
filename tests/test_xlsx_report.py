import importlib.util
import os
import re
import tempfile
import unittest
import zipfile
from unittest import mock
from xml.etree import ElementTree

from dpf.grouping import A_IN_B, NEAR_IDENTICAL, PARTIAL, FolderPair, GroupingResult, Run
from dpf.models import DuplicateGroup
from dpf.report import OFFLINE_NOTE
from dpf.xlsx_report import SHEETS, write_xlsx

from tests.test_grouping import rec
from tests.test_report import SUMMARY, build, build_trees

HAVE_XLSXWRITER = importlib.util.find_spec("xlsxwriter") is not None
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def pair(dir_a, dir_b, matched_a, total_a, matched_b, total_b, relation=PARTIAL, unindexed=0):
    run = Run("IMG_1.JPG", "IMG_9.JPG", "IMG_1.JPG", "IMG_9.JPG", matched_a, matched_a * 10, True)
    return FolderPair(
        dir_a, dir_b, (run,), matched_a, total_a, matched_b, total_b, matched_a * 10,
        relation, unindexed=unindexed,
    )  # fmt: skip


def result_of(*pairs):
    return GroupingResult(list(pairs), [], len(pairs), 0, 0)


class Workbook:
    """Read back an .xlsx: sheet names, shared strings, cell values and hyperlink targets."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        wb = ElementTree.fromstring(self.zip.read("xl/workbook.xml"))
        self.names = [s.get("name") for s in wb.iterfind("m:sheets/m:sheet", NS)]
        try:
            sst = ElementTree.fromstring(self.zip.read("xl/sharedStrings.xml"))
            self.strings = ["".join(t.itertext()) for t in sst.iterfind("m:si", NS)]
        except KeyError:
            self.strings = []

    def close(self):
        self.zip.close()

    def cells(self, sheet):
        """Cell reference -> value (str for strings, float for numbers)."""
        n = self.names.index(sheet) + 1
        root = ElementTree.fromstring(self.zip.read(f"xl/worksheets/sheet{n}.xml"))
        out = {}
        for c in root.iterfind(".//m:c", NS):
            v = c.find("m:v", NS)
            if v is None:
                continue
            out[c.get("r")] = self.strings[int(v.text)] if c.get("t") == "s" else float(v.text)
        return out

    def links(self, sheet):
        n = self.names.index(sheet) + 1
        try:
            rels = self.zip.read(f"xl/worksheets/_rels/sheet{n}.xml.rels").decode()
        except KeyError:
            return []
        return re.findall(r'Target="(file://[^"]*)"', rels)


@unittest.skipUnless(HAVE_XLSXWRITER, "XlsxWriter is not installed")
class XlsxReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, result, groups=(), summary=SUMMARY, **kw):
        path = os.path.join(self.tmp.name, "report.xlsx")
        write_xlsx(result, list(groups), summary, path, **kw)
        wb = Workbook(path)
        self.addCleanup(wb.close)
        return wb

    def test_sheets_and_content(self):
        result, groups = build()
        wb = self.write(result, groups, {**SUMMARY, "csv_path": "/tmp/r.csv"})
        self.assertEqual(wb.names, list(SHEETS))
        self.assertIn("/drives/one/DCIM/100CANON", wb.strings)
        self.assertIn("/drives/one/Phone", wb.strings)
        self.assertIn("/tmp/r.csv", wb.strings)
        cells = wb.cells("Folder pairs")
        self.assertEqual(cells["A1"], "#")
        self.assertEqual(cells["D2"], "partial overlap")
        self.assertEqual((cells["E1"], cells["E2"]), ("Names", "exact"))
        self.assertEqual(cells["F2"], "/drives/one/DCIM/100CANON")
        self.assertEqual(cells["C2"], 225_000_000)  # bytes, so sorting works
        self.assertEqual((cells["H2"], cells["I2"], cells["J2"]), (45, 60, 0.75))
        self.assertEqual(
            cells["R2"],
            "IMG_0100.JPG … IMG_0139.JPG  40 files  (+1 more on Sequences sheet)",
        )
        renamed = next(r for r in range(2, 5) if "Trip" in str(cells.get(f"R{r}")))
        self.assertEqual(
            cells[f"R{renamed}"],
            "E1.jpg … E3.jpg  3 files  → in B: Trip 001.jpg … Trip 003.jpg",
        )
        self.assertEqual(cells[f"E{renamed}"], "similar")
        seq = wb.cells("Sequences")
        self.assertEqual((seq["D2"], seq["E2"], seq["H2"]), ("IMG_0100.JPG", "IMG_0139.JPG", 40))
        self.assertNotIn("F2", seq)  # B names only when they differ
        self.assertNotIn("J2", seq)  # nothing passed over inside the run
        self.assertIn("Trip 001.jpg", seq.values())
        files = wb.cells("Files")
        rows = sum(len(g.files) for g in groups)
        self.assertIn(f"G{rows + 1}", files)
        self.assertNotIn(f"G{rows + 2}", files)
        self.assertIsInstance(files["F2"], float)  # an Excel date, not text
        self.assertEqual(wb.links("Files"), [])
        same = wb.cells("Same folder")
        self.assertEqual((same["A2"], same["B2"], same["D2"]), ("/drives/one/Phone", 1, 1_000))

    def test_summary_root_status(self):
        status = [
            {"root": "/drives/one", "status": "scanned now", "connected": True,
             "scanned_at": 1.0, "files": 1, "bytes": 2},
            {"root": "/drives/two", "status": "from index", "connected": False,
             "scanned_at": None, "files": 3, "bytes": 4},
        ]  # fmt: skip
        wb = self.write(build()[0], summary={**SUMMARY, "root_status": status})
        cells = wb.cells("Summary")
        roots = [k for k, v in cells.items() if v == "Roots"]
        self.assertEqual(len(roots), 1)
        row = int(roots[0][1:])
        self.assertEqual(cells[f"B{row}"], "/drives/one   (scanned now)")
        self.assertEqual(
            cells[f"B{row + 1}"], "/drives/two   (from index, date unknown, not connected)"
        )
        self.assertEqual(cells[f"B{row + 2}"], OFFLINE_NOTE)
        wb = self.write(build()[0])
        self.assertNotIn(OFFLINE_NOTE, wb.strings)
        self.assertIn("/drives/two", wb.strings)
        self.assertNotIn("Pool", wb.strings)

    def test_summary_pool(self):
        cells = self.write(build()[0], summary={**SUMMARY, "pool": "B"}).cells("Summary")
        rows = [k for k, v in cells.items() if v == "Pool"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(cells["B" + rows[0][1:]], "B")

    def test_trees_and_tags(self):
        wb = self.write(build_trees())
        trees = wb.cells("Trees")
        self.assertEqual(trees["A2"], 1)  # shown as T1
        self.assertEqual((trees["C2"], trees["D2"]), ("near-identical trees", "/d1/Photos"))
        self.assertEqual((trees["H2"], trees["K2"]), (0.98, 1))
        self.assertEqual(trees["C3"], "B within A")
        pairs = wb.cells("Folder pairs")
        tagged = {pairs[f"F{r}"]: pairs.get(f"B{r}") for r in range(2, 7)}
        self.assertEqual(tagged["/d1/Photos/2019/Paris"], 1)
        self.assertEqual(tagged["/d1/Misc/a"], 2)

    def test_links_are_percent_encoded(self):
        a, b = "/x/Trip #1 100%", "/y/Café = copy"
        wb = self.write(result_of(pair(a, b, 5, 5, 5, 9, A_IN_B)))
        links = wb.links("Folder pairs")
        self.assertIn("file:///x/Trip%20%231%20100%25", links)
        self.assertIn("file:///y/Caf%C3%A9%20%3D%20copy", links)
        cells = wb.cells("Folder pairs")
        self.assertEqual((cells["F2"], cells["G2"]), (a, b))
        trees = self.write(build_trees())
        self.assertIn("file:///d2/Photo%20backups/2019/Paris", trees.links("Folder pairs"))
        self.assertIn("file:///d2/Photo%20backups", trees.links("Trees"))

    def test_percent_is_floored(self):
        wb = self.write(result_of(pair("/a", "/b", 3842, 3843, 3842, 3842, A_IN_B)))
        cells = wb.cells("Folder pairs")
        self.assertEqual(cells["J2"], 0.99)
        self.assertEqual(cells["M2"], 1.0)
        n = wb.names.index("Folder pairs") + 1
        self.assertNotIn(b"0.9997", wb.zip.read(f"xl/worksheets/sheet{n}.xml"))

    def test_long_path_is_plain_text(self):
        long = "/" + "/".join(["x" * 200] * 11)
        self.assertGreater(len(long), 2079)
        wb = self.write(result_of(pair(long, "/b", 1, 2, 1, 2, NEAR_IDENTICAL)))
        self.assertEqual(wb.links("Folder pairs"), ["file:///b"])
        self.assertIn(long, wb.strings)

    def test_link_limit_per_sheet(self):
        pairs = [pair(f"/a/{n}", f"/b/{n}", 1, 1, 1, 2, A_IN_B) for n in range(3)]
        with mock.patch("dpf.xlsx_report.MAX_URLS", 2):
            wb = self.write(result_of(*pairs))
        self.assertEqual(wb.links("Folder pairs"), ["file:///a/0", "file:///b/0"])
        self.assertEqual(wb.cells("Folder pairs")["F4"], "/a/2")

    def test_other_files_and_odd_strings(self):
        p = pair("=SUM(A1)", "http://example.com", 1, 1, 1, 1, A_IN_B, unindexed=3)
        groups = [DuplicateGroup((rec("/a", "x.jpg", mtime=-1e12), rec("/b", "x.jpg")), "name")]
        wb = self.write(result_of(p), groups)
        cells = wb.cells("Folder pairs")
        self.assertEqual(
            (cells["F2"], cells["G2"], cells["P2"]), ("=SUM(A1)", "http://example.com", 3)
        )
        n = wb.names.index("Folder pairs") + 1
        self.assertNotIn(b"<f>", wb.zip.read(f"xl/worksheets/sheet{n}.xml"))
        self.assertEqual(wb.cells("Files")["F2"], "-1000000000000.0")  # pre-1900 dates as text

    def test_skipped_files_and_names(self):
        runs = (
            Run("DSC00001.ARW", "DSC00005.JPG", "DSC00001.ARW", "DSC00005.JPG", 9, 90, True,
                ("DSC00003.ARW",)),
            Run("DSC00007.ARW", "DSC00009.JPG", "DSC00007.ARW", "DSC00009.JPG", 4, 40, True,
                ("DSC00008.ARW", "DSC00009.ARW")),
        )  # fmt: skip
        p = FolderPair("/a", "/b", runs, 13, 16, 13, 13, 130, A_IN_B, similar_names=True)
        groups = [
            DuplicateGroup((rec("/a", "x.jpg"), rec("/b", "x.jpg")), "name"),
            DuplicateGroup((rec("/a", "y.jpg"), rec("/b", "y (1).jpg")), "name"),
        ]
        wb = self.write(result_of(p), groups)
        pairs = wb.cells("Folder pairs")
        self.assertEqual(pairs["E2"], "similar")
        self.assertEqual(
            pairs["R2"],
            "DSC00001.ARW … DSC00005.JPG  9 files  (+1 inside not matched: DSC00003.ARW)  "
            "(+1 more on Sequences sheet; 2 inside them not matched)",
        )
        seq = wb.cells("Sequences")
        self.assertEqual((seq["J1"], seq["K1"]), ("Not matched inside", "Not matched names"))
        self.assertEqual((seq["J2"], seq["K2"]), (1, "DSC00003.ARW"))
        self.assertEqual((seq["J3"], seq["K3"]), (2, "DSC00008.ARW, DSC00009.ARW"))
        files = wb.cells("Files")
        self.assertEqual((files["H1"], files["H2"], files["H4"]), ("Names", "exact", "similar"))

    def test_empty_result(self):
        wb = self.write(GroupingResult([], [], 0, 0, 0))
        self.assertEqual(wb.names, list(SHEETS))
        self.assertEqual({k[1:] for k in wb.cells("Folder pairs")}, {"1"})


if __name__ == "__main__":
    unittest.main()
