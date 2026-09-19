"""Subscription rule test package (TS-098).

Making this directory a package is what lets the ordinary ``unittest discover -s tests/backend``
reach it: discovery only recurses into importable packages. The fully qualified name is
``media_rules``, which is distinct from the production package ``services.platform.media.rules`` and
from the TS-090 test package ``media``, so nothing collides.

Both entry points are supported and neither is a special case:

* ``python -m unittest discover -s tests/backend`` imports this package as ``media_rules`` and each
  test module as ``media_rules.test_*``, so the sibling fixture module is imported relatively;
* ``python -m unittest discover -s tests/backend/media_rules`` treats this directory as the
  top-level start directory, so the same modules import ``_fixtures`` as a bare top-level module.

Every test module therefore tries the relative import first and falls back to the bare one. No
global ``sys.path`` edit, no module replacement and no change to an older test entry point is
involved.
"""
