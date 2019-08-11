import os
import unittest
from shutil import rmtree

import numpy as np


class TestVolumeUtil(unittest.TestCase):
    tmp_dir = './tmp'

    def setUp(self):
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        try:
            rmtree(self.tmp_dir)
        except OSError:
            pass

    def test_file_reader(self):
        from cluster_tools.utils.volume_utils import file_reader

        def _test_io(f):
            data = np.random.rand(100, 100)
            ds = f.create_dataset('data', data=data, chunks=(10, 10))
            out = ds[:]
            self.assertEqual(out.shape, data.shape)
            self.assertTrue(np.allclose(out, data))

        # test n5
        path = os.path.join(self.tmp_dir, 'a.n5')
        with file_reader(path) as f:
            _test_io(f)

        # test zr
        path = os.path.join(self.tmp_dir, 'a.zr')
        with file_reader(path) as f:
            _test_io(f)

        # test h5
        path = os.path.join(self.tmp_dir, 'a.h5')
        with file_reader(path) as f:
            _test_io(f)


if __name__ == '__main__':
    unittest.main()
