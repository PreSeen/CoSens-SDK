# Execution capture and explicit generation

`mode="auto"` selects existing context and captures Task/Execution/Revision. It does not generate or update Recipes. The host owns execution and artifact confirmation.

After confirmation, invoke `.cosens.generate_recipe(turn_id, request_key=...)`. Record feedback through `.cosens.feedback(...)` or the atomic feedback method; correction prepares a new execution. After the corrected artifact is confirmed, invoke the same generation method for the corrected turn/revision.

Only a turn with a submitted Revision can generate a Recipe. When `auto` decides `activate: false`, no Revision exists; check `client.cosens.get_turn(turn_id)` for `revision_id` before calling `generate_recipe`. If the decision or read-only policy lookup fails, the wrapper returns the native model response without creating a Task.

Related Memory is read on every explicit generation. Feedback attribution and Recipe distillation run inside that job. Recovery and polling reuse the existing job and never infer user confirmation.
