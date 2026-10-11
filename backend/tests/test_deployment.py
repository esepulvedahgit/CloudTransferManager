import asyncio
from pathlib import Path

import uvicorn
import yaml


def test_uvicorn_uses_asyncio_for_megacmd_stdin_compatibility():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    command = compose["services"]["backend"]["command"][-1].split()
    loop_option = command[command.index("--loop") + 1] if "--loop" in command else "auto"
    factory = uvicorn.Config("app.main:app", loop=loop_option).get_loop_factory()

    async def uses_standard_subprocess_loop():
        return isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)

    with asyncio.Runner(loop_factory=factory) as runner:
        assert runner.run(uses_standard_subprocess_loop())
