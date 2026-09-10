# zeroth-core compatibility package

`zeroth-core` has been renamed to `zeroth-platform`.

This package contains no Zeroth implementation. It installs the exact matching
`zeroth-platform` release. Existing real-file installations require the migration
sequence below; a direct in-place pip upgrade can remove platform files.

New installations should use:

```bash
python -m pip install zeroth-platform
```

## Upgrading an existing installation

A fresh virtual environment is the simplest migration option. If reusing an
environment containing an older, real-file `zeroth-core`, uninstall that package
**before** installing the platform or compatibility release. Otherwise pip can
install the platform first, then remove its shared `zeroth/*` files while
uninstalling the old package.

Once the matching releases are available on your configured package index:

```bash
python -m pip uninstall -y zeroth-core
python -m pip install --no-deps --force-reinstall "zeroth-platform==0.25.9.6"
python -m pip install "zeroth-core==0.25.9.6"
```

The second command reinstalls only the exact platform distribution, without
forcing dependency reinstalls. It also repairs an environment where a previous
partial migration left platform metadata installed but deleted shared files.
The third command installs the dependency-only compatibility package and resolves
its normal dependencies. The old PyPI `zeroth-core==0.1.0` is not this compatibility
release. Keep the previous environment available until the migrated deployment
has passed its import and application checks.

The existing `zeroth-core` command remains available as a temporary compatibility alias;
new automation should invoke `zeroth`.
