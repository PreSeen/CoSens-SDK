"""Task workflow HTTP methods; execution remains under host control."""
from .atoms_helpers import path_part
from urllib.parse import urlencode


def validate_artifact(name, content):
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ValueError('artifact name must be nonempty and at most 256 characters')
    if not isinstance(content, str) or not content or len(content.encode('utf-8')) > 1_000_000:
        raise ValueError('artifact content must be nonempty text of at most 1,000,000 UTF-8 bytes')


def _route(method, path, *, wrapped=False, body=False):
    def prepare(resource_id=None, *, data=None):
        if '{id}' in path:
            path_value = path.replace('{id}', path_part(resource_id))
        else:
            if resource_id is not None:
                raise TypeError('this method does not accept a resource ID')
            path_value = path
        if not body and data is not None:
            raise TypeError('this method does not accept data')
        if body and not isinstance(data, dict):
            raise TypeError('data must be an object')
        return path_value, ({'data': data} if wrapped else data)
    return method, prepare


_ROUTES = {
    'start_session': _route('POST', '/v1/sessions/start', body=True),
    'append_session_events': _route('POST', '/v1/sessions/{id}/events', body=True),
    'end_session': _route('POST', '/v1/sessions/{id}/end'),
    'create_task_question': _route('POST', '/v1/tasks/{id}/questions', body=True),
    'list_task_questions': _route('GET', '/v1/tasks/{id}/questions'),
    'answer_task_question': _route('POST', '/v1/questions/{id}/answers', body=True),
    'create_task_rubric': _route('POST', '/v1/tasks/{id}/rubrics', body=True),
    'revise_task_rubric': _route('POST', '/v1/rubrics/{id}/revisions', body=True),
    'create_learning_job': _route('POST', '/v1/tasks/{id}/learning-jobs', body=True),
    'get_learning_job': _route('GET', '/v1/learning-jobs/{id}'),
    'get_learning_rounds': _route('GET', '/v1/learning-jobs/{id}/rounds'),
    'resume_learning_job': _route('POST', '/v1/learning-jobs/{id}/resume'),
    'cancel_learning_job': _route('POST', '/v1/learning-jobs/{id}/cancel'),
    'start_learning_validation': _route('POST', '/v1/learning-jobs/{id}/validations', body=True),
    'get_learning_validation': _route('GET', '/v1/validations/{id}'),
    'record_recipe_use': _route('POST', '/v1/tasks/{id}/recipe-use', body=True),
    'record_recipe_outcome': _route('POST', '/v1/tasks/{id}/outcomes', body=True),
    'get_artifact': _route('GET', '/v1/artifacts/{id}'),
    'create_task': _route('POST', '/v1/tasks', body=True),
    'get_task': _route('GET', '/v1/tasks/{id}'),
    'update_task_context': _route('POST', '/v1/tasks/{id}/context', body=True),
    'start_task_analysis': _route('POST', '/v1/tasks/{id}/analysis'),
    'get_task_analysis': _route('GET', '/v1/task-analyses/{id}'),
    'resume_task_analysis': _route('POST', '/v1/task-analyses/{id}/resume'),
    'start_reconstruction': _route('POST', '/v1/tasks/{id}/reconstructions'),
    'start_execution': _route('POST', '/v1/tasks/{id}/executions'),
    'get_execution': _route('GET', '/v1/executions/{id}'),
    'submit_execution_result': _route('POST', '/v1/executions/{id}/result', body=True),
    'get_revision': _route('GET', '/v1/revisions/{id}'),
    'start_attribution': _route('POST', '/v1/revisions/{id}/attributions', body=True, wrapped=True),
    'get_attribution': _route('GET', '/v1/attributions/{id}'),
    'resume_attribution': _route('POST', '/v1/attributions/{id}/resume'),
    'select_attribution': _route('POST', '/v1/attributions/{id}/selection', body=True, wrapped=True),
    'start_revision': _route('POST', '/v1/revisions/{id}/revisions', body=True, wrapped=True),
    'accept_revision': _route('POST', '/v1/revisions/{id}/acceptance', body=True, wrapped=True),
    'start_recipe_candidate': _route('POST', '/v1/tasks/{id}/recipe-candidates', body=True, wrapped=True),
    'get_recipe_candidate': _route('GET', '/v1/recipe-candidates/{id}'),
    'resume_recipe_candidate': _route('POST', '/v1/recipe-candidates/{id}/resume'),
    'start_recipe_verification': _route('POST', '/v1/recipe-candidates/{id}/verifications'),
    'get_recipe_verification': _route('GET', '/v1/recipe-verifications/{id}'),
}


class AsyncWorkflowAtoms:
    async def get_session_context(self, session_id, *, after=0):
        return await self._http.request('GET', _query_path('/v1/sessions/{id}/context', session_id, 'after', after))

    async def get_task_rubric(self, rubric_id, *, version=None):
        return await self._http.request('GET', _query_path('/v1/rubrics/{id}', rubric_id, 'version', version))


class SyncWorkflowAtoms:
    def get_session_context(self, session_id, *, after=0):
        return self._http.request('GET', _query_path('/v1/sessions/{id}/context', session_id, 'after', after))

    def get_task_rubric(self, rubric_id, *, version=None):
        return self._http.request('GET', _query_path('/v1/rubrics/{id}', rubric_id, 'version', version))


def _query_path(path, resource_id, name, value):
    path = path.replace('{id}', path_part(resource_id))
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if name == 'after' else 1):
            raise ValueError(f'invalid {name}')
        path += '?' + urlencode({name: value})
    return path


def _install(name, method, prepare):
    async def asynchronous(self, resource_id=None, *, data=None):
        path, body = prepare(resource_id, data=data)
        return await self._http.request(method, path, body=body)
    def synchronous(self, resource_id=None, *, data=None):
        path, body = prepare(resource_id, data=data)
        return self._http.request(method, path, body=body)
    asynchronous.__name__ = synchronous.__name__ = name
    asynchronous.__doc__ = synchronous.__doc__ = f'{method} workflow endpoint. Pass request fields in data.'
    setattr(AsyncWorkflowAtoms, name, asynchronous)
    setattr(SyncWorkflowAtoms, name, synchronous)


for _name, (_method, _prepare) in _ROUTES.items():
    _install(_name, _method, _prepare)
