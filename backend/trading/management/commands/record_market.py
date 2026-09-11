import asyncio
import signal

from django.core.management.base import BaseCommand

from trading.locking import single_worker
from trading.recording import record_forever


class Command(BaseCommand):
    help = "Record public Binance spot, Bybit derivatives, news and optional AI/heatmap observations"

    def handle(self, *args, **options):
        with single_worker("recorder") as check:
            async def main():
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, stop.set)

                async def monitor():
                    while not stop.is_set():
                        await asyncio.to_thread(check)
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=5)
                        except TimeoutError:
                            pass

                async with asyncio.TaskGroup() as group:
                    group.create_task(monitor())
                    group.create_task(record_forever(stop))
            asyncio.run(main())
