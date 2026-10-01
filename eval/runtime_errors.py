"""Identify terminal scaffold errors in native session exports."""

def native_session_error(export: object) -> str | None:
    if not isinstance(export, dict):
        return None
    assistants = [
        row for row in export.get("messages", [])
        if isinstance(row, dict)
        and isinstance(row.get("data"), dict)
        and row["data"].get("role") == "assistant"
    ]
    if not assistants:
        return None
    error = assistants[-1]["data"].get("error")
    if not error:
        return None
    if isinstance(error, dict):
        data = error.get("data", {})
        if isinstance(data, dict) and data.get("message"):
            return f"{error.get('name', 'runtime_error')}: {data['message']}"
    return str(error)
