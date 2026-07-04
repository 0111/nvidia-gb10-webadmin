"""Environment detection & self-healing checks for the DGX Spark host.

Every check returns a `CheckResult`. Every "fix" function defaults to
`confirmed=False` and, in that case, only *describes* the command it would
run — it never executes anything dangerous (apt install, sudo, swapoff,
ethtool -s, drop_caches) without an explicit confirmation from the caller
(CLI prompt or a Web API call with confirmed=True).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Status = Literal["ok", "warning", "error"]

CUDA_COMPAT_DIR = Path("/usr/local/cuda-13.3/compat")
CUDA_COMPAT_PACKAGE = "cuda-compat-13-3"
DROP_CACHES_PATH = Path("/proc/sys/vm/drop_caches")
MEMINFO_PATH = Path("/proc/meminfo")
SWAPS_PATH = Path("/proc/swaps")
SYS_CLASS_NET = Path("/sys/class/net")

# Below this, cached memory is considered "healthy" and no reclaim is suggested.
CACHED_WARNING_RATIO = 0.40


@dataclass
class CheckResult:
    """Structured result for a single environment check.

    Attributes:
        name: machine-readable check identifier, e.g. "cuda_compat".
        status: "ok" | "warning" | "error".
        message: human readable description of what was found.
        suggested_command: command the user could run to remediate, if any.
        fixable: whether a `fix_*` / `apply_*` function exists for this check.
        details: extra structured data (free-form) for Web UI rendering.
    """

    name: str
    status: Status
    message: str
    suggested_command: str | None = None
    fixable: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class FixResult:
    """Result of attempting (or merely previewing) a remediation action."""

    name: str
    executed: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    message: str = ""


def _run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


# ---------------------------------------------------------------------------
# 1. cuda-compat
# ---------------------------------------------------------------------------

def check_cuda_compat(compat_dir: Path = CUDA_COMPAT_DIR) -> CheckResult:
    """Detect whether cuda-compat-13-3 library files are present.

    The vllm container (CUDA newer than host driver) relies on
    /usr/local/cuda-13.3/compat being bind-mounted in; if the directory is
    missing or empty, the container will fail to find libcuda.so.
    """
    if not compat_dir.exists():
        return CheckResult(
            name="cuda_compat",
            status="error",
            message=f"目录不存在: {compat_dir}，未检测到 {CUDA_COMPAT_PACKAGE} 兼容包",
            suggested_command=f"sudo apt install {CUDA_COMPAT_PACKAGE}",
            fixable=True,
        )

    required = ["libcuda.so", "libcuda.so.1"]
    found = {p.name for p in compat_dir.iterdir()} if compat_dir.is_dir() else set()
    missing = [name for name in required if name not in found]

    if missing:
        return CheckResult(
            name="cuda_compat",
            status="error",
            message=f"{compat_dir} 缺少关键库文件: {', '.join(missing)}",
            suggested_command=f"sudo apt install --reinstall {CUDA_COMPAT_PACKAGE}",
            fixable=True,
            details={"found": sorted(found)},
        )

    return CheckResult(
        name="cuda_compat",
        status="ok",
        message=f"{CUDA_COMPAT_PACKAGE} 已安装，{compat_dir} 包含所需库文件",
        details={"found": sorted(found)},
    )


def fix_cuda_compat(confirmed: bool = False) -> FixResult:
    """Install cuda-compat-13-3 via apt. Requires explicit confirmation.

    Dangerous: installs a system package. Only actually runs `apt install`
    when confirmed=True; otherwise just returns the command that *would*
    be executed so the caller (CLI prompt / Web confirmation dialog) can
    show it to the user first.
    """
    cmd = ["sudo", "apt", "install", "-y", CUDA_COMPAT_PACKAGE]
    cmd_str = " ".join(cmd)
    if not confirmed:
        return FixResult(name="cuda_compat", executed=False, command=cmd_str,
                          message="未确认执行，仅返回建议命令")
    try:
        result = _run(cmd, timeout=300)
        return FixResult(
            name="cuda_compat", executed=True, command=cmd_str,
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            message="安装命令已执行" if result.returncode == 0 else "安装命令执行失败",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return FixResult(name="cuda_compat", executed=True, command=cmd_str,
                          stderr=str(exc), message="执行异常")


# ---------------------------------------------------------------------------
# 2. ethernet speed
# ---------------------------------------------------------------------------

def _list_ethernet_interfaces() -> list[str]:
    if not SYS_CLASS_NET.exists():
        return []
    return [
        p.name for p in SYS_CLASS_NET.iterdir()
        if p.name.startswith(("en", "eth")) and (p / "speed").exists()
    ]


def check_ethernet_speed(interface: str | None = None) -> CheckResult:
    """Check link-negotiated speed of the primary wired NIC.

    Reads /sys/class/net/<iface>/speed (Mb/s). Falls back to `ethtool` if
    the sysfs value is unavailable (-1 when link is down or driver doesn't
    report it). Warns when speed negotiated at 100Mb/s instead of 1000Mb/s+.
    """
    interfaces = [interface] if interface else _list_ethernet_interfaces()
    if not interfaces:
        return CheckResult(
            name="ethernet_speed", status="warning",
            message="未找到可用的有线网卡接口 (en*/eth*)",
        )

    iface = interfaces[0]
    speed_mbps: int | None = None

    speed_file = SYS_CLASS_NET / iface / "speed"
    try:
        speed_mbps = int(speed_file.read_text().strip())
    except (OSError, ValueError):
        speed_mbps = None

    if speed_mbps is None or speed_mbps <= 0:
        try:
            result = _run(["ethtool", iface])
            match = re.search(r"Speed:\s*(\d+)Mb/s", result.stdout)
            if match:
                speed_mbps = int(match.group(1))
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    if speed_mbps is None or speed_mbps <= 0:
        return CheckResult(
            name="ethernet_speed", status="warning",
            message=f"无法确定接口 {iface} 的协商速度（链路可能未连接）",
            details={"interface": iface},
        )

    if speed_mbps < 1000:
        return CheckResult(
            name="ethernet_speed", status="warning",
            message=f"接口 {iface} 当前协商速度为 {speed_mbps}Mb/s，建议强制协商到 1000Mb/s",
            suggested_command=f"sudo ethtool -s {iface} speed 1000 duplex full autoneg on",
            fixable=True,
            details={"interface": iface, "speed_mbps": speed_mbps},
        )

    return CheckResult(
        name="ethernet_speed", status="ok",
        message=f"接口 {iface} 协商速度 {speed_mbps}Mb/s",
        details={"interface": iface, "speed_mbps": speed_mbps},
    )


def fix_ethernet_speed(interface: str, confirmed: bool = False) -> FixResult:
    """Force NIC renegotiation to 1000Mb/s full duplex via ethtool -s.

    Dangerous: briefly drops the link (and any active SSH session over it).
    """
    cmd = ["sudo", "ethtool", "-s", interface, "speed", "1000", "duplex", "full", "autoneg", "on"]
    cmd_str = " ".join(cmd)
    if not confirmed:
        return FixResult(name="ethernet_speed", executed=False, command=cmd_str,
                          message="未确认执行，仅返回建议命令")
    try:
        result = _run(cmd, timeout=15)
        return FixResult(
            name="ethernet_speed", executed=True, command=cmd_str,
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            message="已尝试强制协商" if result.returncode == 0 else "执行失败",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return FixResult(name="ethernet_speed", executed=True, command=cmd_str,
                          stderr=str(exc), message="执行异常")


# ---------------------------------------------------------------------------
# 3. drop_caches
# ---------------------------------------------------------------------------

def _parse_meminfo() -> dict[str, int]:
    """Parse /proc/meminfo into a dict of field -> kB value."""
    info: dict[str, int] = {}
    if not MEMINFO_PATH.exists():
        return info
    for line in MEMINFO_PATH.read_text().splitlines():
        match = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if match:
            info[match.group(1)] = int(match.group(2))
    return info


def check_drop_caches() -> CheckResult:
    """Evaluate whether page cache reclaim is recommended.

    Heuristic: if Cached / MemTotal exceeds CACHED_WARNING_RATIO and
    MemAvailable is comparatively low, recommend dropping caches before
    loading a large model so the unified-memory budget isn't squeezed by
    reclaimable page cache.
    """
    info = _parse_meminfo()
    mem_total = info.get("MemTotal", 0)
    cached = info.get("Cached", 0)
    mem_available = info.get("MemAvailable", 0)

    if mem_total == 0:
        return CheckResult(name="drop_caches", status="warning",
                            message="无法读取 /proc/meminfo")

    cached_ratio = cached / mem_total
    details = {
        "mem_total_kb": mem_total, "cached_kb": cached,
        "mem_available_kb": mem_available, "cached_ratio": round(cached_ratio, 3),
    }

    if cached_ratio >= CACHED_WARNING_RATIO:
        return CheckResult(
            name="drop_caches", status="warning",
            message=f"页缓存占比 {cached_ratio:.0%}，超过阈值 {CACHED_WARNING_RATIO:.0%}，建议回收缓存",
            suggested_command="sync && echo 3 | sudo tee /proc/sys/vm/drop_caches",
            fixable=True, details=details,
        )

    return CheckResult(
        name="drop_caches", status="ok",
        message=f"页缓存占比 {cached_ratio:.0%}，处于正常范围",
        details=details,
    )


def fix_drop_caches(confirmed: bool = False) -> FixResult:
    """Drop reclaimable page caches (echo 3 > /proc/sys/vm/drop_caches).

    Dangerous: momentarily increases I/O load as caches refill; requires
    root/sudo. Uses `sync` first per kernel docs recommendation.
    """
    cmd_str = "sync && echo 3 | sudo tee /proc/sys/vm/drop_caches"
    if not confirmed:
        return FixResult(name="drop_caches", executed=False, command=cmd_str,
                          message="未确认执行，仅返回建议命令")
    try:
        subprocess.run("sync", shell=False, check=False)
        result = subprocess.run(
            "echo 3 | sudo tee /proc/sys/vm/drop_caches",
            shell=True, capture_output=True, text=True, timeout=15, check=False,
        )
        return FixResult(
            name="drop_caches", executed=True, command=cmd_str,
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            message="已执行缓存回收" if result.returncode == 0 else "执行失败",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return FixResult(name="drop_caches", executed=True, command=cmd_str,
                          stderr=str(exc), message="执行异常")


# ---------------------------------------------------------------------------
# 4. swap
# ---------------------------------------------------------------------------

def check_swap() -> CheckResult:
    """Detect whether any swap device/file is currently enabled.

    On a 128GB+ unified-memory GB10, an active swap can mask real memory
    pressure and degrade latency unpredictably during model loading, so
    the recommended posture is swap disabled.
    """
    if not SWAPS_PATH.exists():
        return CheckResult(name="swap", status="warning", message="无法读取 /proc/swaps")

    lines = [l for l in SWAPS_PATH.read_text().splitlines() if l.strip()]
    entries = lines[1:]  # first line is the header

    if entries:
        return CheckResult(
            name="swap", status="warning",
            message=f"检测到 {len(entries)} 个已启用的 swap 设备/文件",
            suggested_command="sudo swapoff -a",
            fixable=True, details={"entries": entries},
        )

    return CheckResult(name="swap", status="ok", message="未检测到已启用的 swap")


def fix_swap(confirmed: bool = False) -> FixResult:
    """Disable all active swap (swapoff -a). Requires explicit confirmation.

    Dangerous: if memory is currently overcommitted, this can trigger OOM
    kills; only run when there is headroom.
    """
    cmd = ["sudo", "swapoff", "-a"]
    cmd_str = " ".join(cmd)
    if not confirmed:
        return FixResult(name="swap", executed=False, command=cmd_str,
                          message="未确认执行，仅返回建议命令")
    try:
        result = _run(cmd, timeout=30)
        return FixResult(
            name="swap", executed=True, command=cmd_str,
            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode,
            message="已关闭 swap" if result.returncode == 0 else "执行失败",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return FixResult(name="swap", executed=True, command=cmd_str,
                          stderr=str(exc), message="执行异常")


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------

@dataclass
class EnvReport:
    """Aggregated result of all environment checks, for CLI/Web display."""

    checks: list[CheckResult]

    @property
    def overall_status(self) -> Status:
        statuses = {c.status for c in self.checks}
        if "error" in statuses:
            return "error"
        if "warning" in statuses:
            return "warning"
        return "ok"

    def to_dict(self) -> dict:
        return {
            "overall_status": self.overall_status,
            "checks": [
                {
                    "name": c.name, "status": c.status, "message": c.message,
                    "suggested_command": c.suggested_command, "fixable": c.fixable,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


def run_all_checks() -> EnvReport:
    """Run cuda_compat -> ethernet -> drop_caches -> swap, in that order.

    Order matches Project_Task.md 2.3.2's CLI startup sequence so the CLI
    can simply iterate this report top-to-bottom when printing results.
    """
    return EnvReport(checks=[
        check_cuda_compat(),
        check_ethernet_speed(),
        check_drop_caches(),
        check_swap(),
    ])
