#!/usr/bin/env python3
"""Independent generic + dae build driver; never imports the old custom CI.

All mutable build output is confined to a disposable checkout of official VyOS.
Package build failures are propagated, unlike upstream build_package's catch.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

HERE = Path(__file__).resolve().parent
UPSTREAM = "b5ae37645cc0630543859c4b25fb2927a91c9071"
DAE_VERSION = "v2.1.1"
DAE_SHA256 = "bae957fcfaa647e72718d4565feb282115408f8d56cc90b5d49ca21147b512d2"
COMPONENTS = (
    "linux-kernel", "accel-ppp-ng", "nat-rtsp", "qat", "ixgbe", "ixgbevf",
    "i40e", "ice", "iavf", "jool", "realtek-r8152", "ipt-netflow",
)
RUNTIME_PACKAGES = {
    "accel-ppp-ng", "nat-rtsp", "vyos-intel-qat", "vyos-intel-ixgbe",
    "vyos-intel-ixgbevf", "vyos-intel-i40e", "vyos-intel-ice",
    "vyos-intel-iavf", "jool", "vyos-drivers-realtek-r8152", "vyos-ipt-netflow",
}
BASE_PACKAGES = {
    "vyos-1x", "vyos-user-utils", "vyos-http-api-tools", "nginx-light",
    "ssl-cert", "openssl", "frr", "nftables", "podman", "wireguard-tools",
    "openvpn", "strongswan", "isc-kea-dhcp4", "isc-kea-dhcp6", "pdns-recursor",
    "pppoe", "vyos-linux-firmware", "vyos-1x-smoketest", "vyos-1x-vmware",
    "open-vm-tools", "dae",
}


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_info(path):
    values = output("dpkg-deb", "--show", "--showformat=${Package}\t${Version}\t${Architecture}", path).split("\t")
    require(len(values) == 3, f"Invalid package metadata: {path}")
    return dict(zip(("package", "version", "architecture"), values))


def resolve_ref(url, ref):
    if re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return ref
    rows = output("git", "ls-remote", url, f"refs/heads/{ref}",
                  f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}")
    refs = dict(line.split()[::-1] for line in rows.splitlines())
    sha = refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}") or refs.get(f"refs/heads/{ref}")
    require(sha and re.fullmatch(r"[0-9a-f]{40}", sha), f"Cannot resolve {url}: {ref}")
    return sha


def prepare(root):
    import toml  # Only needed to serialize upstream package manifests.
    require(output("git", "-C", root, "rev-parse", "HEAD") == UPSTREAM,
            "Unexpected upstream commit; review this driver before updating it")
    require(not output("git", "-C", root, "status", "--porcelain"),
            "prepare requires a fresh official checkout")
    kernel = root / "scripts/package-build/linux-kernel"
    shutil.copyfile(HERE / "dae.config", kernel / "config/99-generic-dae.config")
    # Upstream resets to origin/main after checking out package.toml's revision.
    # Apply this narrowly checked fix ONLY inside the disposable official checkout.
    path = kernel / "build-intel-nic.sh"
    original = path.read_text()
    require(original.count("git reset --hard origin/main") == 1, "Intel reset patch no longer applies")
    path.write_text(original.replace("git reset --hard origin/main", "git reset --hard HEAD"))
    sources = {}
    for relative in ("linux-kernel", "vpp"):
        path = root / f"scripts/package-build/{relative}/package.toml"
        config = tomllib.loads(path.read_text())
        for package in config["packages"]:
            if relative == "linux-kernel" and package["name"] not in COMPONENTS:
                continue
            url, ref = package.get("scm_url"), package.get("commit_id")
            if url and ref:
                url = url.replace("http://github.com/", "https://github.com/")
                package["scm_url"] = url
                package["commit_id"] = resolve_ref(url, ref)
                sources[package["name"]] = {"url": url, "ref": package["commit_id"]}
        path.write_text(toml.dumps(config))
    reports = root.parent / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "source-lock.json").write_text(json.dumps(sources, indent=2) + "\n")
    # Include driver, overlay, workflow and all upstream recipes in the cache key.
    h = hashlib.sha256(UPSTREAM.encode())
    for path in sorted(HERE.glob("*")):
        if path.is_file():
            h.update(path.name.encode())
            h.update(path.read_bytes())
    h.update((HERE.parents[2] / ".github/workflows/vyos-generic-dae.yml").read_bytes())
    h.update((reports / "source-lock.json").read_bytes())
    h.update(os.environ["BUILD_IMAGE"].encode())
    cache_id = h.hexdigest()
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"cache_id={cache_id}\n")


def build(root):
    kernel = root / "scripts/package-build/linux-kernel"
    os.chdir(kernel)
    spec = importlib.util.spec_from_file_location("official_kernel_builder", kernel / "build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    config = tomllib.loads((kernel / "package.toml").read_text())
    defaults = tomllib.loads((root / "data/defaults.toml").read_text())
    builder.ensure_dependencies(config["dependencies"]["packages"])
    # Dispatch the official low-level build functions directly: no swallowed failures.
    recipes = {p["name"]: p for p in config["packages"]}
    for name in COMPONENTS:
        p = recipes[name]
        print(f"::group::Build {name}", flush=True)
        command = p["build_cmd"]
        if name == "linux-kernel":
            builder.build_kernel(defaults["kernel_version"], defaults["kernel_flavor"])
        elif command == "build_intel_nic":
            builder.build_intel(name, p["commit_id"], p["scm_url"])
        elif name in ("accel-ppp-ng", "nat-rtsp", "ipt-netflow"):
            getattr(builder, command)(p["commit_id"], p["scm_url"])
        else:
            getattr(builder, command)()
        print("::endgroup::", flush=True)
    bundle = root.parent / "generic-dae-bundle"
    bundle.mkdir(exist_ok=True)
    require(not list(bundle.glob("*.deb")), "Refusing to mix an existing bundle with new binaries")
    records = []
    for deb in sorted(kernel.glob("*.deb")):
        info = package_info(deb)
        name = info["package"]
        # VPP is built by Accel-PPP; keep its runtime packages together for ABI parity.
        keep = (name in RUNTIME_PACKAGES or name.startswith("linux-image-") or
                name in {"vpp", "libvppinfra", "python3-vpp-api"} or name.startswith("vpp-"))
        if not keep or name.endswith(("-dbg", "-dbgsym")):
            continue
        require(info["architecture"] in ("amd64", "all"), f"Wrong architecture: {deb}")
        dest = bundle / f"{name}_{info['version']}_{info['architecture']}.deb"
        require(not dest.exists(), f"Duplicate package: {name}")
        shutil.copy2(deb, dest)
        records.append({**info, "file": dest.name, "sha256": digest(dest)})
    names = {r["package"] for r in records}
    expected_kernel = f"linux-image-{defaults['kernel_version']}-{defaults['kernel_flavor']}"
    require((RUNTIME_PACKAGES | {expected_kernel, "vpp", "libvppinfra"}) <= names,
            f"Incomplete built bundle: {(RUNTIME_PACKAGES | {expected_kernel, 'vpp', 'libvppinfra'}) - names}")
    (bundle / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
    shutil.copy2(root.parent / "reports/source-lock.json", bundle / "source-lock.json")


def check_bundle(bundle):
    records = json.loads((bundle / "manifest.json").read_text())
    require(bool(records), "Empty bundle")
    require({p.name for p in bundle.glob("*.deb")} == {r["file"] for r in records}, "Bundle file set differs from manifest")
    require(len({r["package"] for r in records}) == len(records), "Duplicate bundle package")
    require(RUNTIME_PACKAGES <= {r["package"] for r in records}, "Missing runtime packages")
    for record in records:
        filename = record["file"]
        require(Path(filename).name == filename, "Invalid bundle filename")
        path = bundle / filename
        require(digest(path) == record["sha256"], f"Corrupt bundle: {filename}")
        require(package_info(path) == {k: record[k] for k in ("package", "version", "architecture")},
                f"Package metadata mismatch: {filename}")
    return records


def stage_dae(root):
    packages = root / "packages"
    packages.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        deb = Path(temp) / "dae.deb"
        run("curl", "-fL", "--retry", "5", "--connect-timeout", "30", "--max-time", "600",
            f"https://github.com/daeuniverse/dae/releases/download/{DAE_VERSION}/dae-linux-x86_64.deb", "-o", deb)
        require(digest(deb) == DAE_SHA256, "DAE release digest mismatch")
        info = package_info(deb)
        require(info["package"] == "dae" and info["architecture"] == "amd64", "Wrong DAE package")
        require(info["version"].lstrip("v") == DAE_VERSION.lstrip("v"), "Wrong DAE version")
        # Consume the entire stream; never pipe dpkg-deb to grep -q (SIGPIPE).
        listing = output("dpkg-deb", "--contents", deb)
        require(re.search(r"\./usr/bin/dae$", listing, re.MULTILINE), "Missing DAE executable")
        run("dpkg-name", deb)
        normalized = list(Path(temp).glob("dae_*_amd64.deb"))
        require(len(normalized) == 1, "DAE package normalization failed")
        shutil.copy2(normalized[0], packages / normalized[0].name)


def module_report(rootfs, reports):
    module_base = rootfs / "lib/modules"
    versions = [p for p in module_base.iterdir() if p.is_dir()]
    require(len(versions) == 1, "Expected exactly one kernel module tree")
    tree = versions[0]
    config = (rootfs / "boot" / f"config-{tree.name}").read_text()
    for option in (HERE / "dae.config").read_text().splitlines():
        if option.startswith("CONFIG_") or option.startswith("# CONFIG_"):
            require(option in config.splitlines(), f"Missing kernel option: {option}")
    dummy = list((tree / "kernel").rglob("dummy.ko*"))
    require(len(dummy) == 1, "Missing reference module")
    reference = output("modinfo", "-F", "sig_key", dummy[0])
    require(bool(reference), "Kernel module signing key missing")
    modules = [p for p in tree.rglob("*") if p.is_file() and re.search(r"\.ko(?:\.(?:xz|zst|gz))?$", p.name)]
    lines = []
    for module in modules:
        # Check every shipped module, not only the 82599 drivers.
        key = output("modinfo", "-F", "sig_key", module)
        vermagic = output("modinfo", "-F", "vermagic", module)
        require(key == reference, f"Foreign/unsigned module: {module}")
        require(vermagic.split()[0] == tree.name, f"Wrong kernel ABI: {module}")
        lines.append(f"{module.relative_to(rootfs)}\t{key}\t{vermagic}")
    for name in ("igc", "vmxnet3", "ixgbe", "ixgbevf", "i40e", "ice", "iavf",
                 "sch_ingress", "cls_bpf", "ipoe", "vlan_mon", "jool", "nf_conntrack_rtsp", "ipt_NETFLOW"):
        resolved = output("modinfo", "-b", rootfs, "-k", tree.name, "-n", name)
        require(bool(resolved), f"Cannot resolve module {name}")
        if name in ("ixgbe", "ixgbevf", "i40e", "ice", "iavf"):
            require("/updates/" in resolved, f"Wrong Intel module priority: {resolved}")
    (reports / "modules-manifest.txt").write_text("\n".join(sorted(lines)) + "\n")
    (reports / "kernel-config.txt").write_text(config)


def installed_packages(rootfs):
    listing = output("dpkg-query", f"--admindir={rootfs}/var/lib/dpkg", "-W",
                     "-f=${Package}\t${Version}\t${Status}\n")
    installed = {}
    for line in listing.splitlines():
        name, version, status = line.split("\t", 2)
        if status == "install ok installed":
            installed[name] = version
    return installed, listing


def verify(root):
    rootfs = root.parent / "image-root"
    reports = root.parent / "reports"
    records = check_bundle(root.parent / "generic-dae-bundle")
    installed, listing = installed_packages(rootfs)
    require(BASE_PACKAGES <= installed.keys(), f"Missing installed packages: {BASE_PACKAGES - installed.keys()}")
    architecture = tomllib.loads((root / "data/architectures/amd64.toml").read_text())
    require(set(architecture["packages"]) <= installed.keys(), "Official amd64 package list is incomplete")
    for record in records:
        require(installed.get(record["package"]) == record["version"], f"Local package not installed: {record['package']}")
    require(installed["dae"].lstrip("v") == DAE_VERSION.lstrip("v"), "Wrong installed DAE version")
    (reports / "packages-manifest.txt").write_text(listing + "\n")
    require(not output("chroot", rootfs, "dpkg", "--audit"), "dpkg reports incomplete installation")
    run("chroot", rootfs, "apt-get", "check")
    module_report(rootfs, reports)
    templates = "/opt/vyatta/share/vyatta-cfg/templates/service/https"
    for filename in (f"{templates}/node.def", f"{templates}/api/rest/node.def",
                     f"{templates}/api/graphql/node.def",
                     "/usr/libexec/vyos/tests/smoke/cli/test_service_https.py"):
        run("chroot", rootfs, "test", "-s", filename)
    for filename in ("/usr/bin/dae", "/usr/libexec/vyos/conf_mode/service_https.py",
                     "/usr/libexec/vyos/services/vyos-http-api-server", "/usr/sbin/nginx",
                     "/usr/libexec/vyos/tests/smoke/cli/test_service_https.py",
                     "/usr/libexec/vyos/tests/smoke/cli/test_generic_dae.py"):
        run("chroot", rootfs, "test", "-x", filename)
    run("chroot", rootfs, "test", "-s", "/usr/lib/systemd/system/dae.service")
    require("ConditionPathExists=/config/dae/config.dae" in
            (rootfs / "etc/systemd/system/dae.service.d/10-vyos-persistent.conf").read_text(),
            "Missing persistent DAE service guard")
    run("chroot", rootfs, "/usr/share/vyos-http-api-tools/bin/python3", "-c",
        "import fastapi, uvicorn, ariadne, jwt, multipart, wsproto, pam")
    run("chroot", rootfs, "/usr/bin/dae", "--version")
    (reports / "rootfs-validation.txt").write_text("PASS: dpkg, local packages, DAE, HTTPS runtime, kernel config, all module signatures/ABI\n")


def verify_bundle(root):
    bundle = root.parent / "generic-dae-bundle"
    records = check_bundle(bundle)
    with tempfile.TemporaryDirectory(prefix="generic-dae-bundle-") as temp:
        rootfs = Path(temp)
        for record in records:
            run("dpkg-deb", "-x", bundle / record["file"], rootfs)
        trees = list((rootfs / "lib/modules").iterdir())
        require(len(trees) == 1, "Mixed kernel versions in bundle")
        run("depmod", "-b", rootfs, trees[0].name)
        module_report(rootfs, root.parent / "reports")


def stage(root):
    bundle = root.parent / "generic-dae-bundle"
    records = check_bundle(bundle)
    for record in records:
        shutil.copy2(bundle / record["file"], root / "packages" / record["file"])
    # Local packages must exist before ISO assembly, rather than an APT name for dae.
    require(len(list((root / "packages").glob("dae_*_amd64.deb"))) == 1, "DAE not staged")
    reports = root.parent / "reports"
    shutil.copy2(bundle / "manifest.json", reports / "bundle-manifest.json")
    includes = root / "data/live-build-config/includes.chroot"
    smoke = includes / "usr/libexec/vyos/tests/smoke/cli/test_generic_dae.py"
    smoke.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "test_guest.py", smoke)
    smoke.chmod(0o755)
    # Store user configuration on VyOS's persistent /config filesystem.
    # An unconfigured image must not start intercepting any traffic.
    override = includes / "etc/systemd/system/dae.service.d/10-vyos-persistent.conf"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("[Unit]\nAfter=vyos-router.service\n"
                        "ConditionPathExists=/config/dae/config.dae\n\n"
                        "[Service]\nExecStartPre=\n"
                        "ExecStartPre=/usr/bin/dae validate -c /config/dae/config.dae\n"
                        "ExecStart=\n"
                        "ExecStart=/usr/bin/dae run --disable-timestamp -c /config/dae/config.dae\n")
    wants = includes / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    (wants / "dae.service").symlink_to("/usr/lib/systemd/system/dae.service")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "build", "stage-dae", "stage", "verify", "verify-bundle"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    {"prepare": prepare, "build": build, "stage-dae": stage_dae,
     "stage": stage, "verify": verify, "verify-bundle": verify_bundle}[args.stage](root)


if __name__ == "__main__":
    main()
