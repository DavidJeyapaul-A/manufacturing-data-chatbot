"""Test config: makes `app/` importable, exactly as it is inside the container.

The app runs with WORKDIR /srv/app, so modules import each other flatly
(`import templates`, not `import app.templates`). Tests mirror that.
"""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
