"""
시스템 리소스 모니터링 API

sar(sysstat)를 활용한 CPU/메모리/디스크 사용량 조회.
목적: 클라우드 인프라 다운사이징 검토를 위한 히스토리 누적.

- GET /api/system/resources — 현재 상태 + 최근 N일 p50/p95/peak
- GET /api/system/history?metric=cpu&days=7 — 지정 메트릭 시계열
"""

import asyncio
import json
import shutil
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil
from aiohttp import web
from loguru import logger


class SystemAPIHandler:
    """시스템 리소스 API 핸들러"""

    def __init__(self):
        self._sadf_cache: Dict[str, Tuple[float, list]] = {}
        self._sadf_cache_ttl = 300  # 5분

    # -----------------------------------------------------------------
    # 현재 상태 (psutil 직접 수집 — 항상 최신)
    # -----------------------------------------------------------------
    @staticmethod
    def _current_snapshot() -> Dict:
        vmem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage('/')
        cpu_pct = psutil.cpu_percent(interval=None)
        load1, load5, load15 = psutil.getloadavg()
        boot_ts = psutil.boot_time()
        uptime_sec = int(datetime.now().timestamp() - boot_ts)

        # /proc/cpuinfo → 모델 + vCPU 개수
        try:
            with open("/proc/cpuinfo") as f:
                model = ""
                for line in f:
                    if line.startswith("model name"):
                        model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            model = ""

        return {
            "timestamp": datetime.now().isoformat(),
            "cpu": {
                "model": model,
                "vcpus": psutil.cpu_count(logical=True),
                "percent_now": round(cpu_pct, 1),
                "load1": round(load1, 2),
                "load5": round(load5, 2),
                "load15": round(load15, 2),
            },
            "memory": {
                "total_mb": round(vmem.total / 1024 / 1024, 0),
                "used_mb": round(vmem.used / 1024 / 1024, 0),
                "available_mb": round(vmem.available / 1024 / 1024, 0),
                "percent": round(vmem.percent, 1),
                "swap_total_mb": round(swap.total / 1024 / 1024, 0),
                "swap_used_mb": round(swap.used / 1024 / 1024, 0),
            },
            "disk": {
                "total_gb": round(disk.total / 1024 ** 3, 1),
                "used_gb": round(disk.used / 1024 ** 3, 1),
                "free_gb": round(disk.free / 1024 ** 3, 1),
                "percent": round(disk.percent, 1),
            },
            "uptime_days": round(uptime_sec / 86400, 1),
        }

    # -----------------------------------------------------------------
    # sar 히스토리 (sysstat)
    # -----------------------------------------------------------------
    async def _run_sadf(self, args: List[str]) -> list:
        """
        sadf -j 출력 (JSON) 파싱.

        sadf는 동기 CLI → asyncio.to_thread로 래핑.
        """
        cache_key = " ".join(args)
        now = datetime.now().timestamp()
        cached = self._sadf_cache.get(cache_key)
        if cached and (now - cached[0]) < self._sadf_cache_ttl:
            return cached[1]

        if not shutil.which("sadf"):
            return []

        try:
            def _run():
                return subprocess.run(
                    ["sadf", "-j"] + args,
                    capture_output=True, text=True, timeout=10
                )
            proc = await asyncio.to_thread(_run)
            if proc.returncode != 0:
                logger.debug(f"[SystemAPI] sadf 실패 rc={proc.returncode}: {proc.stderr[:200]}")
                return []
            data = json.loads(proc.stdout or "{}")
            # sysstat 12.x: sysstat -> hosts[0] -> statistics
            hosts = data.get("sysstat", {}).get("hosts", [])
            if not hosts:
                return []
            stats = hosts[0].get("statistics", [])
            self._sadf_cache[cache_key] = (now, stats)
            return stats
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            logger.debug(f"[SystemAPI] sadf 오류: {e}")
            return []

    @staticmethod
    def _recent_sa_files(days: int) -> List[str]:
        """최근 N일 sysstat 바이너리 파일 경로 (있는 것만)"""
        base = Path("/var/log/sysstat")
        if not base.exists():
            return []
        files = []
        for d in range(days):
            target = date.today() - timedelta(days=d)
            # sa25 형식 (day-of-month)
            fpath = base / f"sa{target.strftime('%d')}"
            if fpath.exists():
                files.append(str(fpath))
        return files

    @staticmethod
    def _pctile(values: List[float], pct: float) -> Optional[float]:
        if not values:
            return None
        s = sorted(values)
        k = int(len(s) * pct / 100)
        return s[min(k, len(s) - 1)]

    async def get_resources(self, request: web.Request) -> web.Response:
        """GET /api/system/resources — 현재 + 최근 7일 통계"""
        try:
            days = int(request.query.get("days", 7))
        except ValueError:
            days = 7
        days = max(1, min(days, 31))

        snap = self._current_snapshot()
        files = self._recent_sa_files(days)
        cpu_vals: List[float] = []
        mem_vals: List[float] = []

        for f in files:
            # CPU: sar -u (percent)
            cpu_stats = await self._run_sadf([f, "--", "-u"])
            for entry in cpu_stats:
                try:
                    cpu = entry["cpu-load"][0]
                    # idle 기반 usage (user+system+nice+iowait+steal)
                    busy = 100.0 - float(cpu.get("idle", 100))
                    cpu_vals.append(busy)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue

            # Memory: sar -r (percent)
            mem_stats = await self._run_sadf([f, "--", "-r"])
            for entry in mem_stats:
                try:
                    m = entry["memory"]
                    pct = float(m.get("memused-percent", 0))
                    mem_vals.append(pct)
                except (KeyError, TypeError, ValueError):
                    continue

        result = {
            "current": snap,
            "history": {
                "days": days,
                "samples_cpu": len(cpu_vals),
                "samples_mem": len(mem_vals),
                "cpu_percent": {
                    "avg": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
                    "p50": round(self._pctile(cpu_vals, 50), 1) if cpu_vals else None,
                    "p95": round(self._pctile(cpu_vals, 95), 1) if cpu_vals else None,
                    "peak": round(max(cpu_vals), 1) if cpu_vals else None,
                },
                "memory_percent": {
                    "avg": round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None,
                    "p50": round(self._pctile(mem_vals, 50), 1) if mem_vals else None,
                    "p95": round(self._pctile(mem_vals, 95), 1) if mem_vals else None,
                    "peak": round(max(mem_vals), 1) if mem_vals else None,
                },
            },
            "downsize_hint": _downsize_hint(snap, cpu_vals, mem_vals),
        }
        return web.json_response(result)

    async def get_history(self, request: web.Request) -> web.Response:
        """GET /api/system/history?metric=cpu|mem&days=7 — 시계열 (최대 500샘플)"""
        metric = request.query.get("metric", "cpu")
        try:
            days = int(request.query.get("days", 7))
        except ValueError:
            days = 7
        days = max(1, min(days, 31))

        series = []
        files = self._recent_sa_files(days)
        for f in files:
            if metric == "cpu":
                stats = await self._run_sadf([f, "--", "-u"])
                for entry in stats:
                    try:
                        cpu = entry["cpu-load"][0]
                        busy = 100.0 - float(cpu.get("idle", 100))
                        series.append({"ts": entry["timestamp"]["time"], "date": entry["timestamp"]["date"], "v": round(busy, 1)})
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
            elif metric == "mem":
                stats = await self._run_sadf([f, "--", "-r"])
                for entry in stats:
                    try:
                        pct = float(entry["memory"].get("memused-percent", 0))
                        series.append({"ts": entry["timestamp"]["time"], "date": entry["timestamp"]["date"], "v": round(pct, 1)})
                    except (KeyError, TypeError, ValueError):
                        continue

        # 최근 500개만
        if len(series) > 500:
            step = len(series) // 500
            series = series[::step]

        return web.json_response({"metric": metric, "days": days, "samples": len(series), "series": series})


