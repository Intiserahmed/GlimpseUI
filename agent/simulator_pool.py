"""
iOS Simulator Pool — manage multiple parallel simulator instances.

Allows N tests to run simultaneously, each on its own simulator.
Pool is created once per test session and torn down at the end.

Usage:
    pool = SimulatorPool(size=4)
    await pool.start()

    async def run_one(test):
        sim = await pool.acquire()
        try:
            result = await run_test_on_sim(test, sim.udid, sim.bridge_port)
        finally:
            await pool.release(sim)

    await asyncio.gather(*[run_one(t) for t in tests])
    await pool.shutdown()
"""

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SimulatorSlot:
    index:       int
    udid:        str
    name:        str
    bridge_port: int
    in_use:      bool = False


class SimulatorPool:
    """
    Manages a pool of booted iOS simulators for parallel test execution.
    Each slot gets a unique XCTest bridge port so they don't collide.
    """

    BASE_BRIDGE_PORT = 22087   # slot 0 = 22087, slot 1 = 22088, etc.

    def __init__(
        self,
        size:       int = 4,
        device:     str = "iPhone 15",
        os_version: str = "latest",
    ):
        self.size       = size
        self.device     = device
        self.os_version = os_version
        self.slots:     list[SimulatorSlot] = []
        self._lock      = asyncio.Lock()
        self._sem       = asyncio.Semaphore(size)

    async def start(self):
        """Boot all simulators. Call once before running tests."""
        print(f"Booting {self.size} {self.device} simulators...")
        tasks = [self._boot_slot(i) for i in range(self.size)]
        self.slots = await asyncio.gather(*tasks)
        print(f"Pool ready: {len(self.slots)} simulators")

    async def _boot_slot(self, index: int) -> SimulatorSlot:
        """Create and boot one simulator slot."""
        name = f"UINav-{index}"
        port = self.BASE_BRIDGE_PORT + index

        # Create simulator
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "create", name, self.device,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        udid = stdout.decode().strip()

        if not udid:
            raise RuntimeError(f"Failed to create simulator slot {index}")

        # Boot it
        await asyncio.create_subprocess_exec("xcrun", "simctl", "boot", udid)

        # Wait for boot to finish
        await self._wait_booted(udid)
        print(f"  Slot {index}: {udid[:8]}... booted on bridge port {port}")

        return SimulatorSlot(index=index, udid=udid, name=name, bridge_port=port)

    async def _wait_booted(self, udid: str, timeout: float = 60.0):
        """Poll until simctl reports the simulator as 'Booted'."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "list", "devices",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if f"(Booted)" in stdout.decode() and udid in stdout.decode():
                return
            await asyncio.sleep(1.0)
        raise TimeoutError(f"Simulator {udid[:8]} did not boot within {timeout}s")

    async def acquire(self) -> SimulatorSlot:
        """
        Get a free simulator slot.
        Blocks until one is available (respects pool size limit).
        """
        await self._sem.acquire()
        async with self._lock:
            for slot in self.slots:
                if not slot.in_use:
                    slot.in_use = True
                    return slot
        raise RuntimeError("Semaphore/pool size mismatch")

    async def release(self, slot: SimulatorSlot):
        """Return a simulator slot to the pool."""
        async with self._lock:
            slot.in_use = False
        self._sem.release()

    async def reset_slot(self, slot: SimulatorSlot):
        """
        Reset a simulator to a clean state between test runs.
        Faster than shutting down + rebooting.
        """
        try:
            await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "erase", slot.udid
            )
            await asyncio.sleep(2.0)
        except Exception:
            pass

    async def shutdown(self):
        """Shut down and delete all simulators in the pool."""
        print("Shutting down simulator pool...")
        for slot in self.slots:
            try:
                subprocess.run(["xcrun", "simctl", "shutdown", slot.udid],
                               capture_output=True)
                subprocess.run(["xcrun", "simctl", "delete", slot.udid],
                               capture_output=True)
            except Exception:
                pass
        self.slots = []
        print("Pool shut down.")


# ── Android emulator pool ─────────────────────────────────────────────────────

@dataclass
class EmulatorSlot:
    index:   int
    serial:  str
    port:    int
    in_use:  bool = False
    _proc:   object = field(default=None, repr=False)


class AndroidEmulatorPool:
    """
    Manages a pool of Android emulators for parallel test execution.
    Each emulator runs on a unique ADB port.
    """

    BASE_PORT = 5554  # ADB uses even ports: 5554, 5556, 5558, ...

    def __init__(self, size: int = 4, avd: str = "Pixel_7_API_34"):
        self.size  = size
        self.avd   = avd
        self.slots: list[EmulatorSlot] = []
        self._lock = asyncio.Lock()
        self._sem  = asyncio.Semaphore(size)

    async def start(self):
        print(f"Starting {self.size} Android emulators ({self.avd})...")
        tasks = [self._start_slot(i) for i in range(self.size)]
        self.slots = await asyncio.gather(*tasks)
        print(f"Android pool ready: {len(self.slots)} emulators")

    async def _start_slot(self, index: int) -> EmulatorSlot:
        port   = self.BASE_PORT + (index * 2)
        serial = f"emulator-{port}"

        proc = await asyncio.create_subprocess_exec(
            "emulator", "-avd", self.avd,
            "-port", str(port),
            "-no-audio", "-no-window",   # headless
            "-no-snapshot",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await self._wait_booted(serial)
        print(f"  Slot {index}: {serial} ready")
        return EmulatorSlot(index=index, serial=serial, port=port, _proc=proc)

    async def _wait_booted(self, serial: str, timeout: float = 120.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", serial, "shell",
                "getprop", "sys.boot_completed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if stdout.decode().strip() == "1":
                return
            await asyncio.sleep(2.0)
        raise TimeoutError(f"{serial} did not boot within {timeout}s")

    async def acquire(self) -> EmulatorSlot:
        await self._sem.acquire()
        async with self._lock:
            for slot in self.slots:
                if not slot.in_use:
                    slot.in_use = True
                    return slot
        raise RuntimeError("Pool mismatch")

    async def release(self, slot: EmulatorSlot):
        async with self._lock:
            slot.in_use = False
        self._sem.release()

    async def shutdown(self):
        for slot in self.slots:
            try:
                subprocess.run(["adb", "-s", slot.serial, "emu", "kill"],
                               capture_output=True)
                if slot._proc:
                    slot._proc.terminate()
            except Exception:
                pass
