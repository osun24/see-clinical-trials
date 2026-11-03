"""Runtime checks for the public renderer contract, independent of planning."""
from app.models.errors import ServiceError


def check_response(response):
    body = response.model_dump(mode="json", exclude_none=True)
    viz = body["visualization"]
    data = viz["data"]
    def require(condition, message):
        if not condition:
            raise ServiceError("internal_error", "Visualization contract violation: " + message)
    if viz["type"] == "network_graph":
        ids = [n["id"] for n in data["nodes"]]
        require(len(ids) == len(set(ids)), "duplicate node IDs")
        for edge in data["edges"]:
            require(edge["source"] in ids and edge["target"] in ids, "dangling edge")
            require(edge["weight"] >= 0, "negative edge weight")
            require(edge["weight"] == 0 or bool(edge.get("citations")), "edge missing evidence")
        return response
    require(isinstance(data, list), "flat data array required")
    if viz["type"] == "none":
        require(not data and not any(v for v in viz["encoding"].values()), "none must have empty data and encoding")
        return response
    for channel in viz["encoding"].values():
        if isinstance(channel, dict) and "field" in channel:
            require(all(channel["field"] in row for row in data), "encoded field absent from a row")
    for row in data:
        nonzero = row.get("value", 1) != 0
        require(not nonzero or bool(row.get("citations")), "nonzero datum missing evidence")
    return response
