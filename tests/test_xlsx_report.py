import dataclasses
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
from dpf.coverage import ONLY_IN_BACKUP
from dpf.xlsx_report import SHEETS, PoolSheets, sheet_name, write_combined_xlsx, write_xlsx

from tests.test_grouping import rec
from tests.test_report import SUMMARY, build, build_categories, build_coverage, build_trees

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


@unittest.skipUnless(HAVE_XLSXWRITER, "XlsxWriter is not installed")
class CategoryXlsxTest(unittest.TestCase):
    def test_time_shift_and_category_columns(self):
        result, groups, summary, _ = build_categories()
        summary = {**summary, "root_status": [{
            "root": "/d", "status": "scanned now", "files": 12, "bytes": 26082,
            "personal_files": 8, "personal_bytes": 6076, "other_files": 4, "other_bytes": 20006,
        }]}  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.xlsx")
            write_xlsx(result, groups, summary, path)
            wb = Workbook(path)
            self.addCleanup(wb.close)
            pairs = wb.cells("Folder pairs")
            self.assertEqual((pairs["S1"], pairs["T1"]), ("Time shift", "Category"))
            rows = {pairs[f"F{r}"]: r for r in range(2, 5)}
            self.assertEqual(pairs[f"S{rows['/d/one/Trip']}"], "+1 h")
            self.assertEqual(pairs[f"T{rows['/d/one/Plex']}"], "other")
            self.assertEqual(pairs[f"T{rows['/d/one/Photos']}"], "personal")
            seq = wb.cells("Sequences")
            self.assertEqual((seq["L1"], seq["M1"]), ("Time shift", "Category"))
            self.assertIn("+1 h", seq.values())
            files = wb.cells("Files")
            self.assertEqual((files["I1"], files["J1"]), ("Hours after first", "Category"))
            self.assertIn(1.0, [v for k, v in files.items() if k[0] == "I"])
            self.assertEqual(wb.cells("Trees")["N1"], "Time shift")
            values = list(wb.cells("Summary").values())
            for text in ("Categories", "Personal files", "Other size", "/d",
                         "1 group relies on a whole-hour time shift (time zone or DST copies)"):
                self.assertIn(text, values)  # fmt: skip

    def test_no_category_values_without_categories(self):
        result, groups = build()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.xlsx")
            write_xlsx(result, groups, SUMMARY, path)
            wb = Workbook(path)
            self.addCleanup(wb.close)
            pairs = wb.cells("Folder pairs")
            self.assertEqual(pairs["T1"], "Category")
            self.assertNotIn("T2", pairs)
            self.assertNotIn("S2", pairs)


class SheetNameTest(unittest.TestCase):
    def test_valid_and_unique(self):
        used = {"summary"}
        self.assertEqual(sheet_name("A Trees", used), "A Trees")
        self.assertEqual(sheet_name("a trees", used), "a trees (2)")
        self.assertEqual(
            sheet_name("Pool [x]: a/b?*\\'q' Files", used), "Pool _x__ a_b____q_ Files"
        )
        long = sheet_name("Photographs archive Folder pairs", used)
        self.assertEqual((long, len(long)), ("Photographs archive Folder pair", 31))
        again = sheet_name("Photographs archive Folder pairs", used)
        self.assertEqual((again, len(again)), ("Photographs archive Folder (2)", 30))
        self.assertEqual(sheet_name("Summary", used), "Summary (2)")


