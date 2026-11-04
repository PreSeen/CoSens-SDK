# Execution capture and explicit generation

`mode="auto"` selects existing context and captures Task/Execution/Revision. It does not generate or update Recipes. The host owns execution and artifact confirmation.

After confirmation, invoke `.cosens.generate_recipe(turn_id, request_key=...)`. Record feedback through `.cosens.feedback(...)` or the atomic feedback method; correction prepares a new execution. After the corrected artifact is confirmed, invoke the same generation method for the corrected turn/revision.

Related Memory is read on every explicit generation. Feedback attribution and Recipe distillation run inside that job. Recovery and polling reuse the existing job and never infer user confirmation.
