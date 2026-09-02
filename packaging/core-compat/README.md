# zeroth-core compatibility package

`zeroth-core` has been renamed to `zeroth-platform`.

This package contains no Zeroth implementation. It installs the exact matching
`zeroth-platform` release so existing installation manifests can migrate without an
immediate break. New installations should use:

```bash
python -m pip install zeroth-platform
```

The existing `zeroth-core` command remains available as a temporary compatibility alias;
new automation should invoke `zeroth`.
