"""arq worker entrypoint: `arq workers.arq_worker.WorkerSettings`.

Pipeline stages and cron jobs are registered here as later milestones add them.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.worker import func

from core.config import ensure_data_dirs, get_config, get_settings
from core.logging import setup_logging
from core.redis import ARQ_QUEUE

log = logging.getLogger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


async def embed_abstracts_job(ctx: dict[str, Any]) -> str:
    from ingest.abstract_embed import embed_abstracts

    return await embed_abstracts(ctx["cfg"])


async def recompute_scores_job(ctx: dict[str, Any]) -> str:
    from ingest.scores import recompute_scores

    return await recompute_scores(ctx["cfg"])


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging()
    ctx["cfg"] = get_config()
    ensure_data_dirs()
    log.info("worker started")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        ping,
        func(embed_abstracts_job, timeout=6 * 3600, max_tries=1),
        func(recompute_scores_job, timeout=1800, max_tries=1),
    ]
    on_startup = startup
    queue_name = ARQ_QUEUE
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    health_check_interval = 30
