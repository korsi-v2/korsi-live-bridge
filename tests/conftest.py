#
# SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Test setup.

`ex_app/lib` is put on the path because the app's modules import each other flatly (`from constants
import ...`) -- that is upstream's layout, driven by `WORKDIR /ex_app/lib` in the container, and
changing it would touch every file for no benefit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ex_app" / "lib"))
