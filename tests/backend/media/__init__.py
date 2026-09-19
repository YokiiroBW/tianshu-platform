"""Bilibili media metadata test package (TS-090).

Making this directory a package is what lets the ordinary ``unittest discover -s tests/backend``
run reach it: discovery only recurses into directories that are importable packages. The fully
qualified name is ``media``, which is distinct from the production package
``services.platform.media``, so nothing collides.

Both entry points are supported and neither is a special case:

* ``python -m unittest discover -s tests/backend`` imports this package as ``media`` and each test
  module as ``media.test_*``, so the sibling fixture module is imported relatively;
* ``python -m unittest discover -s tests/backend/media`` treats this directory as the top-level
  start directory, so the same modules import ``_fixtures`` as a bare top-level module.

Every test module therefore tries the relative import first and falls back to the bare one. No
global ``sys.path`` edit, no module replacement and no change to an older test entry point is
involved.
"""
