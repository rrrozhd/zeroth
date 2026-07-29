# zeroth-console

The bundled web console for [zeroth-core](https://pypi.org/project/zeroth-core/) —
the static UI build, packaged so it installs without a Node toolchain.

Don't install this directly; use the extra on the main package:

```bash
pip install "zeroth-core[console]"
```

Any Zeroth service then serves the console at `/console/` on the same origin as
its API. See the [Zeroth repository](https://github.com/rrrozhd/zeroth)
for documentation.
