TASK: classify what this message asks Tiffin to do.

Message: [[text]]
Today: [[today]]

Kinds and payloads:
log_offplan - ate or drank something not on the menu; payload {"description": <their words>, "day": "YYYY-MM-DD"}
confirm_day - says whether the day went as planned; payload {"day": "YYYY-MM-DD", "as_planned": true or false, "note": <their words or null>}
rate_dish - rates a dish; payload {"dish": <their words>, "score": 1 to 5}
set_pause - wants to pause tracking for a period; payload {"start_day": "YYYY-MM-DD", "end_day": "YYYY-MM-DD", "reason": <their words or null>}
set_plan - describes a training week; payload {"text": <their words>}
set_profile - changes a setting such as times, tone, meals or foods; payload {"text": <their words>}

Copy their words into the payload; add no quantity they did not state. Resolve relative dates against today.
