import unittest

from dpf.names import camera_token, match_key, strip_copy_marks

# (name, expected key)
KEYS = [
    # copy suffixes and prefixes
    ("DSC00001-2.jpg", "dsc00001.jpg"),
    ("DSC01234 (1).ARW", "dsc01234.arw"),
    ("DSC01234(2).ARW", "dsc01234.arw"),
    ("IMG_0001 - Copy.JPG", "img_0001.jpg"),
    ("IMG_0001 - Copy (2).JPG", "img_0001.jpg"),
    ("IMG_0001 - Copy - Copy.JPG", "img_0001.jpg"),
    ("IMG_0001 copy.jpg", "img_0001.jpg"),
    ("IMG_0001 copy 3.jpg", "img_0001.jpg"),
    ("Copy of IMG_0001.jpg", "img_0001.jpg"),
    ("Copy (2) of Copy of IMG_0001.jpg", "img_0001.jpg"),
    ("PXL_20211231_013444284~2.jpg", "pxl_20211231_013444284.jpg"),
    ("20190316_192630~01.jpg", "20190316_192630.jpg"),
    ("DSC00001_3.jpg", "dsc00001.jpg"),
    ("IMG_0001-12 (1).jpg", "img_0001.jpg"),
    ("2023-08-26_DSC01234-2.ARW", "dsc01234.arw"),
    ("Beach_3 (1).jpg", "beach_3.jpg"),
    ("105_3661mod - Copy - Copy (2).jpg", "105_3661mod.jpg"),
    # camera file numbers inside renamed files
    ("2023-08-26_DSC01234.ARW", "dsc01234.arw"),
    ("Lincoln Air Show DSC04205.ARW", "dsc04205.arw"),
    ("DSC04205-Edit.jpg", "dsc04205.jpg"),
    ("_DSC1234.ARW", "dsc1234.arw"),
    ("Air show _DSC1234.ARW", "dsc1234.arw"),
    ("2023_DSC01234.ARW", "dsc01234.arw"),
    ("Trip DSC_0627.NEF", "dsc_0627.nef"),
    ("Trip _MG_1234.CR2", "mg_1234.cr2"),
    ("Fuji DSCF0042.RAF", "dscf0042.raf"),
    ("K1 IMGQ1234.DNG", "imgq1234.dng"),
    ("2019 IMGP0042.PEF", "imgp0042.pef"),
    ("Holiday IMG_0001.HEIC", "img_0001.heic"),
    ("Ride GX010039.MP4", "gx010039.mp4"),
    ("Ride GOPR0039.MP4", "gopr0039.mp4"),
    ("Lumix P1020970.JPG", "p1020970.jpg"),
    ("Old SAM_0696.JPG", "sam_0696.jpg"),
    ("IMG_0001 2.jpg", "img_0001.jpg"),  # macOS "Keep Both", via the camera number
    ("DSC01234 DSC01234.jpg", "dsc01234.jpg"),  # the same number twice is one token
    # left alone: camera numbers are never truncated
    ("IMG_1234.jpg", "img_1234.jpg"),
    ("IMG_1235.jpg", "img_1235.jpg"),
    ("DSC_0627.NEF", "dsc_0627.nef"),
    ("GX010039.MP4", "gx010039.mp4"),
    ("C0001.MP4", "c0001.mp4"),
    ("C0001 (1).MP4", "c0001.mp4"),
    # left alone: a number after a space numbers distinct photos
    ("Party 2.jpg", "party 2.jpg"),
    ("Party 3.jpg", "party 3.jpg"),
    # left alone: -N/_N after other text numbers distinct pictures
    ("Pic-1.bmp", "pic-1.bmp"),
    ("DSF-01.bmp", "dsf-01.bmp"),
    ("100_2529_1.JPG", "100_2529_1.jpg"),
    # left alone: dates, times and long numbers
    ("IMG_2019-04-19-00195247.jpg", "img_2019-04-19-00195247.jpg"),
    ("DSC01234-123.jpg", "dsc01234-123.jpg"),
    ("2023-08-26.jpg", "2023-08-26.jpg"),
    ("02_18_2011_23_38_37.jpg", "02_18_2011_23_38_37.jpg"),
    ("Hawaii (2019).jpg", "hawaii (2019).jpg"),
    ("IMG_20230826_143012.jpg", "img_20230826_143012.jpg"),
    # left alone: tokens inside longer runs of letters/digits, or several tokens
    ("C0017913-4C7D-4C64-8960-D9991148B110.dng", "c0017913-4c7d-4c64-8960-d9991148b110.dng"),
    ("ABCDSC012345678.jpg", "abcdsc012345678.jpg"),
    ("DSC012345.jpg", "dsc012345.jpg"),
    ("XDSC01234.jpg", "xdsc01234.jpg"),
    ("Trip C0001.MP4", "trip c0001.mp4"),
    ("DSC01234_DSC01235.jpg", "dsc01234_dsc01235.jpg"),
    ("Pano IMGQ0001-IMGQ0004.DNG", "pano imgq0001-imgq0004.dng"),
    # degenerate names keep what would otherwise vanish
    ("(1).jpg", "(1).jpg"),
    ("_1.jpg", "_1.jpg"),
    ("Copy of .jpg", "copy of .jpg"),
    (".jpg", ".jpg"),
    ("noext", "noext"),
]

