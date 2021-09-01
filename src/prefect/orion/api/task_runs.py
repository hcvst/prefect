from typing import List
from uuid import UUID

import sqlalchemy as sa
from fastapi import Body, Depends, HTTPException, Path, Response, status

from prefect.orion import models, schemas
from prefect.orion.api import dependencies
from prefect.orion.utilities.server import OrionRouter

router = OrionRouter(prefix="/task_runs", tags=["Task Runs"])


@router.post("/")
async def create_task_run(
    task_run: schemas.actions.TaskRunCreate,
    response: Response,
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> schemas.core.TaskRun:
    """
    Create a task run
    """
    nested = await session.begin_nested()
    try:
        task_run = await models.task_runs.create_task_run(
            session=session, task_run=task_run
        )
        response.status_code = status.HTTP_201_CREATED
    except sa.exc.IntegrityError:
        await nested.rollback()
        query = sa.select(models.orm.TaskRun).filter(
            sa.and_(
                models.orm.TaskRun.flow_run_id == task_run.flow_run_id,
                models.orm.TaskRun.task_key == task_run.task_key,
                models.orm.TaskRun.dynamic_key == task_run.dynamic_key,
            )
        )
        result = await session.execute(query)
        task_run = result.scalar()
    return task_run


@router.get("/{id}")
async def read_task_run(
    task_run_id: UUID = Path(..., description="The task run id", alias="id"),
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> schemas.core.TaskRun:
    """
    Get a task run by id
    """
    task_run = await models.task_runs.read_task_run(
        session=session, task_run_id=task_run_id
    )
    if not task_run:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_run


@router.get("/")
async def read_task_runs(
    flow_run_id: UUID,
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> List[schemas.core.TaskRun]:
    """
    Query for task runs
    """
    return await models.task_runs.read_task_runs(
        session=session, flow_run_id=flow_run_id
    )


@router.delete("/{id}", status_code=204)
async def delete_task_run(
    task_run_id: UUID = Path(..., description="The task run id", alias="id"),
    session: sa.orm.Session = Depends(dependencies.get_session),
):
    """
    Delete a task run by id
    """
    result = await models.task_runs.delete_task_run(
        session=session, task_run_id=task_run_id
    )
    if not result:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.post("/{id}/set_state", status_code=201)
async def set_task_run_state(
    task_run_id: UUID = Path(..., description="The task run id", alias="id"),
    state: schemas.actions.StateCreate = Body(..., description="The intended state."),
    session: sa.orm.Session = Depends(dependencies.get_session),
    response: Response = None,
) -> schemas.responses.SetStateResponse:
    """Set a task run state, invoking any orchestration rules."""

    # create the state
    orchestration_result = await models.task_run_states.orchestrate_task_run_state(
        session=session,
        task_run_id=task_run_id,
        state=state,
    )

    return schemas.responses.SetStateResponse(
        state=orchestration_result.state,
        status=orchestration_result.status,
    )
