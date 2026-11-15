# Execution capture and explicit learning

`mode="auto"` selects existing context and captures a Task and its Execution. It does not learn or update Recipes. The host owns execution and artifact confirmation.

A turn has a learnable execution only when `client.cosens.get_turn(turn_id)` shows `state == "completed"` and an `execution_id`. When `auto` decides `activate: false`, or the decision or read-only policy lookup fails, there is no execution: the wrapper returns the native model response without creating a Task.

After the user confirmed the artifact, call `.cosens.generate_recipe(turn_id)` and `.cosens.wait_learning(turn_id)`; [the API reference](api-reference.md) has the learning rules, statuses and errors. The wrapper's `wait_learning` stores the turn's learning record before it raises, so `.cosens.resume_learning(turn_id)` can continue a resumable learning.

Record feedback through `.cosens.feedback(message, source=...)` or `client.atoms.record_feedback(execution_id, ...)`. When the decision classifies a follow-up message as feedback on the previous turn, the wrapper records it on that turn's execution and starts this turn as a correction (`previous_execution_id`, key `auto-correction:<turn_id>`). A correction turn makes no Recipe lookup: it injects the Recipe the service binds to the correction from the task, and no Memory evidence. After the corrected artifact is confirmed, call `generate_recipe` for the correction turn to learn the next version.

The Recipe lookup offers the matcher only `generated` Recipes (`list_recipes(limit=100, origin="generated")`, then `get_recipe`), because only those can be applied to an execution. Related Memory is read during learning only when the execution was started with `recall_memory=True`. Feedback attribution and Recipe distillation run inside the learning. Recovery and polling never infer user confirmation.
