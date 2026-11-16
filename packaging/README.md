# Building packages

The version comes from `waddlemail/__version__`. For a release, bump it there, then also update
`Version:`/`%changelog` in `packaging/rpm/waddlemail.spec` and add an entry to `debian/changelog`.

## Fedora / RPM

    git archive --prefix=WaddleMail-1.0.0/ -o ~/rpmbuild/SOURCES/WaddleMail-1.0.0.tar.gz HEAD
    rpmbuild -ba packaging/rpm/waddlemail.spec      # or: mock -r fedora-rawhide-x86_64 on the SRPM

## Debian / Ubuntu

    sudo apt install build-essential debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools
    dpkg-buildpackage -us -uc -b                    # run in the repo root; the .deb lands one level up

Both use `packaging/install-data.sh` to place the desktop file and icons under `/usr/share`.
