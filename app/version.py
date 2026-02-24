import os

APP_VERSION: str = os.getenv("APP_VERSION", "unknown")
BUILD_TIME_UTC: str = os.getenv("BUILD_TIME_UTC", "unknown")


def get_build_info() -> dict:
    return {
        "app_version": APP_VERSION,
        "build_time_utc": BUILD_TIME_UTC,
    }
