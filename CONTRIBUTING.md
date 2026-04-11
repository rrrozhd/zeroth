# Contributing to zeroth-core

Thanks for your interest in contributing to `zeroth-core`! This guide covers
everything you need to get a local dev environment running, submit a pull
request, and file issues.

## Development setup

`zeroth-core` uses [`uv`](https://docs.astral.sh/uv/) as its package manager
and task runner.

```bash
git clone https://github.com/rrrozhd/zeroth-core.git
cd zeroth-core
uv sync --all-extras --all-groups     # install base + every extra + dev deps
uv run pytest -v                      # run the test suite
uv run ruff check src tests           # lint
uv run ruff format src                # format
```

If you only need the base runtime (no optional backends), drop
`--all-extras --all-groups` from the `uv sync` invocation.

## Running the example

The repository ships a minimal end-to-end fixture at `examples/hello.py`:

```bash
python examples/hello.py
```

The example requires an `ANTHROPIC_API_KEY` environment variable to talk to
a real LLM. If the variable is not set, the script prints a skip notice and
exits cleanly — so you can verify it runs without secrets, and CI jobs on
forked pull requests will not fail for missing credentials.

## Pull request conventions

- **Commit format:** `type(scope): subject`, e.g. `feat(memory): add redis
  backend` or `fix(orchestrator): handle empty node graph`. Conventional
  commit types we use: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`,
  `style`, `chore`.
- **Branch naming:** `feat/...`, `fix/...`, `docs/...`, `refactor/...`, etc.
- **Linked issues:** If your PR addresses an existing issue, reference it in
  the PR description (e.g. `Closes #42`).
- **Tests:** New functionality should ship with tests. Bug fixes should come
  with a regression test whenever feasible.
- **Lint & format:** Run `uv run ruff check src tests` and
  `uv run ruff format src` before pushing.
- **Docstrings:** Google-style docstrings are enforced by the docstring gate
  introduced in Phase 27.

## Filing issues

Please file bug reports and feature requests on GitHub:

https://github.com/rrrozhd/zeroth-core/issues

When reporting a bug, include:

- Steps to reproduce (a minimal snippet is ideal)
- The `zeroth-core` version (`python -c "import zeroth.core; print(zeroth.core.__version__)"` when available, otherwise your install command)
- Your Python version (`python --version`) and operating system
- The full traceback, if any

## License

`zeroth-core` is distributed under the Apache License 2.0. See the
[LICENSE](LICENSE) file for the full text.

By contributing to this repository, you agree that your contributions will
be licensed under the same Apache-2.0 terms as the rest of the project.

## Code of conduct

A formal community code of conduct will be added in a future phase. For now,
please communicate professionally and in good faith with maintainers and
other contributors.
