import os
import tempfile
import unittest

from dpf.categories import (
    OTHER,
    PERSONAL,
    Categories,
    load_categories,
    of_dirs,
    parse_categories,
    tally,
)
from dpf.index import Index
from dpf.pools import PoolError
from tests.test_grouping import rec

M = "/run/media/u"


class CategoriesTest(unittest.TestCase):
    def test_patterns(self):
        cats = Categories(["*/Plex*", "_Torrents", f"{M}/Storage 2A/Films", "/x/[0-9]*"])
        cases = [
            (f"{M}/Storage 1A/Photos/2020", PERSONAL),
            (f"{M}/Storage 1A/Plex Media", OTHER),  # relative: at any depth
            (f"{M}/Storage 1A/Plex Media/Movies/A (2001)", OTHER),  # ancestors count
            (f"{M}/Storage 1A/plex", OTHER),  # case is ignored, as on NTFS
            (f"{M}/Storage 1A/Photos/Plexiglass", OTHER),
            (f"{M}/Storage 1A/Complex", PERSONAL),  # per segment, not substring
            ("/Plex", PERSONAL),  # "*/Plex*" needs a parent folder
            (f"{M}/Storage 1A/_Torrents/x", OTHER),
            (f"{M}/Storage 1A/_torrents2", PERSONAL),
            (f"{M}/Storage 2A/Films", OTHER),  # absolute: anchored
            (f"{M}/Storage 2A/Films/Kids", OTHER),
            (f"{M}/Storage 3A/Films", PERSONAL),
            (f"{M}/Storage 2A/Old/Films", PERSONAL),
            ("/x/2020", OTHER),
            ("/x/y/2020", PERSONAL),
            ("/", PERSONAL),
        ]
        for d, expected in cases:
            with self.subTest(d):
                self.assertEqual(cats.of(d), expected)
                self.assertEqual(cats(d + "/"), expected)

    def test_empty(self):
        cats = Categories()
        self.assertFalse(cats)
        self.assertEqual(cats.of("/a/Plex"), PERSONAL)
        self.assertTrue(Categories(["x"]))

    def test_of_dirs(self):
        cats = Categories(["Plex"])
        self.assertEqual(of_dirs(cats, ["/a/Plex", "/b/Plex/x"]), OTHER)
        self.assertEqual(of_dirs(cats, ["/a/Plex", "/b/Photos"]), PERSONAL)
        self.assertEqual(of_dirs(None, ["/a/Plex"]), PERSONAL)

    def test_parse(self):
        self.assertFalse(parse_categories({}))
        self.assertFalse(parse_categories({"categories": {}}))
        self.assertEqual(parse_categories({"categories": {"other": "Plex/"}}).other, ("Plex",))
        home = os.path.expanduser("~")
        cats = parse_categories({"categories": {"other": ["~/Films", "*/Plex*"]}})
        self.assertEqual(cats.other, (f"{home}/Films", "*/Plex*"))
        errors = [
            ({"categories": ["x"]}, "must be a table"),
            ({"categories": {"personal": ["x"]}}, "unknown key personal"),
            ({"categories": {"other": [1]}}, "must be a list"),
            ({"categories": {"other": ["/"]}}, "must be a list"),
            ({"categories": {"other": {"a": 1}}}, "must be a list"),
        ]
        for data, message in errors:
            with self.subTest(data), self.assertRaises(PoolError) as cm:
                parse_categories(data, "cfg")
            self.assertIn(message, str(cm.exception))

    def test_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pools.toml")
            self.assertFalse(load_categories(path))
            with open(path, "w") as fh:
                fh.write('[pools]\nA = ["/a"]\n[categories]\nother = ["*/Plex*"]\n')
            self.assertEqual(load_categories(path).other, ("*/Plex*",))
            with open(path, "w") as fh:
                fh.write("[categories\n")
            with self.assertRaises(PoolError):
                load_categories(path)

    def test_tally(self):
        idx = Index(":memory:")
        self.addCleanup(idx.close)
        idx.replace_root("/d", [
            rec("/d/Photos", "a.jpg", 10), rec("/d/Photos/x", "b.jpg", 20),
            rec("/d/Plex", "m.mkv", 1000), rec("/d/Plex/s", "n.mkv", 500),
            rec("/d/Photos", "e.jpg", 0),
        ])  # fmt: skip
        got = tally(idx, ["/d"], Categories(["Plex"]))
        self.assertEqual(got, {PERSONAL: [3, 30], OTHER: [2, 1500]})
        self.assertEqual(
            tally(idx, ["/d/Plex"], Categories()), {PERSONAL: [2, 1500], OTHER: [0, 0]}
        )


if __name__ == "__main__":
    unittest.main()
