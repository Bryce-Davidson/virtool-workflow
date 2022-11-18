import asyncio
import os
import signal
import sys
from importlib import import_module
from logging import getLogger
from pathlib import Path
from typing import Any, Dict, Optional

from pyfixtures import FixtureScope, runs_in_new_fixture_context
from virtool_core.logging import configure_logs
from virtool_core.redis import configure_redis

from virtool_workflow import discovery, execute
from virtool_workflow.runtime.events import Events
from virtool_workflow.hooks import (
    on_failure,
    on_cancelled,
    on_success,
    on_step_start,
    on_terminated,
    on_error,
)
from virtool_workflow.redis import get_next_job_with_timeout, wait_for_cancellation
from virtool_workflow.runtime.sentry import configure_sentry
from virtool_workflow.workflow import Workflow

logger = getLogger("runtime")


def configure_workflow(
    fixtures_path: Optional[Path],
    workflow_path: Path,
):
    logger.info("Importing workflow")
    workflow = discovery.discover_workflow(workflow_path)

    logger.info(f"Looking for fixtures at '{fixtures_path}'")

    using_custom_fixtures_path = bool(fixtures_path)

    if not fixtures_path:
        fixtures_path = Path("./fixtures.py")

    try:
        discovery.import_module_from_file(
            fixtures_path.name.rstrip(".py"), fixtures_path
        )
    except FileNotFoundError:
        if using_custom_fixtures_path:
            logger.fatal(f"No fixtures file found at '{fixtures_path}'")
            sys.exit(1)
        else:
            logger.info(f"No fixtures.py found")

    for name in (
        "virtool_workflow.builtin_fixtures",
        "virtool_workflow.analysis.fixtures",
        "virtool_workflow.runtime.providers",
    ):
        module = import_module(name)
        logger.debug(f"Imported {module}")

    return workflow


def configure_builtin_status_hooks():
    """
    Configure built-in job status hooks.

    Push status updates to API when various lifecycle hooks are triggered.

    """

    @on_step_start
    async def handle_step_start(push_status):
        await push_status(state="running")

    @on_error(once=True)
    async def handle_error(error, push_status):
        await push_status(
            stage="",
            state="error",
            error=error,
            max_tb=50,
        )

    @on_cancelled(once=True)
    async def handle_cancelled(push_status):
        await push_status(stage="", state="cancelled")

    @on_terminated
    async def handle_terminated(push_status):
        await push_status(stage="", state="terminated")

    @on_success(once=True)
    async def handle_success(push_status):
        await push_status(stage="", state="complete")


def cleanup_builtin_status_hooks():
    """
    Clear callbacks for built-in status hooks.

    This prevents carryover of hooks between tests. Carryover won't be encountered in
    production because workflow processes exit after one run.

    TODO: Find a better way to isolate hooks to workflow runs.

    """
    on_step_start.clear()
    on_failure.clear()
    on_cancelled.clear()
    on_success.clear()
    on_error.clear()
    on_terminated.clear()


async def run_workflow(
    config: Dict[str, Any],
    job_id: str,
    workflow: Workflow,
    events: Events,
) -> Dict[str, Any]:
    # Configure hooks here so that they can be tested when using `run_workflow`.
    configure_builtin_status_hooks()

    async with FixtureScope() as scope:
        scope["config"] = config
        scope["job_id"] = job_id

        execute_task = asyncio.create_task(execute(workflow, scope, events))

        try:
            await execute_task
        except asyncio.CancelledError:
            execute_task.cancel()

        await execute_task

        cleanup_builtin_status_hooks()

        return scope.get("results", {})


@runs_in_new_fixture_context()
async def start_runtime(
    dev: bool,
    fixtures_file: Path,
    jobs_api_connection_string: str,
    mem: int,
    proc: int,
    redis_connection_string: str,
    redis_list_name: str,
    timeout: int,
    sentry_dsn: str,
    work_path: Path,
    workflow_file: Path,
):
    configure_logs(dev)
    configure_sentry(sentry_dsn)

    workflow = configure_workflow(fixtures_file, workflow_file)

    config = dict(
        dev=dev,
        jobs_api_connection_string=jobs_api_connection_string,
        mem=mem,
        proc=proc,
        work_path=work_path,
    )

    async with configure_redis(redis_connection_string, timeout=15) as redis:
        try:
            job_id = await get_next_job_with_timeout(redis_list_name, redis, timeout)
        except asyncio.TimeoutError:
            # This happens due to Kubernetes scheduling issues or job cancellations. It
            # is not an error.
            logger.warning("Timed out while waiting for job")
            sys.exit(0)

    events = Events()

    workflow_run = asyncio.create_task(run_workflow(config, job_id, workflow, events))

    def terminate_workflow(*_):
        events.terminated.set()
        workflow_run.cancel()

    signal.signal(signal.SIGTERM, terminate_workflow)

    def cancel_workflow(*_):
        events.cancelled.set()
        workflow_run.cancel()

    async with configure_redis(redis_connection_string, timeout=15) as redis:
        cancellation_task = asyncio.create_task(
            wait_for_cancellation(redis, job_id, cancel_workflow)
        )

        await workflow_run

        cancellation_task.cancel()
        await cancellation_task

    if events.terminated.is_set():
        sys.exit(124)
