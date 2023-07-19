import os
import sys
import logging
import asyncio
import time
import signal
from configparser import ConfigParser
from usbackup.snapshot import UsbackupSnapshot
from usbackup.exceptions import UsbackupConfigError

__all__ = ['UsbackupManager']

class UsbackupManager:
    def __init__(self, params: dict) -> None:
        self._service: bool = params.get('service', False)
        self._pid_file: str = "/tmp/usbackup.pid"
        self._running: bool = False

        config = self._parse_config(params.get('config_files'))

        self._logger: logging.Logger = self._gen_logger(params.get('log_file', ''), params.get('log_level', 'INFO'))
        self._snapshots: list[UsbackupSnapshot] = self._gen_snapshots(params.get('snapshot_names'), config)

    def run(self) -> None:
        if self._service:
            self._run_service()
        else:
            self._run_once()

    def _parse_config(self, config_files: list[str]) -> dict:
        if not config_files:
            raise UsbackupConfigError("No config files specified")
        
        config_inst = ConfigParser()
        config_inst.read(config_files)

        config = {}

        for section in config_inst.sections():
            section_data = {}

            for key, value in config_inst.items(section):
                section_data[key] = value

            config[section] = section_data

        return config
    
    def _gen_logger(self, log_file: str, log_level: str) -> logging.Logger:
        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }

        format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

        if not log_level in levels:
            log_level = "INFO"

        logger = logging.getLogger()
        logger.setLevel(levels[log_level])

        if log_file:
            handler = logging.FileHandler(log_file)
        else:
            handler = logging.StreamHandler()

        handler.setLevel(levels[log_level])
        handler.setFormatter(logging.Formatter(format))

        logger.addHandler(handler)

        return logger

    def _gen_snapshots(self, snapshot_names: list[str], config: dict) -> list[UsbackupSnapshot]:
        snapshots_names = []
        config_keys = list(config.keys())

        if snapshot_names:
            names = list(set(snapshot_names))
            for name in names:
                if not name in config_keys:
                    raise UsbackupConfigError(f"Snapshot {name} not found in config file")
            
                snapshots_names.append(name)
        else:
            snapshots_names = config_keys

        if 'GLOBALS' in snapshots_names:
            snapshots_names.remove('GLOBALS')

        if not snapshots_names:
            raise UsbackupConfigError("No snapshots found in config file")
        
        global_config = {}

        if 'GLOBALS' in config_keys:
            global_config = config.get('GLOBALS')

        snapshots = []
        
        for snapshot_name in snapshots_names:
            snapshot_config = config.get(snapshot_name)
            snapshot_config = {**global_config, **snapshot_config}

            snapshots.append(UsbackupSnapshot(snapshot_name, snapshot_config, logger=self._logger))

        return snapshots

    def _run_service(self) -> None:
        pid = str(os.getpid())

        if os.path.isfile(self._pid_file):
            self._logger.error("Service is already running")
            sys.exit(1)

        with open(self._pid_file, 'w') as f:
            f.write(pid)

        self._logger.info("Starting service")

        # catch sigterm
        def sigterm_handler(signum, frame):
            raise KeyboardInterrupt
        
        signal.signal(signal.SIGTERM, sigterm_handler)

        loop = asyncio.get_event_loop()

        async def run_service():
            while True:
                self._do_backup()
                await asyncio.sleep(60)

        try:
            loop.run_until_complete(run_service())
        except (KeyboardInterrupt):
            pass
        except (Exception) as e:
            self._logger.exception(e, exc_info=True)
        finally:
            self._logger.info("Shutting down service")

            os.unlink(self._pid_file)

            tasks = asyncio.all_tasks(loop)

            for task in tasks:
                task.cancel()

            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))

            for task in tasks:
                if task.cancelled():
                    continue
                if task.exception() is not None:
                    raise task.exception()
            loop.close()
    
    def _run_once(self) -> None:
        self._do_backup()

    def _do_backup(self) -> None:
        if self._running:
            logging.warning("Previous backup is still running")
            return

        # set _running to current timestamp
        self._running = time.time()

        for snapshot in self._snapshots:
            try:
                snapshot.backup(self._running, not self._service)
            except (Exception) as e:
                self._logger.exception(f"{snapshot.name} snapshot exception: {e}", exc_info=True)
            except (KeyboardInterrupt) as e:
                snapshot.cleanup()
                raise e
            
            snapshot.cleanup()

        self._running = False