@unittest.skipUnless(HAVE_XLSXWRITER, "XlsxWriter is not installed")
class CoverageXlsxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "report.xlsx")

    def open(self):
        wb = Workbook(self.path)
        self.addCleanup(wb.close)
        return wb

    def test_single_pool_with_coverage(self):
        result, groups = build()
        cov = build_coverage()
        summary = {**SUMMARY, "pool": "A", "missing_csv_path": "/tmp/missing.csv"}
        write_xlsx(result, groups, summary, self.path, [cov])
        wb = self.open()
        self.assertEqual(wb.names, [*SHEETS, "A not in B"])
        cells = wb.cells("A not in B")
        self.assertEqual(
            [cells[f"{c}1"] for c in "ABCDEFGHIJKL"],
            ["#", "Missing size", "Folder", "Whole tree", "Folders", "Missing files",
             "Total files", "Missing %", "Similar name there", "Other files not compared",
             "Runs", "First missing run"],
        )  # fmt: skip
        self.assertEqual(
            [cells[f"{c}2"] for c in "BCDEFGH"], [7_000_000, "/a/Trip", "yes", 2, 5, 5, 1.0]
        )
        self.assertEqual([cells[f"{c}3"] for c in "CEFGHIK"], ["/a/DCIM", 1, 7, 10, 0.7, 1, 2])
        self.assertEqual(cells["L3"], "DSC00003.ARW … DSC00005.ARW  3 files  (+1 more runs)")
        self.assertNotIn("D3", cells)
        self.assertIn("file:///a/Trip", wb.links("A not in B"))
        summary_cells = wb.cells("Summary")
        values = list(summary_cells.values())
        self.assertIn("Backup coverage", values)
        self.assertIn(f"{cov.title}: pool B is the backup of pool A.", values)
        self.assertIn("Missing files CSV", values)
        self.assertIn("/tmp/missing.csv", values)
        row = next(k[1:] for k, v in summary_cells.items() if (k[0], v) == ("A", "A not in B"))
        self.assertEqual(
            [summary_cells[f"{c}{row}"] for c in "BCDEFGH"], [12, 7_000_700, 0.8, 3, 15, 1, 1]
        )

    def test_tiny_share_is_not_zero(self):
        cov = build_coverage()
        cov.folders[1] = dataclasses.replace(cov.folders[1], missing=1, total=1_000)
        write_xlsx(GroupingResult([], [], 0, 0, 0), [], SUMMARY, self.path, [cov])
        self.assertEqual(self.open().cells("A not in B")["H3"], 0.001)  # shown as "<1%"

    def test_combined_workbook(self):
        result_a, groups_a = build()
        result_b = build_trees()
        a_cov, b_cov = build_coverage(), build_coverage(ONLY_IN_BACKUP, "B", "A")
        pools = [
            PoolSheets("A", result_a, groups_a, {**SUMMARY, "pool": "A"}),
            PoolSheets("B", result_b, [], {**SUMMARY, "pool": "B", "roots": ["/d1", "/d2"]}),
        ]
        summary = {"mode": "1", "basis": "x", "pools": ["A", "B"], "elapsed": 1.0}
        write_combined_xlsx(pools, [a_cov, b_cov], summary, self.path)
        wb = self.open()
        self.assertEqual(wb.names, [
            "Summary",
            "A Trees", "A Folder pairs", "A Sequences", "A Same folder", "A Files",
            "B Trees", "B Folder pairs", "B Sequences", "B Same folder", "B Files",
            "A not in B", "B not in A",
        ])  # fmt: skip
        self.assertEqual(wb.cells("A Folder pairs")["F2"], "/drives/one/DCIM/100CANON")
        self.assertEqual(wb.cells("B Trees")["D2"], "/d1/Photos")
        self.assertEqual(wb.cells("B not in A")["C2"], "/a/Trip")
        values = list(wb.cells("Summary").values())
        for text in ("Pool A", "Pool B", "Pool A sheets", "Coverage sheets", "B Files",
                     "B not in A", f"{a_cov.title}: pool B is the backup of pool A."):  # fmt: skip
            self.assertIn(text, values)
        self.assertEqual(values.count("Roots"), 2)
        n = wb.names.index("Summary") + 1
        xml = wb.zip.read(f"xl/worksheets/sheet{n}.xml").decode()
        for name in wb.names[1:]:
            self.assertIn(f"location=\"'{name}'!A1\"", xml)


if __name__ == "__main__":
    unittest.main()
