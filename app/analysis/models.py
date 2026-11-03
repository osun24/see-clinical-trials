from typing import Literal
from pydantic import BaseModel


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class Expand(StrictModel):
    op: Literal["expand"]
    id: str
    input: str
    collection: Literal["locations", "interventions", "conditions", "officials"]


class Filter(StrictModel):
    op: Literal["filter"]
    id: str
    input: str
    field: str
    predicate: Literal["equals", "not_equals", "contains", "present"]
    value: str = ""


class Derive(StrictModel):
    op: Literal["derive"]
    id: str
    input: str
    field: str
    output: str
    transform: Literal["lower", "strip", "year", "months_between"]
    other_field: str | None = None


class Relationship(StrictModel):
    op: Literal["relationship"]
    id: str
    input: str
    x: str
    y: str


class Memberships(StrictModel):
    """A registered extraction preset compiled into the ordinary executor."""
    op: Literal["memberships"]
    id: str
    input: str
    dimension: str
    output: str


class Aggregate(StrictModel):
    op: Literal["aggregate"]
    id: str
    input: str
    group_by: list[str]
    measure: Literal["count_distinct_trials", "count_rows"]
    missing: Literal["exclude", "bucket"] = "exclude"


class Sort(StrictModel):
    op: Literal["sort"]
    id: str
    input: str
    field: Literal["label", "value"]
    descending: bool = True


class Limit(StrictModel):
    op: Literal["limit"]
    id: str
    input: str
    count: int
    tail: Literal["drop", "other"]


Step = Memberships | Expand | Filter | Derive | Relationship | Aggregate | Sort | Limit


class AnalysisPlan(StrictModel):
    version: Literal["1"] = "1"
    steps: list[Step]
