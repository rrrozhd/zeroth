from datetime import datetime

from pydantic import BaseModel


class GroundTruthCostIn(BaseModel):
    period_start: datetime
    period_end: datetime
    capability_id: str
    component: str
    amount_usd: float


class GroundTruthImportRequest(BaseModel):
    rows: list[GroundTruthCostIn]
