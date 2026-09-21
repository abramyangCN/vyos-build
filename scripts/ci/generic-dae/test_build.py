"""Fast regression tests: no Docker, kernel build or network required."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("generic_dae_builder", Path(__file__).with_name("build.py"))
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class BuildTests(unittest.TestCase):
    def test_subprocess_failure_propagates(self):
        with self.assertRaises(subprocess.CalledProcessError):
            driver.run("/bin/sh", "-c", "exit 7")

    def test_full_git_commit_does_not_query_moving_refs(self):
        with patch.object(driver, "output") as command:
            self.assertEqual(driver.resolve_ref("https://example.invalid/repo", "a" * 40), "a" * 40)
            command.assert_not_called()

    def test_annotated_tag_uses_peeled_commit(self):
        refs = f"{'a'*40}\trefs/tags/v1\n{'b'*40}\trefs/tags/v1^{{}}"
        with patch.object(driver, "output", return_value=refs):
            self.assertEqual(driver.resolve_ref("repo", "v1"), "b" * 40)

    def test_missing_ref_is_fatal(self):
        with patch.object(driver, "output", return_value=""):
            with self.assertRaises(RuntimeError):
                driver.resolve_ref("repo", "missing")

    def test_dpkg_removed_package_is_not_installed(self):
        raw = "dae\t2.1.1\tdeinstall ok config-files\nnginx-light\t1.0\tinstall ok installed"
        with patch.object(driver, "output", return_value=raw):
            installed, _ = driver.installed_packages(Path("/unused"))
            self.assertNotIn("dae", installed)
            self.assertIn("nginx-light", installed)

    def test_package_metadata(self):
        with patch.object(driver, "output", return_value="dae\t2.1.1\tamd64"):
            self.assertEqual(driver.package_info("file"),
                             {"package": "dae", "version": "2.1.1", "architecture": "amd64"})

    def test_bundle_rejects_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text("[]")
            with self.assertRaisesRegex(RuntimeError, "Empty"):
                driver.check_bundle(root)

    def test_bundle_checks_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "test.deb").write_bytes(b"modified")
            (root / "manifest.json").write_text(json.dumps([
                {"file": "test.deb", "package": "test", "sha256": "0" * 64}
            ]))
            with patch.object(driver, "RUNTIME_PACKAGES", {"test"}):
                with self.assertRaisesRegex(RuntimeError, "Corrupt"):
                    driver.check_bundle(root)

    def test_missing_component_cannot_pass_bundle_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "test.deb").touch()
            (root / "manifest.json").write_text(json.dumps([
                {"file": "test.deb", "package": "test", "sha256": "0" * 64}
            ]))
            with self.assertRaisesRegex(RuntimeError, "Missing runtime"):
                driver.check_bundle(root)

    def test_low_level_build_failure_is_not_swallowed(self):
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                kernel = root / "scripts/package-build/linux-kernel"
                kernel.mkdir(parents=True)
                (root / "data").mkdir()
                (root / "data/defaults.toml").write_text('kernel_version="test"\nkernel_flavor="vyos"\n')
                (kernel / "package.toml").write_text(
                    '[dependencies]\npackages=[]\n[[packages]]\nname="linux-kernel"\nbuild_cmd="build_kernel"\n')
                (kernel / "build.py").write_text(
                    'def ensure_dependencies(deps): pass\n'
                    'def build_kernel(*args): raise RuntimeError("kernel failed")\n')
                with patch.object(driver, "COMPONENTS", ("linux-kernel",)):
                    with self.assertRaisesRegex(RuntimeError, "kernel failed"):
                        driver.build(root)
        finally:
            os.chdir(previous)

    def test_stage_adds_guarded_persistent_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "upstream"
            (root / "packages").mkdir(parents=True)
            (root / "packages/dae_2.1.1_amd64.deb").touch()
            bundle = root.parent / "generic-dae-bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_text("[]")
            (root.parent / "reports").mkdir()
            with patch.object(driver, "check_bundle", return_value=[]):
                driver.stage(root)
            includes = root / "data/live-build-config/includes.chroot"
            override = (includes / "etc/systemd/system/dae.service.d/10-vyos-persistent.conf").read_text()
            self.assertIn("ConditionPathExists=/config/dae/config.dae", override)
            self.assertIn("validate -c /config/dae/config.dae", override)
            self.assertFalse((includes / "etc/systemd/system/multi-user.target.wants/dae.service").exists())
            hook = root / "data/live-build-config/hooks/live/99-enable-dae.chroot"
            self.assertTrue(hook.stat().st_mode & 0o111)
            self.assertIn("systemctl enable dae.service", hook.read_text())
            # Reproduce build-vyos-image's exact copy mode. It must not follow
            # a dangling service symlink before local packages are installed.
            shutil.copytree(root / "data/live-build-config", root.parent / "copied-live-build-config")
            self.assertFalse((includes / "config/dae/config.dae").exists())


if __name__ == "__main__":
    unittest.main()
