import math

AGV_WIDTH_M = 0.20
AGV_LENGTH_M = 0.20
AGV_TURN_RADIUS_M = math.hypot(AGV_WIDTH_M, AGV_LENGTH_M) / 2


def heading_to_yaw(heading_deg):
    """Convert map-up clockwise degrees to +X counterclockwise radians."""
    return math.remainder(math.radians(90 - heading_deg), math.tau)
