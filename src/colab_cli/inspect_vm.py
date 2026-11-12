# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The read-only kernel code behind `colab inspect` (docs/08, phase C).

It describes variables by name, type, length, shape, dtype, device and size,
never by value, so a token held in a variable is not printed. The package
baseline lives on the runtime (STATE_DIR), not in local session state.
"""

import json
from typing import Any, Dict, List, Optional

STATE_DIR = "/content/.colab-cli"
MARKER = "COLAB_INSPECT "

INSPECT_SOURCE = r'''
def _colab_inspect(sections, state_dir):
    import json, os, shutil, subprocess, sys, types

    # Libraries whose objects carry a cheap shape/dtype/device.
    array_libs = {"numpy", "pandas", "torch", "tensorflow", "jax", "jaxlib",
                  "cupy", "scipy", "polars", "xarray"}
    skip_types = (types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
                  types.MethodType, type)
    out = {}

    if "vars" in sections:
        ip = get_ipython()
        hidden = set(getattr(ip, "user_ns_hidden", None) or {})
        rows = []
        for name, value in list(ip.user_ns.items()):
            if (name.startswith("_") or name in hidden
                    or name in ("In", "Out", "exit", "quit", "get_ipython")
                    or isinstance(value, skip_types)):
                continue
            kind = type(value)
            module = getattr(kind, "__module__", "") or ""
            top = module.split(".")[0]
            row = {"name": name,
                   "type": kind.__name__ if top in ("builtins", "") else f"{top}.{kind.__name__}"}
            if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
                row["len"] = len(value)
            elif top in array_libs:
                for attr in ("shape", "dtype", "device"):
                    try:
                        found = getattr(value, attr)
                    except Exception:
                        continue
                    if found is None or callable(found):
                        continue
                    if attr == "shape":
                        try:
                            found = [None if x is None else int(x) for x in found]
                        except Exception:
                            found = str(found)
                    else:
                        found = str(found)
                    row[attr] = found
                try:
                    nbytes = getattr(value, "nbytes", None)
                except Exception:
                    nbytes = None
                if isinstance(nbytes, int):
                    row["nbytes"] = nbytes
            rows.append(row)
        out["vars"] = rows[:500]

    if "gpu" in sections:
        devices = []
        try:
            done = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15)
            for line in done.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 4:
                    devices.append({"name": parts[0], "memory_used_mb": int(float(parts[1])),
                                    "memory_total_mb": int(float(parts[2])),
                                    "utilization_pct": int(float(parts[3]))})
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        gpu = {"devices": devices}
        torch = sys.modules.get("torch")
        if torch is not None:
            try:
                if torch.cuda.is_available():
                    mb = 2 ** 20
                    gpu["torch"] = {
                        "allocated_mb": round(torch.cuda.memory_allocated() / mb, 1),
                        "max_allocated_mb": round(torch.cuda.max_memory_allocated() / mb, 1),
                        "reserved_mb": round(torch.cuda.memory_reserved() / mb, 1)}
            except Exception:
                pass
        out["gpu"] = gpu

    if "disk" in sections:
        target = "/content" if os.path.isdir("/content") else os.getcwd()
        usage = shutil.disk_usage(target)
        gb = 1024 ** 3
        out["disk"] = {"path": target, "total_gb": round(usage.total / gb, 1),
                       "used_gb": round(usage.used / gb, 1), "free_gb": round(usage.free / gb, 1)}

    if "memory" in sections:
        info = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        gb = 1024 ** 3
        out["memory"] = ({"total_gb": round(info["MemTotal"] / gb, 1),
                          "available_gb": round(info.get("MemAvailable", 0) / gb, 1)}
                         if "MemTotal" in info else None)

    if "packages" in sections:
        from importlib import metadata
        current = {}
        for dist in metadata.distributions():
            name = (dist.metadata.get("Name") or "").lower()
            if name:
                current[name] = dist.version
        path = os.path.join(state_dir, "packages-baseline.json")
        created = False
        try:
            with open(path, encoding="utf-8") as f:
                base = json.load(f)
        except (OSError, ValueError):
            base, created = current, True
            try:
                os.makedirs(state_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(current, f)
            except OSError:
                pass
        out["packages"] = {
            "count": len(current),
            "baseline_created": created,
            "added": [{"name": n, "version": v} for n, v in sorted(current.items()) if n not in base],
            "removed": sorted(n for n in base if n not in current),
            "changed": [{"name": n, "from": base[n], "to": v}
                        for n, v in sorted(current.items()) if n in base and base[n] != v],
        }

    print("COLAB_INSPECT " + json.dumps(out, default=str))
'''


def build_inspect_code(sections: List[str], state_dir: str = STATE_DIR) -> str:
    return (
        INSPECT_SOURCE
        + f"_colab_inspect({list(sections)!r}, {state_dir!r})\n"
        + "del _colab_inspect\n"
    )


def parse_inspect_output(outputs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for out in outputs or []:
        for line in str(out.get("text", "")).splitlines():
            if line.startswith(MARKER):
                return json.loads(line[len(MARKER):])
    return None
