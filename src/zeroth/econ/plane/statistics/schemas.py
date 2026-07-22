from pydantic import BaseModel


class IntervalEstimate(BaseModel):
    mean: float
    low: float
    high: float


class CalibrationSummary(BaseModel):
    capability_id: str
    mape: float
    mae: float
    rmse: float
    interval_coverage: float
    bias: float
    sample_size: int