def _downsize_hint(snap: Dict, cpu_vals: List[float], mem_vals: List[float]) -> Dict:
    """
    관측치를 바탕으로 Lightsail 플랜 다운사이징 가능성 추정.

    판단 기준 (보수적):
    - CPU p95 < 30% AND peak < 70% → 1 vCPU로도 여유
    - Memory peak < 60% AND p95 < 50% → RAM 한 단계 낮춰도 OK
    - Disk used < 40% → 더 작은 SSD로 충분
    """
    cpu_p95 = None
    cpu_peak = None
    mem_p95 = None
    mem_peak = None
    if cpu_vals:
        cpu_peak = max(cpu_vals)
        s = sorted(cpu_vals)
        cpu_p95 = s[int(len(s) * 0.95)]
    if mem_vals:
        mem_peak = max(mem_vals)
        s = sorted(mem_vals)
        mem_p95 = s[int(len(s) * 0.95)]

    hints = []
    vcpus = snap["cpu"]["vcpus"]
    total_mem = snap["memory"]["total_mb"]
    disk_used_pct = snap["disk"]["percent"]

    if cpu_p95 is not None and cpu_peak is not None:
        if cpu_p95 < 30 and cpu_peak < 70 and vcpus >= 2:
            hints.append(f"CPU: p95={cpu_p95:.0f}% / peak={cpu_peak:.0f}% — 1 vCPU 감축 검토 가능")
        elif cpu_peak > 85:
            hints.append(f"CPU: peak={cpu_peak:.0f}% — 현 스펙 유지 권장")

    if mem_p95 is not None and mem_peak is not None:
        if mem_peak < 60 and mem_p95 < 50:
            hints.append(f"Memory: p95={mem_p95:.0f}% / peak={mem_peak:.0f}% — RAM 한 단계 ↓ 검토 가능")
        elif mem_peak > 85:
            hints.append(f"Memory: peak={mem_peak:.0f}% — 현 스펙 유지 권장 (OOM 리스크)")

    if disk_used_pct < 40:
        hints.append(f"Disk: {disk_used_pct:.0f}% 사용 — 더 작은 SSD 가능 (단 Lightsail은 플랜 묶음)")

    # 샘플 부족 경고
    if cpu_vals and len(cpu_vals) < 288:  # 48시간 (10분 간격)
        hints.append(f"⚠️ 관측 샘플 {len(cpu_vals)}개 — 최소 7일 이상 누적 후 판단 권장")

    return {
        "hints": hints,
        "cpu_p95": cpu_p95,
        "cpu_peak": cpu_peak,
        "mem_p95": mem_p95,
        "mem_peak": mem_peak,
    }


def setup_system_api_routes(app: web.Application):
    """시스템 API 라우트 등록"""
    handler = SystemAPIHandler()
    app.router.add_get("/api/system/resources", handler.get_resources)
    app.router.add_get("/api/system/history", handler.get_history)
