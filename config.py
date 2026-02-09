"""Configuration for Austin Golf Booking Bot"""

# Base URLs
BASE_URL = "https://txaustinweb.myvscloud.com/webtrac/web"
SEARCH_URL = f"{BASE_URL}/search.html?display=detail&module=GR"
LOGIN_URL = f"{BASE_URL}/login.html"

# Course codes from Vermont Systems
# These are the secondarycode values in the dropdown
# Ordered by priority: Lions > Roy Kizer > Jimmy Clay
COURSE_CODES = {
    "4": "Lions",
    "2": "Roy Kizer",
    "1": "Jimmy Clay",
}

# Target courses in priority order
TARGET_COURSES = [
    "Lions",
    "Roy Kizer",
    "Jimmy Clay",
]

# Time preferences in order of priority
# 9am > 9:30am > 10am > 10:30am > 8am > 11am
TIME_PRIORITY = [
    "9:00 AM",
    "9:08 AM",
    "9:16 AM",
    "9:24 AM",
    "9:32 AM",
    "9:40 AM",
    "9:48 AM",
    "10:00 AM",
    "10:08 AM",
    "10:16 AM",
    "10:24 AM",
    "10:32 AM",
    "10:40 AM",
    "10:48 AM",
    "8:00 AM",
    "8:08 AM",
    "8:16 AM",
    "8:24 AM",
    "8:32 AM",
    "8:40 AM",
    "8:48 AM",
    "11:00 AM",
    "11:08 AM",
    "11:16 AM",
    "11:24 AM",
    "11:32 AM",
    "11:40 AM",
    "11:48 AM",
]

# Number of players
NUM_PLAYERS = 4

# Booking window
MIN_HOUR = 8   # 8 AM
MAX_HOUR = 11  # 11 AM (inclusive)
