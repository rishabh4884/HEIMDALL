"""Test-package guard: the suite never reads or writes the developer's real HEIMDALL state.

Any test that builds an engine from default settings (``Engine()``, ``Engine.from_environment()``) resolves its state and cache
directories through the environment, so both point at one disposable directory for the whole run. Tests that need their own
state still pass explicit settings. The opt-in live tests keep an explicitly exported ``HEIMDALL_CACHE_DIR``.
"""

import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="heimdall-tests-")
os.environ["HEIMDALL_STATE_DIR"] = os.path.join(_ROOT, "state")
os.environ.setdefault("HEIMDALL_CACHE_DIR", os.path.join(_ROOT, "cache"))
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
