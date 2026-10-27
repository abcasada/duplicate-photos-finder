import os
import shutil
import tempfile
import unittest

from dpf.pools import (
    Pool,
    PoolError,
    get_pool,
    load_pools,
    matches,
    parse_pools,
    pool_paths,
    pools_of,
)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "pools.toml")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_load(self):
        self.write(
            "# comment\n[pools]\n"
            'A = ["/m/Storage 1TB", "/m/Storage [0-9]A */"]\n'
            'Backup = "/m/Storage [0-9]B *"\n'
        )
        pools = load_pools(self.path)
        self.assertEqual(list(pools), ["a", "backup"])
        self.assertEqual(pools["a"], Pool("A", ("/m/Storage 1TB", "/m/Storage [0-9]A *")))
        self.assertEqual(pools["backup"].patterns, ("/m/Storage [0-9]B *",))
        self.assertIs(get_pool(pools, "a"), pools["a"])
        self.assertIs(get_pool(pools, "BACKUP"), pools["backup"])

    def test_expands_home(self):
        pools = parse_pools({"pools": {"P": ["~/Pictures"]}})
        self.assertEqual(pools["p"].patterns, (os.path.expanduser("~/Pictures"),))

    def test_missing_file(self):
        with self.assertRaises(PoolError) as cm:
            load_pools(self.path)
        msg = str(cm.exception)
        self.assertIn(f"no pools file at {self.path}", msg)
        self.assertIn("[pools]\nA = [", msg)

    def test_bad_toml(self):
        self.write("[pools]\nA = [unquoted]\n")
        with self.assertRaises(PoolError) as cm:
            load_pools(self.path)
        self.assertIn(f"cannot read pools file {self.path}", str(cm.exception))
        self.assertIn("[pools]\nA = [", str(cm.exception))

    def test_invalid_definitions(self):
        cases = [
            ({"other": 1}, "cfg defines no [pools] table"),
            ({"pools": {}}, "cfg defines no [pools] table"),
            ({"pools": {"A": []}}, "cfg: pool A has no paths"),
            ({"pools": {"A": [1]}}, "cfg: pool A must be a list"),
            ({"pools": {"A": {"x": "/y"}}}, "cfg: pool A must be a list"),
            ({"pools": {"A": ["relative/*"]}}, "cfg: pool A: relative/* is not an absolute path"),
            ({"pools": {"A": ["/a"], "a": ["/b"]}}, "cfg: pools A and a differ only in case"),
        ]
        for data, expected in cases:
            with self.subTest(expected), self.assertRaises(PoolError) as cm:
                parse_pools(data, "cfg")
            self.assertIn(expected, str(cm.exception))

    def test_unknown_pool(self):
        pools = parse_pools({"pools": {"A": ["/a"], "B": ["/b"]}})
        with self.assertRaisesRegex(PoolError, "unknown pool C; defined pools: A, B"):
            get_pool(pools, "C")


