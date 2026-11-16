#!/usr/bin/env bash
# Build the .rpm and .deb for the committed HEAD in throwaway podman containers.
# Usage: packaging/build.sh [rpm|deb|all]      Output goes to dist/
# Env:   FEDORA_IMAGE, DEBIAN_IMAGE (container images), ENGINE (podman or docker)
set -euo pipefail

cd "$(dirname "$0")/.."
what="${1:-all}"
ENGINE="${ENGINE:-podman}"
FEDORA_IMAGE="${FEDORA_IMAGE:-registry.fedoraproject.org/fedora:latest}"
DEBIAN_IMAGE="${DEBIAN_IMAGE:-docker.io/library/debian:trixie}"

version=$(python3 -c 'import re;print(re.search(r"\"(.+)\"",open("waddlemail/__init__.py").read()).group(1))')

# the version must agree everywhere, or the packages would be mislabelled
grep -q "^Version:[[:space:]]*$version\$" packaging/rpm/waddlemail.spec \
    || { echo "spec Version does not match $version" >&2; exit 1; }
grep -q "^waddlemail ($version)" debian/changelog \
    || { echo "debian/changelog top entry does not match $version" >&2; exit 1; }

[ -z "$(git status --porcelain --untracked-files=no -- . ':!.idea')" ] \
    || echo "warning: uncommitted changes are NOT included; packages are built from HEAD" >&2

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p dist
git archive --prefix="WaddleMail-$version/" -o "$work/WaddleMail-$version.tar.gz" HEAD

build_rpm() {
    cat > "$work/rpm.sh" <<'INNER'
set -e
dnf -y -q install rpm-build python3-devel pyproject-rpm-macros desktop-file-utils >/dev/null
mkdir -p ~/rpmbuild/SOURCES
cp /work/WaddleMail-*.tar.gz ~/rpmbuild/SOURCES/
tar -xzf /work/WaddleMail-*.tar.gz -C /tmp
spec=$(ls /tmp/WaddleMail-*/packaging/rpm/waddlemail.spec)
# dynamic BuildRequires: build, install what rpm reports missing, repeat
for _ in 1 2 3 4 5; do
    if rpmbuild -ba "$spec" >/tmp/rpmbuild.log 2>&1; then ok=1; break; fi
    n=$(ls ~/rpmbuild/SRPMS/*buildreqs.nosrc.rpm 2>/dev/null | head -1)
    if [ -z "$n" ]; then tail -40 /tmp/rpmbuild.log; exit 1; fi
    rpm -qp --requires "$n" | grep -v '^rpmlib' | while read -r r; do dnf -y -q install "$r" >/dev/null; done
    rm -f "$n"
done
[ "${ok:-}" = 1 ] || { tail -40 /tmp/rpmbuild.log; exit 1; }
cp ~/rpmbuild/RPMS/noarch/*.rpm ~/rpmbuild/SRPMS/*.src.rpm /out/
INNER
    "$ENGINE" run --rm -v "$work:/work:Z" -v "$PWD/dist:/out:Z" "$FEDORA_IMAGE" bash /work/rpm.sh
}

build_deb() {
    cat > "$work/deb.sh" <<'INNER'
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq build-essential debhelper dh-python pybuild-plugin-pyproject \
    python3-all python3-setuptools >/dev/null
mkdir /build && tar -xzf /work/WaddleMail-*.tar.gz -C /build
cd /build/WaddleMail-*
dpkg-buildpackage -us -uc -b >/tmp/deb.log 2>&1 || { tail -40 /tmp/deb.log; exit 1; }
cp /build/*.deb /out/
INNER
    "$ENGINE" run --rm -v "$work:/work:Z" -v "$PWD/dist:/out:Z" "$DEBIAN_IMAGE" bash /work/deb.sh
}

case "$what" in
    rpm) build_rpm ;;
    deb) build_deb ;;
    all) build_rpm; build_deb ;;
    *) echo "usage: $0 [rpm|deb|all]" >&2; exit 2 ;;
esac

echo; echo "Built in dist/:"; ls -1 dist
