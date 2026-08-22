from app.toolbox import recall
import json

questions = {
    "When was the sensor grid last brought back into alignment?": "14 March",
    "Roughly how many licensed motormen operate service across the network?": "sixty-eight",
    "When did the board formally approve the arrangement for sharing the drying machine?": "21 May",
    "On what date did the air-scrubbing equipment break down?": "2 November",
    "What is the maximum number of bones allowed per skeleton?": "ninety",
    "How many triangles are allowed per streaming cell?": "forty thousand",
    "What is the daily fare cap for unlimited travel?": "four pounds ninety",
    "How many days can a cold-storage bay be left unused before it is forfeited?": "ninety",
    "What torque is required for the hydrophone gasket?": "12 newton-meters",
    "What torque is required for braking system caliper clamping bolts?": "nine newton-meters",
    "What is the threshold for alanine aminotransferase withdrawal?": "260",
    "What is the maintenance dose for Velmara?": "240",
    "Where is the Hollowlight Capture Stage?": "STOP_13",
    "Where is the Verity Observatory?": "STOP_05",
    "How many member households are in the cooperative?": "fifty-four"
}

failed = []
for q, expected in questions.items():
    res = recall(q)
    full_text = " ".join(res).lower()
    if expected.lower() not in full_text:
        print(f"FAILED: {q}")
        print(f"  Expected: {expected}")
        failed.append(q)

if not failed:
    print("ALL TESTS PASSED!")
