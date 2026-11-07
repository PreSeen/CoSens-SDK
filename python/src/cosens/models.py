"""Small JSON result helpers that preserve server fields."""


class Result(dict):
    """A server JSON object. ``request_id`` carries the response's ``x-request-id``.

    A ``request_id`` field sent by the server in the body takes precedence.
    """

    _request_id = None

    @property
    def request_id(self):
        return self.get("request_id", self._request_id)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def result(value):
    if isinstance(value, dict):
        return Result({key: result(item) for key, item in value.items()})
    if isinstance(value, list):
        return [result(item) for item in value]
    return value
