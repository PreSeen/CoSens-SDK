"""Run RSI in the developer's process, with the developer's model and CoSens only as Memory."""

from ..models import result
from .publish import recall_published
from .recipes import LocalRecipeLifecycle
from .store import LocalStore
from .workflow import LocalTaskWorkflow


class LocalRSI(LocalTaskWorkflow, LocalRecipeLifecycle):
    """The workflow methods of ``client.atoms``, backed by local storage and ``model``.

    ``memory`` is a ``CoSensClient`` used only for ``/v1/memory/*``; with ``None`` nothing is recalled or published.
    Generation runs inside ``wait_workflow_job``.
    """

    def __init__(self, *, memory, model, state_path=None):
        self._atoms = memory.atoms if memory is not None else None
        self._model = model
        self._store = LocalStore(state_path)

    def close(self):
        self._store.close()

    async def recall_recipes(self, query, *, token_budget=8000):
        """Find Recipes published to Memory, including from other machines."""
        if self._atoms is None:
            raise ValueError("recall_recipes requires a memory client")
        return result(await recall_published(self._atoms, query, token_budget))

    async def publish_recipe(self, recipe_id):
        """Retry publishing the current version of a Recipe chain."""
        if self._atoms is None:
            raise ValueError("publish_recipe requires a memory client")
        with self._store.transaction() as db:
            head = db.execute("SELECT current_candidate_id FROM recipe_heads WHERE recipe_id=?", (recipe_id,)).fetchone()
            if head is None or not head["current_candidate_id"]:
                raise LookupError("usable Recipe not found")
            candidate_id = head["current_candidate_id"]
            state = self._candidate(db, candidate_id)["publish_state"]
        if state != "published":
            state = await self._publish_candidate(candidate_id)
        return result({"recipe_id": recipe_id, "recipe_candidate_id": candidate_id, "publish_state": state})
