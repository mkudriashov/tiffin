"""Scheduled SQLite backup, integrity checks and retention."""

from __future__ import annotations

from .. import backup as backup_store
from .. import log
from ..clock import isoday
from ..scheduler import Job, JobContext, fixed

logger = log.get("tiffin.backup")


def run(ctx: JobContext) -> None:
    paths = ctx.app.config.paths
    day = isoday(ctx.day)
    try:
        manifest = backup_store.run_backup(
            paths.db,
            paths.backups,
            day,
            keep_days=int(ctx.app.config.get("ops.backup_keep_days", backup_store.KEEP_DAYS)),
            clock=ctx.clock,
        )
    except Exception as e:
        backup_store.write_last(
            paths.state,
            {
                "ok": False,
                "day": day,
                "error": f"{type(e).__name__}: {e}"[:300],
                "made_at": ctx.clock.utc_stamp(),
            },
        )
        logger.exception("backup failed", extra={"event": "backup_failed"})
        ctx.handle.degrade(f"{type(e).__name__}: {e}"[:200])
        ctx.outbox.alert(
            ctx.app.registry(ctx.db).admin(),
            "backup_failed",
            f"the {day} backup did not run: {type(e).__name__}: {e}",
        )
        return
    backup_store.write_last(
        paths.state,
        {
            "ok": True,
            "day": day,
            "made_at": manifest["made_at"],
            "db_sha256": manifest["db_sha256"],
            "pruned": manifest["pruned"],
        },
    )
    ctx.handle.note(f"{manifest['db_bytes']} bytes, {len(manifest['tables'])} tables")
    removed = log.prune(
        paths.logs,
        keep_days=int(ctx.app.config.get("ops.log_keep_days", 90)),
        clock=ctx.clock,
    )
    ctx.handle.note(f"{removed} old logs removed")


JOB = Job(
    name="backup",
    scope="system",
    kind="data",
    times=fixed("03:30"),
    run=run,
    catch_up="always",
    needs_profile=False,
)
