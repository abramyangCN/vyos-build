#!/usr/bin/env python3
"""Executed inside the installed QEMU guest, never on the production router."""
import pathlib
import subprocess
import unittest


class GenericDaeBootTest(unittest.TestCase):
    def test_btf_and_dae(self):
        self.assertGreater(pathlib.Path('/sys/kernel/btf/vmlinux').stat().st_size, 0)
        result = subprocess.check_output(['/usr/bin/dae', '--version'], text=True)
        self.assertIn('2.1.1', result)

    def test_bpf_modules_can_load(self):
        for name in ('sch_ingress', 'cls_bpf', 'igc', 'ixgbe', 'ixgbevf'):
            subprocess.run(['sudo', 'modprobe', name], check=True)


if __name__ == '__main__':
    unittest.main()
