"""Makes this directory a regular package, which it has to be.

TikTokApi 7.3.3 ships its own test suite as a top-level `tests` package, so
installing the `trends` extra puts a `tests/__init__.py` in site-packages. A
regular package beats a namespace package during import resolution regardless
of sys.path order, so without this file `from tests.conftest import ...` in
test_gates.py resolves to TikTokApi's tests and fails.

Being a regular package ourselves puts us back on equal footing, where sys.path
order decides -- and the project root precedes site-packages.

CI does not hit this: it installs only the `dev` extra, so TikTokApi is absent.
It appears the moment anyone installs `.[dev,trends]` to run a real scout.
"""
