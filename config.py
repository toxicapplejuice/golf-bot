"""Configuration for Austin Golf Booking Bot"""

# Base URLs
BASE_URL = "https://txaustinweb.myvscloud.com/webtrac/web"
SEARCH_URL = f"{BASE_URL}/search.html?display=detail&module=GR"
LOGIN_URL = f"{BASE_URL}/login.html"

# Course codes from Vermont Systems (secondarycode values in the dropdown).
# Ordered by search priority: Lions > Roy Kizer > Jimmy Clay > Morris Williams.
# Dict iteration order matters — the bot searches courses in this order.
COURSE_CODES = {
    "4": "Lions",
    "2": "Roy Kizer",
    "1": "Jimmy Clay",
    "3": "Morris Williams",
}

# Time preferences in order of priority
TIME_PRIORITY = [
    # 9am block
    "9:00 AM", "9:08 AM", "9:16 AM", "9:24 AM", "9:32 AM", "9:40 AM", "9:48 AM",
    # 8am block
    "8:00 AM", "8:08 AM", "8:16 AM", "8:24 AM", "8:32 AM", "8:40 AM", "8:48 AM",
    # 10am block
    "10:00 AM", "10:08 AM", "10:16 AM", "10:24 AM", "10:32 AM", "10:40 AM", "10:48 AM",
    # 11am block
    "11:00 AM", "11:08 AM", "11:16 AM", "11:24 AM", "11:32 AM", "11:40 AM", "11:48 AM",
    # 12pm block
    "12:00 PM", "12:08 PM", "12:16 PM", "12:24 PM", "12:32 PM", "12:40 PM", "12:48 PM",
    # 1pm block
    "1:00 PM", "1:08 PM", "1:16 PM", "1:24 PM", "1:32 PM", "1:40 PM", "1:48 PM",
    # Later afternoon
    "2:00 PM", "2:08 PM", "2:16 PM", "2:24 PM", "2:32 PM", "2:40 PM", "2:48 PM",
    "3:00 PM", "3:08 PM", "3:16 PM", "3:24 PM", "3:32 PM", "3:40 PM", "3:48 PM",
    "4:00 PM", "4:08 PM", "4:16 PM", "4:24 PM", "4:32 PM", "4:40 PM", "4:48 PM",
    "5:00 PM", "5:08 PM", "5:16 PM", "5:24 PM", "5:32 PM", "5:40 PM", "5:48 PM",
]

# Number of players (default; override with --players CLI flag)
NUM_PLAYERS = 4

# If no slots found for NUM_PLAYERS, retry with this many.
# Set to None to disable the fallback.
FALLBACK_NUM_PLAYERS = 2

# Booking window (8am-1pm preferred)
MIN_HOUR = 8   # 8 AM
MAX_HOUR = 13  # 1 PM (inclusive)

# Fallback window — if morning window yields nothing, widen search up to this hour
FALLBACK_MAX_HOUR = 17  # 5 PM (inclusive)
