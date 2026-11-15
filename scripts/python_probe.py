"""Native interpreter facts used by the PowerShell prerequisite check."""
import json
import platform
import sys

print(json.dumps({"machine": platform.machine(), "version": list(sys.version_info[:3]), "executable": sys.executable}))
