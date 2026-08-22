"""tests 共用設定：tools/ 非套件，補進 sys.path 讓測試可
``import build_bundle``（做法與 fcn-pricing 的 tests/conftest.py 一致）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
