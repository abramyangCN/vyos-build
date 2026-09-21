# Independent vyos-generic-dae workflow

Run **Actions → vyos-generic-dae → Run workflow**, using `rolling`.
The old `dae-ready` / `with-dae` workflows and their helpers are not invoked.

## Base and additions

- Checks out **official** `vyos/vyos-build` commit
  `b5ae37645cc0630543859c4b25fb2927a91c9071` into a separate directory.
- Preserves official Debian Bookworm, rolling repositories, amd64 package list,
  generic ISO flavor and development smoketest tools. This is not a 1.5 LTS replica.
- Adds `dae.config` to the official kernel configuration; retains forced module
  signature checking, I226-V `igc`, ESXi `vmxnet3`, and Intel 82599 drivers.
- Builds kernel, Accel-PPP (including its VPP build), RTSP, QAT, ixgbe, ixgbevf,
  i40e, ice, iavf, Jool, r8152 and ipt-netflow in one container/signing-key lifetime.
  Mellanox and r8126 are not added: they are not in this official generic amd64 list.
- Keeps official firmware/microcode packages, and adds `vyos-1x-vmware` integration.
- Pins DAE v2.1.1 to a reviewed SHA-256, uses a Debian-standard filename, installs
  it through the live-build local package mechanism, and checks final dpkg status.

## Validation gates

1. Fast local regression tests, disk/KVM/container preflight, DAE download verification.
2. Complete bundle metadata/hashes, every module signature and kernel ABI, module
   resolution priority, DAE BPF/BTF and NIC kernel options.
3. Actual squashfs dpkg status, official architecture packages, exact locally built
   package versions, DAE executable, HTTPS command templates and Python API runtime.
4. Official QEMU UEFI installation/boot harness, `service_https` smoke suite and
   additional DAE/BTF/module-loading tests. No ISO artifact is published if these fail.

The complete bundle has its own cache and is saved **before** ISO assembly, so an
ISO/API failure does not discard successful kernel work. No private signing keys
are cached or uploaded. The key includes official commit, dedicated scripts,
resolved dependency refs and build-container digest. The first build also builds
VPP and can take several hours; the entire job has a 6-hour limit.

VPP's `vpp-dev` has an exact-version dependency on `libvppinfra-dev`. That
development package is kept in a small, separately keyed closure cache and its
version is checked against both `vpp-dev` and `libvppinfra` before ISO assembly.
This lets an old verified kernel bundle be repaired by rebuilding only VPP, not
the kernel and NIC modules.

VyOS applies VPP patches with `git am`; its generated package suffix can change
between builds even when the frozen source and patch refs are identical. If that
metadata-only drift occurs, the workflow rewrites the closure package's Debian
Version and exact dependency fields to the cached VPP set, leaves its payload
untouched, and then regenerates and verifies the package hash and metadata.

## DAE configuration after installation

The executable, geo assets and service are in the ISO. Store your own configuration
at `/config/dae/config.dae` (root-owned, mode 0600); this is the persistent VyOS
configuration filesystem. The systemd override validates that file before launch.
The service is enabled but **skipped when this file does not exist**: a fresh ISO
does not capture traffic or contain any nodes, API keys, WAN credentials or passwords.
After supplying a valid configuration, use `sudo systemctl start dae`.

HTTPS remains disabled until you configure API keys/listen address/client restrictions
through the official CLI. VyManager/VyMCP stay on the NAS, not inside this image.

## Artifacts and limits

- `vyos-generic-dae-amd64`: tested ISO, SHA-256 and build information.
- `vyos-generic-dae-reports-*`: build/test logs, full package manifest, kernel
  configuration, module signature manifest, source lock and applied upstream diff.
  Reports are uploaded even on failure (provided the job reaches artifact upload).

Published metadata distinguishes CI tests from real-world tests: physical 82599/
I226-V link negotiation, ISP PPPoE, DAE proxy traffic and NAS VyManager/VyMCP
end-to-end use still need testing in your ESXi environment. Internal module signing
does not certify this custom image for UEFI Secure Boot.

The official rolling APT feeds are moving, not snapshots. Source and image digests
are recorded, but this workflow does not claim bit-for-bit reproducibility or
freeze every upstream package. A changed feed can fail validation rather than
silently publishing an incomplete image.

Local checks: `python3 -m unittest discover -s scripts/ci/generic-dae -p test_build.py -v`.
