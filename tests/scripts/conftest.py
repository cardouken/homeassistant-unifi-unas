import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "custom_components" / "unifi_unas" / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
