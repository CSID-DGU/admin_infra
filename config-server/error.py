def infra_error(step, error, detail, progress=None, rollback=None, **extra):
    body = {
        "step": step,
        "error": error,
        "detail": detail,
        "progress": progress if progress is not None else (rollback or {}),
    }
    body.update({key: value for key, value in extra.items() if value is not None})
    return body


def k8s_error_fields(exc):
    return {
        "k8s_status": exc.status,
        "k8s_reason": exc.reason,
        "k8s_body": exc.body,
    }
