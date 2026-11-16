# Building packages

    packaging/build.sh          # both; or: packaging/build.sh rpm | deb

This builds the committed `HEAD` inside throwaway podman containers (Fedora for the RPM, Debian
trixie for the DEB) and leaves the results in `dist/`. Needs only `git` and `podman` (set
`ENGINE=docker` to use docker). Uncommitted changes are not included.

## Releasing

1. Bump `__version__` in `waddlemail/__init__.py`, `Version:` and `%changelog` in
   `packaging/rpm/waddlemail.spec`, and add an entry to the top of `debian/changelog`.
   `build.sh` refuses to run if these disagree.
2. Commit, then `packaging/build.sh`.
3. Install both packages on a clean machine and check that the app starts from the menu.
4. `git tag -a vX.Y.Z -m "WaddleMail X.Y.Z" && git push origin main vX.Y.Z`, then attach
   `dist/*.rpm` and `dist/*.deb` to a GitHub Release.

Both packages use `packaging/install-data.sh` to place the desktop file and icons under
`/usr/share`. The RPM also needs `rpm-build` if you build it outside the container:
`rpmbuild -ba packaging/rpm/waddlemail.spec` with the source tarball in `~/rpmbuild/SOURCES`.
