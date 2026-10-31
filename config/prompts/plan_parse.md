TASK: turn a person's description of their training week into the typed plan.

Text: [[text]]

Kinds: climb, ropes, ride, run, strength, mobility, swim, class, walk, other, rest.
Return sessions for all seven weekdays, one entry per weekday, 0 = Monday to 6 = Sunday, in that order. A day not mentioned is rest with target_minutes 0 and start_time null. target_minutes is the stated duration in minutes, or 60 when a session is named without one. start_time is HH:MM when given, otherwise null. Anything not in the list of kinds is other. Two sessions on one day: keep the longer one.
