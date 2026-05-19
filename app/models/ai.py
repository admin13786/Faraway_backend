from pydantic import BaseModel, Field


class GenerateStrategyRequest(BaseModel):
    destination: str = Field(min_length=1, max_length=100)
    days: int = Field(default=3, gt=0, le=60)
    budget: str = Field(default="中等", max_length=50)
    hotel_req: str = Field(default="不限", max_length=200)
    allergies: str = Field(default="无", max_length=200)
    pace: str = Field(default="适中", max_length=50)
    group_type: str = Field(default="自由行", max_length=50)


class DailyPlan(BaseModel):
    day: int
    activities: list[str]
    food: list[str]
    accommodation: str


class GeneratedStrategy(BaseModel):
    destination: str
    overview: str
    daily_plans: list[DailyPlan]
    tips: list[str]
