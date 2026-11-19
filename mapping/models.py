from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class SensorMount(Contract):
    sensor_id: str = Field(min_length=1, max_length=40, pattern=r'^[A-Za-z0-9_-]+$')
    x_m: float
    y_m: float
    yaw_rad: float
    fov_rad: float = Field(gt=0, le=3.141593)
    min_range_m: float = Field(gt=0)
    max_range_m: float = Field(gt=0, le=20)
    range_sigma_m: float = Field(gt=0, le=1)

    @model_validator(mode='after')
    def range_order(self):
        if self.max_range_m <= self.min_range_m:
            raise ValueError('max_range_m must exceed min_range_m')
        return self


class CameraViewMount(Contract):
    channel: Literal['a', 'b']
    x_m: float
    y_m: float
    yaw_rad: float
    fov_rad: float = Field(gt=0, le=3.141593)
    max_range_m: float = Field(gt=0, le=20)


class MapConfig(Contract):
    frame_id: str = Field(min_length=1, max_length=60)
    width_m: float = Field(gt=0, le=100)
    height_m: float = Field(gt=0, le=100)
    resolution_m: float = Field(ge=0.02, le=1)
    origin_x_m: float = 0
    origin_y_m: float = 0
    sensors: list[SensorMount] = Field(min_length=1, max_length=8)
    camera_views: list[CameraViewMount] = Field(default_factory=list, max_length=2)
    max_age_s: float = Field(default=1, gt=0, le=5)
    max_sync_error_s: float = Field(default=0.1, gt=0, le=0.5)
    min_pose_confidence: float = Field(default=0.8, ge=0, le=1)
    max_pose_position_std_m: float = Field(default=0.1, gt=0, le=1)
    max_pose_yaw_std_rad: float = Field(default=0.1, gt=0, le=1)
    enable_free_updates: StrictBool = False

    @model_validator(mode='after')
    def bounded_grid(self):
        import math
        cols, rows = self.width_m / self.resolution_m, self.height_m / self.resolution_m
        if not math.isclose(cols, round(cols)) or not math.isclose(rows, round(rows)):
            raise ValueError('map dimensions must be multiples of resolution_m')
        if round(cols) * round(rows) > 40000:
            raise ValueError('map is limited to 40000 cells')
        ids = [sensor.sensor_id for sensor in self.sensors]
        if len(set(ids)) != len(ids):
            raise ValueError('sensor IDs must be unique')
        channels = {view.channel for view in self.camera_views}
        if len(channels) != len(self.camera_views):
            raise ValueError('camera channels must be unique')
        return self


class Pose(Contract):
    frame_id: str
    timestamp_s: float = Field(gt=0)
    x_m: float
    y_m: float
    yaw_rad: float
    valid: StrictBool
    confidence: float = Field(ge=0, le=1)
    position_std_m: float = Field(ge=0)
    yaw_std_rad: float = Field(ge=0)
    source: Literal['aruco', 'odometry', 'fused', 'external_measured']


class RangeObservation(Contract):
    sensor_id: str
    timestamp_s: float = Field(gt=0)
    distance_m: float | None = Field(default=None, gt=0)
    valid: StrictBool
    return_type: Literal['echo', 'no_echo']
    source: Literal['hardware', 'recorded']


class ObservationBatch(Contract):
    pose: Pose
    measurements: list[RangeObservation] = Field(min_length=1, max_length=32)
