"""
ZATCA's Java SDK as a test oracle and fallback (`ZATCA_SDK_HOME`, e.g.
C:\\zatca\\sdk). Only ever runs commands that need no private key:
-generateHash. Signing stays in Python. (The SDK 3.0.8's -validate fails
the XSD step even on its own signed samples, so it isn't used.)
"""

import json
import re
import subprocess
from pathlib import Path

from django.conf import settings


class SdkUnavailable(Exception):
    pass


def home(override=""):
    path = Path(override or getattr(settings, "ZATCA_SDK_HOME", "") or "")
    if not str(path) or not (path / "Apps" / "global.json").exists():
        raise SdkUnavailable("ZATCA_SDK_HOME isn't set to the SDK folder (the one containing Apps\\global.json).")
    return path


def _run(sdk_home, *args):
    apps = sdk_home / "Apps"
    config = json.loads((apps / "global.json").read_text(encoding="utf-8"))
    jar = apps / f"cli-{config['version']}-jar-with-dependencies.jar"
    if not jar.exists():
        raise SdkUnavailable(f"{jar.name} wasn't found in {apps}.")
    completed = subprocess.run(
        ["java", "-Djdk.module.illegalAccess=deny", "-Dfile.encoding=UTF-8", "-jar", str(jar),
         "--globalVersion", config["version"], "-certpassword", config.get("certPassword", ""), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return completed.stdout + completed.stderr


def generate_hash(invoice_path, sdk_home=""):
    output = _run(home(sdk_home), "-generateHash", "-invoice", str(invoice_path))
    match = re.search(r"INVOICE HASH = (\S+)", output)
    if not match:
        raise SdkUnavailable(f"The SDK printed no INVOICE HASH for {Path(invoice_path).name}.")
    return match.group(1)

