"""AWS Lambda handler for golf booking bot."""

import json
from datetime import datetime
from bot import run_booking


def handler(event, context):
    """Lambda entry point. Triggered by EventBridge schedule."""
    print(f"Lambda invoked at {datetime.now().isoformat()}")

    # Lambda always runs immediately (no waiting for 8pm - EventBridge handles scheduling)
    results = run_booking(skip_wait=True)

    response = {
        "statusCode": 200,
        "body": json.dumps({
            "saturday": results["saturday"],
            "sunday": results["sunday"],
            "timestamp": datetime.now().isoformat(),
        })
    }

    print(f"Results: {json.dumps(response['body'])}")
    return response
