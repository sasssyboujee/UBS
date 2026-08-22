Adaptive API Gateway Challenge (Student Guide)
Context
Server A recently moved from Version 1 (V1) to Version 2 (V2). The participant server is expected to help bridge the old and new models while also reporting service-health metrics from heartbeat data.

Goal
Implement a server that exposes POST /solve and returns a combined response based on the incoming request.

Required Endpoint
POST /solve
Sample Request
The endpoint receives:

{
    "payload": "ewoJImFkYXB0SW5wdXQiOiB7CgkJInVzZXIiOiB7CgkJCSJpZCI6ICJVNDIiLAoJCQkiZnVsbE5hbWUiOiAiSmFuZSBEb2UiCgkJfSwKCQkiYWN0aW9uIjogIkNSRUFURSIsCgkJIm1ldGFkYXRhIjogewoJCQkicHJpb3JpdHkiOiAiSElHSCIKCQl9Cgl9LAoJImhlYXJ0YmVhdHMiOiBbCgkJewoJCQkic2VydmljZSI6ICJhdXRoIiwKCQkJInRpbWVzdGFtcCI6IDE3MTAwMDAxMjMsCgkJCSJsYXRlbmN5TXMiOiAxMjAsCgkJCSJzdGF0dXMiOiAiT0siCgkJfSwKCQl7CgkJCSJzZXJ2aWNlIjogImF1dGgiLAoJCQkidGltZXN0YW1wIjogMTcxMDAwMDEyNSwKCQkJImxhdGVuY3lNcyI6IDE4MCwKCQkJInN0YXR1cyI6ICJGQUlMIgoJCX0sCgkJewoJCQkic2VydmljZSI6ICJhdXRoIiwKCQkJInRpbWVzdGFtcCI6IDE3MTAwMDAxMjEsCgkJCSJsYXRlbmN5TXMiOiA5NSwKCQkJInN0YXR1cyI6ICJPSyIKCQl9CgldLAoJInNsb1F1ZXJ5IjogewoJCSJzZXJ2aWNlIjogImF1dGgiLAoJCSJzaW5jZSI6IDE3MTAwMDAxMjMKCX0KfQ=="
}
where the payload somehow decodes to this:

{
    "adaptInput": {
        "user": {
            "id": "U42",
            "fullName": "Jane Doe"
        },
        "action": "CREATE",
        "metadata": {
            "priority": "HIGH"
        }
    },
    "heartbeats": [
        {
            "service": "auth",
            "timestamp": 1710000123,
            "latencyMs": 120,
            "status": "OK"
        },
        {
            "service": "auth",
            "timestamp": 1710000125,
            "latencyMs": 180,
            "status": "FAIL"
        },
        {
            "service": "auth",
            "timestamp": 1710000121,
            "latencyMs": 95,
            "status": "OK"
        }
    ],
    "sloQuery": {
        "service": "auth",
        "since": 1710000123
    }
}
Sample Response
Your POST /solve must return JSON in this shape:

{
    "adaptOutput": {
        "id": "U42",
        "name": "Jane Doe",
        "action": "create",
        "priority": 3
    },
    "sloOutput": {
        "availability": 0.5,
        "p95LatencyMs": 180
    }
}