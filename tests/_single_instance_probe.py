"""Child process for the real Windows named-object lifecycle test."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.single_instance import SingleInstance


root = Path(sys.argv[1])
action = sys.argv[2]
guard = SingleInstance(root, show_existing=action != "hidden")
print("PRIMARY" if guard.primary else "SECONDARY", flush=True)
if action == "hold":
    time.sleep(60)
guard.close()