# Pairs that must never share a key.
DISTINCT = [
    ("IMG_1234.jpg", "IMG_1235.jpg"),
    ("DSC01234.ARW", "DSC01234.JPG"),
    ("DSC01234 (1).ARW", "DSC01234.JPG"),
    ("Party 2.jpg", "Party 3.jpg"),
    ("2023-08-26.jpg", "2023-08-27.jpg"),
    ("DSC_0627.NEF", "DSC_0628.NEF"),
    ("C0017913-4C7D-4C64-8960-D9991148B110.dng", "C0017.dng"),
    ("DSC01234.ARW", "DSC1234.ARW"),
    ("DSC01234_DSC01235.jpg", "DSC01234.jpg"),
    ("Pic-1.bmp", "Pic-2.bmp"),
    ("GuitRig3LE-01.bmp", "GuitRig3LE-02.bmp"),
    ("IMG_2019-04-19-00195247.jpg", "IMG_2019-04-19-00214752.jpg"),
]


class MatchKeyTest(unittest.TestCase):
    def test_keys(self):
        for name, key in KEYS:
            with self.subTest(name=name):
                self.assertEqual(match_key(name), key)

    def test_distinct(self):
        for a, b in DISTINCT:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(match_key(a), match_key(b))

    def test_same_key_for_cosmetic_variants(self):
        variants = [
            "DSC01234.ARW", "dsc01234.arw", "DSC01234 (1).ARW", "DSC01234-2.ARW",
            "DSC01234 - Copy.ARW", "Copy of DSC01234.ARW", "DSC01234~2.ARW",
            "2023-08-26_DSC01234.ARW", "Air Show DSC01234 (1).ARW",
        ]  # fmt: skip
        self.assertEqual({match_key(v) for v in variants}, {"dsc01234.arw"})

    def test_strip_copy_marks(self):
        self.assertEqual(strip_copy_marks("x - copy (2) - copy"), "x")
        self.assertEqual(strip_copy_marks("img_1234"), "img_1234")
        self.assertEqual(strip_copy_marks("2023-08-26-1"), "2023-08-26-1")

    def test_camera_token(self):
        self.assertEqual(camera_token("2023-08-26_dsc01234"), "dsc01234")
        self.assertIsNone(camera_token("dsc01234 dsc01235"))
        self.assertIsNone(camera_token("party"))


if __name__ == "__main__":
    unittest.main()