class MatchTest(unittest.TestCase):
    def test_matches_path_or_ancestor(self):
        pattern = "/m/Storage [0-9]A *"
        self.assertTrue(matches("/m/Storage 2A 8TB", pattern))
        self.assertTrue(matches("/m/Storage 2A 8TB/Photos/2019", pattern))
        self.assertTrue(matches("/m/Storage 2A 8TB/", pattern))
        self.assertFalse(matches("/m/Storage 2B 8TB", pattern))
        self.assertFalse(matches("/m", pattern))
        self.assertFalse(matches("/m/storage 2A 8TB", pattern))  # case-sensitive paths
        self.assertTrue(matches("/m/Storage 1TB/x", "/m/Storage 1TB"))
        self.assertFalse(matches("/m/Storage 1TB-old", "/m/Storage 1TB"))

    def test_wildcards_stay_within_one_segment(self):
        pattern = "/run/media/*/Storage [0-9]A *"
        self.assertTrue(matches("/run/media/adam/Storage 2A 8TB", pattern))
        self.assertTrue(matches("/run/media/adam/Storage 2A 8TB/old/x", pattern))
        self.assertFalse(matches("/run/media/adam/Storage 2B 8TB/old/Storage 1A x", pattern))
        self.assertFalse(matches("/run/media/a/b/Storage 1A x", pattern))
        self.assertFalse(matches("/m/x/y", "/m/*y"))
        self.assertTrue(matches("/m/x//y/", "/m/x/?"))
        self.assertTrue(matches("/anything", "/"))
        self.assertFalse(matches("/run/media", pattern))

    def test_pools_of(self):
        pools = parse_pools({"pools": {
            "A": ["/m/Storage 1TB", "/m/Storage [0-9]A *"],
            "B": ["/m/Storage [0-9]B *"],
            "All": ["/m/*"],
        }})  # fmt: skip
        self.assertEqual(pools_of(pools, "/m/Storage 1TB"), ["A", "All"])
        self.assertEqual(pools_of(pools, "/m/Storage 1B 4TB (2)"), ["B", "All"])
        self.assertEqual(pools_of(pools, "/home/me/Pictures"), [])
        self.assertEqual(pools_of(pools, "/m/Storage 2B 8TB/old/Storage 1A x"), ["B", "All"])


class PoolPathsTest(unittest.TestCase):
    """Patterns match mounted directories (glob) and indexed roots (fnmatch)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.m = os.path.join(self.tmp, "media")
        for name in ("Storage 1A 4TB", "Storage 2A 8TB", "Storage 1B 4TB (2)", "Other"):
            os.makedirs(os.path.join(self.m, name))
        open(os.path.join(self.m, "Storage 9A file"), "w").close()

    def pool(self, *patterns):
        return Pool("A", tuple(f"{self.m}/{p}" for p in patterns))

    def test_online_and_indexed(self):
        indexed = [
            f"{self.m}/Storage 2A 8TB",  # connected and indexed
            f"{self.m}/Storage 3A 8TB",  # unplugged
            f"{self.m}/Storage 4A 8TB/Photos",  # unplugged, scanned as a subfolder
            f"{self.m}/Storage 3B 8TB",
        ]
        paths = pool_paths(self.pool("Storage [0-9]A *", "Storage 1TB"), indexed)
        self.assertEqual(paths, [
            f"{self.m}/Storage 1A 4TB",
            f"{self.m}/Storage 1TB",  # literal: kept though neither connected nor indexed
            f"{self.m}/Storage 2A 8TB",
            f"{self.m}/Storage 3A 8TB",
            f"{self.m}/Storage 4A 8TB/Photos",
        ])  # fmt: skip

    def test_indexed_subfolder_of_other_drive_not_matched(self):
        indexed = [f"{self.m}/Storage 2B 8TB/old/Storage 1A x", f"{self.m}/Storage 3A 8TB"]
        paths = pool_paths(self.pool("Storage [0-9]A *"), indexed)
        self.assertEqual(paths, [
            f"{self.m}/Storage 1A 4TB", f"{self.m}/Storage 2A 8TB", f"{self.m}/Storage 3A 8TB",
        ])  # fmt: skip

    def test_brackets_in_names(self):
        paths = pool_paths(self.pool("Storage [0-9]B *"), [])
        self.assertEqual(paths, [f"{self.m}/Storage 1B 4TB (2)"])

    def test_no_match(self):
        with self.assertRaises(PoolError) as cm:
            pool_paths(self.pool("Nothing *", "Storage [5-9]A *"), [f"{self.m}/Storage 1B x"])
        msg = str(cm.exception)
        self.assertIn("pool A matches no connected or indexed drive", msg)
        self.assertIn(f"\n  {self.m}/Nothing *\n  {self.m}/Storage [5-9]A *", msg)


if __name__ == "__main__":
    unittest.main()
