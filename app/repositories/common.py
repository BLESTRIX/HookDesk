from uuid import UUID

from sqlalchemy import func, select

from app.core.errors import AppError


def owned(db, model, resource_id, principal, *, lock=False):
    try:
        resource_id = str(UUID(str(resource_id)))
    except ValueError:
        raise AppError(404, "not_found", "Resource not found") from None
    query = select(model).where(
        model.id == resource_id, model.organization_id == principal.organization_id
    )
    if lock:
        query = query.with_for_update()
    result = db.scalar(query)
    if result is None:
        raise AppError(404, "not_found", "Resource not found")
    return result


def paginate(db, query, page=1, page_size=25):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list(db.scalars(query.offset((page - 1) * page_size).limit(page_size)))
    return rows, total
