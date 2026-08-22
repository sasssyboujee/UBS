Adaptive API Gateway Challenge (Student Guide)
Context
Server A recently moved from Version 1 (V1) to Version 2 (V2). The participant server is expected to help bridge the old and new models.

Goal
Implement a server that exposes POST /solve and returns a transformed payload based on the incoming request.

Required Endpoint
POST /solve
Sample Request
The endpoint receives:

```json
{
  "payload": "ewoJImFkYXB0SW5wdXQiOiB7CgkJInVzZXIiOiB7CgkJCSJpZCI6ICJVNDIiLAoJCQkiZnVsbE5hbWUiOiAiSmFuZSBEb2UiCgkJfSwKCQkiYWN0aW9uIjogIkNSRUFURSIsCgkJIm1ldGFkYXRhIjogewoJCQkicHJpb3JpdHkiOiAiSElHSCIKCQl9Cgl9Cn0="
}
```

where the payload somehow decodes to this:

```json
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
```

    }

}
Sample Response
Your POST /solve must return JSON in this shape:

```json
{
    "adaptOutput": {
        "id": "U42",
        "name": "Jane Doe",
        "action": "create",
        "priority": 3
    }
```

}
